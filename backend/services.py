"""Business services. Call mutations inside Database.write(), including deduplication."""
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable
from .auth import digest
from .schemas import DepositInput, PickupInput
from .state import BusinessError, log, transition


def identifier() -> str:
    return str(uuid.uuid4())


def payload_hash(payload: dict) -> str:
    return digest(json.dumps(payload, sort_keys=True, separators=(',', ':')))


def idempotent(conn: sqlite3.Connection, actor_id: int, request_id: str,
               operation: str, payload: dict, action: Callable[[], dict]) -> dict:
    hashed = payload_hash(payload)
    old = conn.execute('SELECT * FROM request_records WHERE actor_id=? AND request_id=?',
                       (actor_id, request_id)).fetchone()
    if old:
        if old['operation'] != operation or old['payload_hash'] != hashed:
            raise BusinessError(409, 'request_id_payload_conflict')
        return json.loads(old['response'])
    response = action()
    conn.execute('INSERT INTO request_records VALUES (?,?,?,?,?)',
                 (actor_id, request_id, operation, hashed, json.dumps(response)))
    return response


def command(conn: sqlite3.Connection, transaction_id: str, action: str) -> str:
    existing = conn.execute('SELECT id FROM commands WHERE transaction_id=? AND action=?',
                            (transaction_id, action)).fetchone()
    if existing:
        return existing['id']
    command_id = identifier()
    conn.execute('INSERT INTO commands VALUES (?,?,?,?,?)',
                 (command_id, transaction_id, action, 'PENDING', time.time()))
    tx = conn.execute('SELECT * FROM transactions WHERE id=?', (transaction_id,)).fetchone()
    log(conn, tx['parcel_id'], transaction_id, tx['locker_id'], f'{action}_INTENT')
    return command_id


def allocate(conn: sqlite3.Connection, data: DepositInput) -> dict:
    resident = conn.execute('SELECT r.id FROM residents r JOIN rooms rm ON rm.id=r.room_id '
                            'WHERE rm.room_code=? AND r.phone=?',
                            (data.room_code, data.recipient_phone)).fetchone()
    if not resident:
        raise BusinessError(404, 'recipient_not_found')
    sizes = ('S', 'M') if data.parcel_size == 'S' else ('M', 'M')
    locker = conn.execute("SELECT * FROM lockers l WHERE size IN (?,?) AND state='AVAILABLE' "
                          'AND online=1 AND NOT EXISTS (SELECT 1 FROM transactions t '
                          'WHERE t.locker_id=l.id AND t.active=1) '
                          "ORDER BY CASE size WHEN 'S' THEN 0 ELSE 1 END, id LIMIT 1", sizes).fetchone()
    if not locker:
        raise BusinessError(409, 'no_suitable_locker')
    parcel_id, tx_id = identifier(), identifier()
    now = time.time()
    conn.execute('INSERT INTO parcels VALUES (?,?,?,?,?)',
                 (parcel_id, resident['id'], data.parcel_size, 'CREATED', now))
    conn.execute('INSERT INTO transactions(id,parcel_id,locker_id,kind,status,active,created_at) '
                 "VALUES (?,?,?,'DEPOSIT','PENDING',1,?)", (tx_id, parcel_id, locker['id'], now))
    transition(conn, 'lockers', locker['id'], 'RESERVED', parcel_id, tx_id, locker['id'])
    log(conn, parcel_id, tx_id, locker['id'], 'PARCEL_CREATED', None, 'CREATED')
    command(conn, tx_id, 'UNLOCK')
    return dict(parcel_id=parcel_id, transaction_id=tx_id, locker_id=locker['id'],
                requested_size=data.parcel_size, allocated_size=locker['size'], status='CREATED')


def owned_parcel(conn: sqlite3.Connection, parcel_id: str, actor: dict) -> sqlite3.Row:
    row = conn.execute('SELECT * FROM parcels WHERE id=?', (parcel_id,)).fetchone()
    if not row:
        raise BusinessError(404, 'parcel_not_found')
    if actor['role'] == 'RESIDENT' and row['resident_id'] != actor['resident_id']:
        raise BusinessError(403, 'not_parcel_owner')
    return row


def maybe_lock(conn: sqlite3.Connection, tx_id: str) -> None:
    tx = conn.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone()
    if (tx['confirmed'] and tx['closed_sequence'] is not None
            and tx['closed_at'] is not None and 0 <= time.time() - tx['closed_at'] <= 10):
        command(conn, tx_id, 'LOCK')


