"""One FastAPI PC application; gateway integration remains in-process."""
import sqlite3
import time
from typing import Annotated, Any
from collections.abc import Callable
from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .auth import digest, hash_password, issue_token, verify_password
from .db import Database
from .schemas import (AllocationOutput, DepositInput, KioskSessionInput, Login,
                      PickupInput, ResidentInput, RoomInput)
from .services import (allocate, confirm, create_kiosk_session, idempotent,
                       owned_parcel, pickup)
from .state import BusinessError

bearer = HTTPBearer(auto_error=False)
RequestID = Annotated[str, Header(alias='X-Request-ID', min_length=1, max_length=128)]


def create_router(db: Database) -> APIRouter:
    router = APIRouter()

    def account(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> dict:
        if not credentials:
            raise BusinessError(401, 'authentication_required')
        with db.read() as conn:
            row = conn.execute('SELECT a.* FROM accounts a JOIN auth_sessions s ON s.account_id=a.id '
                               'WHERE s.token_hash=? AND s.expires_at>?',
                               (digest(credentials.credentials), time.time())).fetchone()
        if not row:
            raise BusinessError(401, 'invalid_or_expired_token')
        return dict(row)

    def roles(*allowed: str) -> Callable[[dict], dict]:
        def check(actor: Annotated[dict, Depends(account)]) -> dict:
            if actor['role'] not in allowed:
                raise BusinessError(403, 'role_forbidden')
            return actor
        return check

    Admin = Annotated[dict, Depends(roles('ADMIN'))]
    Operator = Annotated[dict, Depends(roles('ADMIN', 'KIOSK'))]
    Reader = Annotated[dict, Depends(roles('ADMIN', 'RESIDENT'))]
    Resident = Annotated[dict, Depends(roles('RESIDENT'))]

    @router.post('/api/v1/auth/login')
    def login(data: Login) -> dict:
        with db.write() as conn:
            row = conn.execute('SELECT * FROM accounts WHERE username=?', (data.username,)).fetchone()
            # Dummy hash work also runs for unknown usernames.
            stored = row['password_hash'] if row else hash_password('unavailable-account')
            if not verify_password(data.password, stored) or not row:
                raise BusinessError(401, 'invalid_credentials')
            return {'access_token': issue_token(conn, row['id']), 'token_type': 'bearer', 'expires_in': 3600}

    @router.post('/api/v1/auth/logout')
    def logout(actor: Annotated[dict, Depends(account)], credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)]) -> dict:
        with db.write() as conn:
            conn.execute('DELETE FROM auth_sessions WHERE token_hash=?', (digest(credentials.credentials),))
        return {'status': 'logged_out'}

    @router.post('/api/v1/rooms', status_code=201)
    def add_room(data: RoomInput, actor: Admin, request_id: RequestID) -> dict:
        with db.write() as conn:
            def action() -> dict:
                cursor = conn.execute('INSERT INTO rooms(room_code) VALUES (?)', (data.room_code,))
                return {'id': cursor.lastrowid, 'room_code': data.room_code}
            return idempotent(conn, actor['id'], request_id, 'create_room', data.model_dump(), action)

    @router.get('/api/v1/rooms')
    def rooms(actor: Reader) -> list[dict]:
        with db.read() as conn:
            if actor['role'] == 'ADMIN':
                rows = conn.execute('SELECT * FROM rooms ORDER BY room_code').fetchall()
            else:
                rows = conn.execute('SELECT rm.* FROM rooms rm JOIN residents r ON r.room_id=rm.id WHERE r.id=?',
                                    (actor['resident_id'],)).fetchall()
            return [dict(row) for row in rows]

    @router.post('/api/v1/residents', status_code=201)
    def add_resident(data: ResidentInput, actor: Admin, request_id: RequestID) -> dict:
        with db.write() as conn:
            def action() -> dict:
                room = conn.execute('SELECT id FROM rooms WHERE room_code=?', (data.room_code,)).fetchone()
                if not room:
                    raise BusinessError(404, 'room_not_found')
                cursor = conn.execute('INSERT INTO residents(room_id,phone) VALUES (?,?)', (room['id'], data.phone))
                resident_id = cursor.lastrowid
                conn.execute('INSERT INTO accounts(username,password_hash,role,resident_id) VALUES (?,?,?,?)',
                             (data.username, hash_password(data.password), 'RESIDENT', resident_id))
                return {'id': resident_id, 'room_code': data.room_code, 'phone': data.phone, 'username': data.username}
            # Store a fingerprint, never a password or a fast unsalted password hash in dedup records.
            payload = data.model_dump(exclude={'password'})
            existing = conn.execute('SELECT password_hash FROM accounts WHERE username=?', (data.username,)).fetchone()
            if existing and not verify_password(data.password, existing[0]):
                raise BusinessError(409, 'resource_conflict')
            return idempotent(conn, actor['id'], request_id, 'create_resident', payload, action)

    @router.get('/api/v1/residents')
    def residents(actor: Reader) -> list[dict]:
        with db.read() as conn:
            query = 'SELECT r.id,rm.room_code,r.phone,a.username FROM residents r JOIN rooms rm ON rm.id=r.room_id JOIN accounts a ON a.resident_id=r.id'
            rows = conn.execute(query + (' WHERE r.id=?' if actor['role'] == 'RESIDENT' else ''),
                                (actor['resident_id'],) if actor['role'] == 'RESIDENT' else ()).fetchall()
            return [dict(row) for row in rows]

    @router.get('/api/v1/lockers')
    def lockers(actor: Annotated[dict, Depends(account)]) -> list[dict]:
        with db.read() as conn:
            return [dict(row) for row in conn.execute('SELECT id,size,state,online,node_id FROM lockers ORDER BY id')]

    @router.get('/api/v1/lockers/{locker_id}')
    def locker(locker_id: str, actor: Annotated[dict, Depends(account)]) -> dict:
        with db.read() as conn:
            row = conn.execute('SELECT id,size,state,online,node_id FROM lockers WHERE id=?', (locker_id,)).fetchone()
            if not row:
                raise BusinessError(404, 'locker_not_found')
            return dict(row)

    @router.post('/api/v1/deposits', response_model=AllocationOutput, status_code=201)
    def deposit(data: DepositInput, actor: Operator, request_id: RequestID) -> dict:
        with db.write() as conn:
            return idempotent(conn, actor['id'], request_id, 'deposit', data.model_dump(), lambda: allocate(conn, data))

    @router.post('/api/v1/deposits/{parcel_id}/confirm')
    def confirm_deposit(parcel_id: str, actor: Operator, request_id: RequestID) -> dict:
        with db.write() as conn:
            return idempotent(conn, actor['id'], request_id, 'confirm_deposit', {'parcel_id': parcel_id},
                              lambda: confirm(conn, parcel_id, 'DEPOSIT', actor))

    @router.post('/api/v1/kiosk-sessions', status_code=201)
    def kiosk_session(data: KioskSessionInput, actor: Operator, request_id: RequestID) -> dict:
        with db.write() as conn:
            # Code is returned once, not persisted in request_records response.
            previous = conn.execute('SELECT 1 FROM request_records WHERE actor_id=? AND request_id=?',
                                    (actor['id'], request_id)).fetchone()
            if previous:
                idempotent(conn, actor['id'], request_id, 'kiosk_session', data.model_dump(), lambda: {})
                raise BusinessError(409, 'kiosk_code_already_issued')
            result = create_kiosk_session(conn, data.parcel_id, actor['id'])
            idempotent(conn, actor['id'], request_id, 'kiosk_session', data.model_dump(),
                       lambda: {key: value for key, value in result.items() if key != 'code'})
            return result

    @router.post('/api/v1/pickups/{parcel_id}')
    def request_pickup(parcel_id: str, data: PickupInput, actor: Resident, request_id: RequestID) -> Any:
        with db.write() as conn:
            # The high-entropy kiosk code is hashed before saving the payload fingerprint.
            payload = {'parcel_id': parcel_id, 'session_id': data.session_id, 'code_hash': digest(data.code)}
            result = idempotent(conn, actor['id'], request_id, 'pickup', payload,
                                lambda: pickup(conn, parcel_id, data, actor))
        if result.get('error'):
            return JSONResponse(status_code=403, content={'detail': result['error']})
        return result

    @router.post('/api/v1/pickups/{parcel_id}/confirm')
    def confirm_pickup(parcel_id: str, actor: Resident, request_id: RequestID) -> dict:
        with db.write() as conn:
            return idempotent(conn, actor['id'], request_id, 'confirm_pickup', {'parcel_id': parcel_id},
                              lambda: confirm(conn, parcel_id, 'PICKUP', actor))

    @router.get('/api/v1/parcels')
    def parcels(actor: Reader) -> list[dict]:
        with db.read() as conn:
            rows = conn.execute('SELECT * FROM parcels' + (' WHERE resident_id=?' if actor['role'] == 'RESIDENT' else '') + ' ORDER BY created_at,id',
                                (actor['resident_id'],) if actor['role'] == 'RESIDENT' else ()).fetchall()
            return [dict(row) for row in rows]

    @router.get('/api/v1/parcels/{parcel_id}')
    def parcel(parcel_id: str, actor: Reader) -> dict:
        with db.read() as conn:
            return dict(owned_parcel(conn, parcel_id, actor))

    @router.get('/api/v1/parcels/{parcel_id}/history')
    def history(parcel_id: str, actor: Reader) -> list[dict]:
        with db.read() as conn:
            owned_parcel(conn, parcel_id, actor)
            return [dict(row) for row in conn.execute('SELECT * FROM event_logs WHERE parcel_id=? ORDER BY id', (parcel_id,))]

    @router.get('/api/v1/transactions')
    def transactions(actor: Reader) -> list[dict]:
        with db.read() as conn:
            query = 'SELECT t.* FROM transactions t JOIN parcels p ON p.id=t.parcel_id'
            return [dict(row) for row in conn.execute(query + (' WHERE p.resident_id=?' if actor['role'] == 'RESIDENT' else '') + ' ORDER BY t.created_at,t.id',
                       (actor['resident_id'],) if actor['role'] == 'RESIDENT' else ())]

    @router.get('/api/v1/transactions/{transaction_id}')
    def transaction(transaction_id: str, actor: Reader) -> dict:
        with db.read() as conn:
            row = conn.execute('SELECT * FROM transactions WHERE id=?', (transaction_id,)).fetchone()
            if not row:
                raise BusinessError(404, 'transaction_not_found')
            owned_parcel(conn, row['parcel_id'], actor)
            return dict(row)

    return router
