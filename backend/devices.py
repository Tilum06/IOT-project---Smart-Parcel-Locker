"""Internal gateway boundary. No HTTP endpoint can supply physical evidence."""
from dataclasses import asdict, dataclass
from typing import Literal, Protocol
import sqlite3
import time
from .db import Database
from .services import maybe_lock, payload_hash
from .state import BusinessError, log, transition


class DeviceAdapter(Protocol):
    def send(self, command_id: str, transaction_id: str, locker_id: str, action: str) -> None: ...


class PendingDeviceAdapter:
    def send(self, command_id: str, transaction_id: str, locker_id: str, action: str) -> None:
        """Hardware absent: retain durable PENDING intent, never fabricate success."""


@dataclass(frozen=True)
class DeviceEvent:
    event_id: str
    transaction_id: str
    locker_id: str
    boot_id: str
    sequence: int
    observed_at: float
    kind: Literal['ACK', 'UNLOCK_APPLIED', 'LOCK_APPLIED', 'DOOR_OPEN', 'DOOR_CLOSED',
                  'SENSOR', 'ERROR', 'OFFLINE']
    command_id: str | None = None
    door: Literal['OPEN', 'CLOSED', 'UNKNOWN'] = 'UNKNOWN'


def fail_safe(conn: sqlite3.Connection, tx: sqlite3.Row) -> None:
    locker_id, parcel_id, tx_id = tx['locker_id'], tx['parcel_id'], tx['id']
    if conn.execute('SELECT state FROM lockers WHERE id=?', (locker_id,)).fetchone()[0] != 'UNKNOWN':
        transition(conn, 'lockers', locker_id, 'UNKNOWN', parcel_id, tx_id, locker_id)
    if conn.execute('SELECT status FROM parcels WHERE id=?', (parcel_id,)).fetchone()[0] != 'EXCEPTION':
        transition(conn, 'parcels', parcel_id, 'EXCEPTION', parcel_id, tx_id, locker_id)
    conn.execute("UPDATE transactions SET status='EXCEPTION' WHERE id=?", (tx_id,))
    conn.execute('UPDATE lockers SET online=0 WHERE id=?', (locker_id,))


def complete_if_ready(conn: sqlite3.Connection, tx_id: str) -> None:
    """LOCK_APPLIED proves actuation only, never physical latch engagement."""
    tx = conn.execute('SELECT * FROM transactions WHERE id=?', (tx_id,)).fetchone()
    locker = conn.execute('SELECT * FROM lockers WHERE id=?', (tx['locker_id'],)).fetchone()
    expected = 'DEPOSITING' if tx['kind'] == 'DEPOSIT' else 'COLLECTING'
    if (tx['status'] != 'PENDING' or locker['state'] != expected or not locker['online']
            or not tx['boot_id'] or tx['boot_id'] != locker['boot_id'] or not tx['confirmed']
            or tx['opened_sequence'] is None or tx['closed_sequence'] is None
            or tx['locked_sequence'] is None or tx['closed_at'] is None
            or not 0 <= time.time() - tx['closed_at'] <= 10):
        return
    target = 'STORED' if tx['kind'] == 'DEPOSIT' else 'COMPLETED'
    state = 'OCCUPIED' if tx['kind'] == 'DEPOSIT' else 'AVAILABLE'
    transition(conn, 'parcels', tx['parcel_id'], target, tx['parcel_id'], tx['id'], tx['locker_id'])
    transition(conn, 'lockers', tx['locker_id'], state, tx['parcel_id'], tx['id'], tx['locker_id'])
    conn.execute("UPDATE transactions SET status='COMPLETED',active=0 WHERE id=?", (tx['id'],))