def confirm(conn: sqlite3.Connection, parcel_id: str, kind: str, actor: dict) -> dict:
    owned_parcel(conn, parcel_id, actor)
    tx = conn.execute('SELECT * FROM transactions WHERE parcel_id=? AND kind=? '
                      'ORDER BY created_at DESC LIMIT 1', (parcel_id, kind)).fetchone()
    if not tx or tx['status'] == 'EXCEPTION':
        raise BusinessError(409, 'invalid_state_transition')
    if tx['status'] != 'COMPLETED' and not tx['confirmed']:
        conn.execute('UPDATE transactions SET confirmed=1 WHERE id=?', (tx['id'],))
        log(conn, parcel_id, tx['id'], tx['locker_id'], 'USER_CONFIRMED')
        maybe_lock(conn, tx['id'])
    return {'parcel_id': parcel_id, 'transaction_id': tx['id'],
            'status': conn.execute('SELECT status FROM parcels WHERE id=?', (parcel_id,)).fetchone()[0]}


def create_kiosk_session(conn: sqlite3.Connection, parcel_id: str, kiosk_id: int) -> dict:
    parcel = conn.execute('SELECT * FROM parcels WHERE id=?', (parcel_id,)).fetchone()
    if not parcel:
        raise BusinessError(404, 'parcel_not_found')
    if parcel['status'] != 'STORED':
        raise BusinessError(409, 'invalid_state_transition')
    session_id, code = identifier(), secrets.token_urlsafe(16)
    expiry = time.time() + 30
    conn.execute('INSERT INTO kiosk_sessions(id,code_hash,kiosk_id,parcel_id,resident_id,expires_at) '
                 'VALUES (?,?,?,?,?,?)',
                 (session_id, digest(code), kiosk_id, parcel_id, parcel['resident_id'], expiry))
    return {'session_id': session_id, 'code': code, 'expires_at': expiry, 'ttl_seconds': 30}


def pickup(conn: sqlite3.Connection, parcel_id: str, data: PickupInput, actor: dict) -> dict:
    parcel = owned_parcel(conn, parcel_id, actor)
    if parcel['status'] != 'STORED':
        raise BusinessError(409, 'invalid_state_transition')
    session = conn.execute('SELECT * FROM kiosk_sessions WHERE id=?', (data.session_id,)).fetchone()
    if (not session or session['parcel_id'] != parcel_id or session['resident_id'] != actor['resident_id']
            or session['expires_at'] <= time.time() or session['used'] or session['attempts'] >= 5):
        raise BusinessError(403, 'invalid_kiosk_session')
    if not hmac.compare_digest(session['code_hash'], digest(data.code)):
        # This response is persisted by idempotency and commits the failed attempt.
        conn.execute('UPDATE kiosk_sessions SET attempts=attempts+1 WHERE id=?', (session['id'],))
        return {'error': 'invalid_kiosk_code'}
    previous = conn.execute("SELECT * FROM transactions WHERE parcel_id=? AND kind='DEPOSIT' "
                            "AND status='COMPLETED'", (parcel_id,)).fetchone()
    if not previous:
        raise BusinessError(409, 'invalid_state_transition')
    locker = conn.execute('SELECT * FROM lockers WHERE id=?', (previous['locker_id'],)).fetchone()
    if locker['state'] != 'OCCUPIED' or not locker['online']:
        raise BusinessError(409, 'locker_unavailable')
    tx_id = identifier()
    conn.execute('INSERT INTO transactions(id,parcel_id,locker_id,kind,status,active,created_at) '
                 "VALUES (?,?,?,'PICKUP','PENDING',1,?)", (tx_id, parcel_id, locker['id'], time.time()))
    conn.execute('UPDATE kiosk_sessions SET used=1 WHERE id=?', (session['id'],))
    transition(conn, 'parcels', parcel_id, 'PICKUP_PENDING', parcel_id, tx_id, locker['id'])
    transition(conn, 'lockers', locker['id'], 'COLLECTING', parcel_id, tx_id, locker['id'])
    command(conn, tx_id, 'UNLOCK')
    return {'parcel_id': parcel_id, 'transaction_id': tx_id, 'locker_id': locker['id'],
            'status': 'PICKUP_PENDING'}
