"""Run from repository root: python -m backend.manage --help."""
import argparse
import getpass
import os
from .auth import hash_password
from .db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description='Phase 1 database administration')
    parser.add_argument('--db', default=os.getenv('LOCKER_DB_PATH', 'backend/data/locker.sqlite3'))
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('init')
    account = sub.add_parser('create-account')
    account.add_argument('--username', required=True)
    account.add_argument('--role', choices=['ADMIN', 'KIOSK'], required=True)
    sub.add_parser('demo', help='Create fake room 001 and resident; asks for a password')
    args = parser.parse_args()
    db = Database(args.db)
    db.initialize()
    if args.action == 'init':
        print('Schema v1 initialized; L01=S and L02=M are offline until trusted integration.')
        return
    password = getpass.getpass('Password (at least 12 characters): ')
    if len(password) < 12 or len(password) > 128:
        parser.error('Password must be 12–128 characters')
    if password != getpass.getpass('Repeat password: '):
        parser.error('Passwords differ')
    with db.write() as conn:
        if args.action == 'create-account':
            conn.execute('INSERT INTO accounts(username,password_hash,role) VALUES (?,?,?)',
                         (args.username, hash_password(password), args.role))
        else:
            conn.execute("INSERT OR IGNORE INTO rooms(room_code) VALUES ('001')")
            room_id = conn.execute("SELECT id FROM rooms WHERE room_code='001'").fetchone()[0]
            cursor = conn.execute('INSERT INTO residents(room_id,phone) VALUES (?,?)', (room_id, '0000000001'))
            conn.execute('INSERT INTO accounts(username,password_hash,role,resident_id) VALUES (?,?,?,?)',
                         ('demo-resident', hash_password(password), 'RESIDENT', cursor.lastrowid))
    print('Created account. Demo phone is synthetic; lockers remain offline.')


if __name__ == '__main__':
    main()