def ingest(db: Database, event: DeviceEvent) -> None:
    """Called only by trusted in-process adapter. Fresh ordered evidence of one boot/session."""
    with db.write() as conn:
        hashed = payload_hash(asdict(event))
        old = conn.execute('SELECT payload_hash FROM device_events WHERE event_id=?',
                           (event.event_id,)).fetchone()
        if old:
            if old[0] != hashed:
                raise BusinessError(409, 'device_event_payload_conflict')
            return
        tx = conn.execute('SELECT * FROM transactions WHERE id=?', (event.transaction_id,)).fetchone()
        if not tx or tx['locker_id'] != event.locker_id:
            raise BusinessError(404, 'device_transaction_not_found')
        if tx['status'] != 'PENDING':
            raise BusinessError(409, 'transaction_not_pending')
        now = time.time()
        if not event.boot_id or not 0 <= now - event.observed_at <= 10 or event.observed_at < tx['created_at']:
            raise BusinessError(409, 'stale_device_event')
        locker = conn.execute('SELECT * FROM lockers WHERE id=?', (event.locker_id,)).fetchone()
        if ((tx['boot_id'] and tx['boot_id'] != event.boot_id)
                or (locker['boot_id'] and locker['boot_id'] != event.boot_id)):
            fail_safe(conn, tx)
            return
        if event.sequence <= locker['last_sequence'] or event.observed_at < locker['last_observed_at']:
            raise BusinessError(409, 'out_of_order_device_event')
        conn.execute('INSERT INTO device_events VALUES (?,?,?,?)',
                     (event.event_id, hashed, tx['id'], now))
        conn.execute('UPDATE lockers SET boot_id=?,last_sequence=?,last_observed_at=? WHERE id=?',
                     (event.boot_id, event.sequence, event.observed_at, event.locker_id))
        log(conn, tx['parcel_id'], tx['id'], event.locker_id, event.kind)
        if event.kind in ('ERROR', 'OFFLINE'):
            if event.kind == 'ERROR' and event.command_id:
                conn.execute("UPDATE commands SET status='ERROR' WHERE id=? AND transaction_id=?",
                             (event.command_id, tx['id']))
            fail_safe(conn, tx)
            return
        if event.kind == 'ACK':
            return
        if event.kind in ('UNLOCK_APPLIED', 'LOCK_APPLIED'):
            action = 'UNLOCK' if event.kind == 'UNLOCK_APPLIED' else 'LOCK'
            cmd = conn.execute('SELECT * FROM commands WHERE id=? AND transaction_id=? AND action=?',
                               (event.command_id, tx['id'], action)).fetchone()
            if not cmd or cmd['status'] != 'PENDING' or event.observed_at < cmd['created_at']:
                raise BusinessError(409, 'invalid_command_result')
            conn.execute("UPDATE commands SET status='APPLIED' WHERE id=?", (cmd['id'],))
            if action == 'UNLOCK':
                conn.execute('UPDATE transactions SET boot_id=? WHERE id=?', (event.boot_id, tx['id']))
                if tx['kind'] == 'DEPOSIT':
                    transition(conn, 'lockers', event.locker_id, 'DEPOSITING', tx['parcel_id'], tx['id'], event.locker_id)
                    transition(conn, 'parcels', tx['parcel_id'], 'DEPOSITING', tx['parcel_id'], tx['id'], event.locker_id)
            else:
                if tx['closed_sequence'] is None or event.door != 'CLOSED':
                    raise BusinessError(409, 'lock_requires_closed_door')
                conn.execute('UPDATE transactions SET locked_sequence=? WHERE id=?', (event.sequence, tx['id']))
                complete_if_ready(conn, tx['id'])
            return
        if not tx['boot_id']:
            raise BusinessError(409, 'unlock_not_applied')
        if event.kind == 'DOOR_OPEN':
            conn.execute('UPDATE transactions SET opened_sequence=?,closed_sequence=NULL,closed_at=NULL,locked_sequence=NULL WHERE id=?',
                         (event.sequence, tx['id']))
            # Opening again after LOCK intent is unsafe: require manual reconciliation.
            if conn.execute("SELECT 1 FROM commands WHERE transaction_id=? AND action='LOCK'", (tx['id'],)).fetchone():
                fail_safe(conn, tx)
        elif event.kind == 'DOOR_CLOSED':
            if tx['opened_sequence'] is None:
                raise BusinessError(409, 'missing_door_open')
            conn.execute('UPDATE transactions SET closed_sequence=?,closed_at=? WHERE id=?',
                         (event.sequence, event.observed_at, tx['id']))
            maybe_lock(conn, tx['id'])
            complete_if_ready(conn, tx['id'])
        elif event.kind == 'SENSOR':
            if event.door != 'CLOSED':
                fail_safe(conn, tx)
                return
            if tx['closed_sequence'] is not None:
                conn.execute('UPDATE transactions SET closed_at=? WHERE id=?', (event.observed_at, tx['id']))
                maybe_lock(conn, tx['id'])
                complete_if_ready(conn, tx['id'])


def dispatch_pending(db: Database, adapter: DeviceAdapter) -> None:
    """Explicit future gateway hook; never automatically replay UNLOCK at startup."""
    with db.read() as conn:
        pending = conn.execute("SELECT c.*,t.locker_id FROM commands c JOIN transactions t "
                               "ON t.id=c.transaction_id WHERE c.status='PENDING' AND t.status='PENDING'").fetchall()
    for item in pending:
        adapter.send(item['id'], item['transaction_id'], item['locker_id'], item['action'])
