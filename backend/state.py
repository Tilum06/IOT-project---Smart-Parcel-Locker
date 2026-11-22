"""Explicit business state transitions. Physical evidence is checked separately."""
import sqlite3
import time


class BusinessError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code


LOCKER_TRANSITIONS = {
    'AVAILABLE': {'RESERVED', 'UNKNOWN', 'FAULT', 'MAINTENANCE'},
    'RESERVED': {'DEPOSITING', 'UNKNOWN', 'FAULT'},
    'DEPOSITING': {'OCCUPIED', 'UNKNOWN', 'FAULT'},
    'OCCUPIED': {'COLLECTING', 'UNKNOWN', 'FAULT'},
    'COLLECTING': {'AVAILABLE', 'UNKNOWN', 'FAULT'},
    'UNKNOWN': {'MAINTENANCE'}, 'FAULT': {'MAINTENANCE'},
    'MAINTENANCE': {'UNKNOWN'},
}
PARCEL_TRANSITIONS = {
    'CREATED': {'DEPOSITING', 'EXCEPTION', 'CANCELLED'},
    'DEPOSITING': {'STORED', 'EXCEPTION'},
    'STORED': {'PICKUP_PENDING', 'EXCEPTION'},
    'PICKUP_PENDING': {'COMPLETED', 'EXCEPTION'},
    'COMPLETED': set(), 'CANCELLED': set(), 'EXCEPTION': set(),
}


def transition(conn: sqlite3.Connection, table: str, resource_id: str, target: str,
               parcel_id: str, transaction_id: str, locker_id: str) -> None:
    if table not in ('lockers', 'parcels'):
        raise ValueError('Unsupported state table')
    column = 'state' if table == 'lockers' else 'status'
    row = conn.execute(f'SELECT {column} FROM {table} WHERE id=?', (resource_id,)).fetchone()
    if row is None:
        raise BusinessError(404, 'resource_not_found')
    old = row[0]
    allowed = LOCKER_TRANSITIONS if table == 'lockers' else PARCEL_TRANSITIONS
    if target not in allowed[old]:
        raise BusinessError(409, 'invalid_state_transition')
    conn.execute(f'UPDATE {table} SET {column}=? WHERE id=?', (target, resource_id))
    log(conn, parcel_id, transaction_id, locker_id, table, old, target)


def log(conn: sqlite3.Connection, parcel_id: str, transaction_id: str,
        locker_id: str, event: str, old: str | None = None, new: str | None = None) -> None:
    conn.execute('INSERT INTO event_logs(parcel_id,transaction_id,locker_id,event,old_state,new_state,created_at) '
                 'VALUES (?,?,?,?,?,?,?)',
                 (parcel_id, transaction_id, locker_id, event, old, new, time.time()))
