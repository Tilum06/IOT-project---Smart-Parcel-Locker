"""SQLite connections and forward-only, transactional schema migrations."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator

SCHEMA = """
CREATE TABLE rooms (
 id INTEGER PRIMARY KEY, room_code TEXT NOT NULL UNIQUE
 CHECK(length(room_code)>0 AND room_code NOT GLOB '*[^0-9]*'));
CREATE TABLE residents (
 id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
 phone TEXT NOT NULL, UNIQUE(room_id, phone));
CREATE TABLE accounts (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('ADMIN','RESIDENT','KIOSK')),
 resident_id INTEGER UNIQUE REFERENCES residents(id),
 CHECK((role='RESIDENT' AND resident_id IS NOT NULL) OR
       (role!='RESIDENT' AND resident_id IS NULL)));
CREATE TABLE auth_sessions (
 token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
 expires_at REAL NOT NULL);
CREATE TABLE lockers (
 id TEXT PRIMARY KEY, size TEXT NOT NULL CHECK(size IN ('S','M')),
 state TEXT NOT NULL CHECK(state IN ('AVAILABLE','RESERVED','DEPOSITING','OCCUPIED',
 'COLLECTING','UNKNOWN','FAULT','MAINTENANCE')),
 online INTEGER NOT NULL CHECK(online IN (0,1)), node_id INTEGER NOT NULL,
 boot_id TEXT, last_sequence INTEGER NOT NULL DEFAULT -1,
 last_observed_at REAL NOT NULL DEFAULT 0);
CREATE TABLE parcels (
 id TEXT PRIMARY KEY, resident_id INTEGER NOT NULL REFERENCES residents(id),
 requested_size TEXT NOT NULL CHECK(requested_size IN ('S','M')),
 status TEXT NOT NULL CHECK(status IN ('CREATED','DEPOSITING','STORED',
 'PICKUP_PENDING','COMPLETED','CANCELLED','EXCEPTION')), created_at REAL NOT NULL);
CREATE TABLE transactions (
 id TEXT PRIMARY KEY, parcel_id TEXT NOT NULL REFERENCES parcels(id),
 locker_id TEXT NOT NULL REFERENCES lockers(id),
 kind TEXT NOT NULL CHECK(kind IN ('DEPOSIT','PICKUP')),
 status TEXT NOT NULL CHECK(status IN ('PENDING','COMPLETED','EXCEPTION')),
 active INTEGER NOT NULL CHECK(active IN (0,1)), confirmed INTEGER NOT NULL DEFAULT 0
 CHECK(confirmed IN (0,1)), created_at REAL NOT NULL, boot_id TEXT,
 opened_sequence INTEGER, closed_sequence INTEGER, closed_at REAL, locked_sequence INTEGER,
 CHECK((status='COMPLETED' AND active=0) OR (status!='COMPLETED' AND active=1)));
CREATE UNIQUE INDEX one_active_locker ON transactions(locker_id) WHERE active=1;
CREATE UNIQUE INDEX one_active_parcel ON transactions(parcel_id) WHERE active=1;
CREATE TABLE commands (
 id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL REFERENCES transactions(id),
 action TEXT NOT NULL CHECK(action IN ('UNLOCK','LOCK')),
 status TEXT NOT NULL CHECK(status IN ('PENDING','APPLIED','ERROR')),
 created_at REAL NOT NULL, UNIQUE(transaction_id,action));
CREATE TABLE event_logs (
 id INTEGER PRIMARY KEY, parcel_id TEXT REFERENCES parcels(id),
 transaction_id TEXT REFERENCES transactions(id), locker_id TEXT REFERENCES lockers(id),
 event TEXT NOT NULL, old_state TEXT, new_state TEXT, created_at REAL NOT NULL);
CREATE TABLE device_events (
 event_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
 transaction_id TEXT NOT NULL REFERENCES transactions(id), received_at REAL NOT NULL);
CREATE TABLE request_records (
 actor_id INTEGER NOT NULL REFERENCES accounts(id), request_id TEXT NOT NULL,
 operation TEXT NOT NULL, payload_hash TEXT NOT NULL, response TEXT NOT NULL,
 PRIMARY KEY(actor_id,request_id));
CREATE TABLE kiosk_sessions (
 id TEXT PRIMARY KEY, code_hash TEXT NOT NULL, kiosk_id INTEGER NOT NULL REFERENCES accounts(id),
 parcel_id TEXT NOT NULL REFERENCES parcels(id), resident_id INTEGER NOT NULL REFERENCES residents(id),
 expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0,1)),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0));
INSERT INTO lockers(id,size,state,online,node_id) VALUES
 ('L01','S','AVAILABLE',0,2), ('L02','M','AVAILABLE',0,2);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=15000')
        return conn

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.write() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise RuntimeError('Database schema is newer than this application')
            if version == 0:
                # execute individually: executescript would implicitly commit BEGIN.
                for statement in SCHEMA.split(';'):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute('PRAGMA user_version=1')

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()
