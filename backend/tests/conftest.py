import pytest
from fastapi.testclient import TestClient
from backend.auth import hash_password, issue_token
from backend.db import Database
from backend.main import create_app
from .mock_device import MockDeviceAdapter


@pytest.fixture
def system(tmp_path):
    db = Database(str(tmp_path / 'test.sqlite3'))
    db.initialize()
    password = 'synthetic-test-password'
    hashed = hash_password(password)
    with db.write() as conn:
        for code, phone, username in [('001', '0000000001', 'alice'), ('002', '0000000002', 'bob')]:
            room = conn.execute('INSERT INTO rooms(room_code) VALUES (?)', (code,)).lastrowid
            resident = conn.execute('INSERT INTO residents(room_id,phone) VALUES (?,?)', (room, phone)).lastrowid
            conn.execute('INSERT INTO accounts(username,password_hash,role,resident_id) VALUES (?,?,?,?)',
                         (username, hashed, 'RESIDENT', resident))
        for username, role in [('admin', 'ADMIN'), ('kiosk', 'KIOSK')]:
            conn.execute('INSERT INTO accounts(username,password_hash,role) VALUES (?,?,?)', (username, hashed, role))
        tokens = {row['username']: issue_token(conn, row['id']) for row in conn.execute('SELECT * FROM accounts').fetchall()}
    with TestClient(create_app(db)) as client:
        # Test-only trusted connectivity. There is deliberately no production API for this.
        MockDeviceAdapter.connect(db)
        yield db, client, tokens


def headers(tokens, user='kiosk', request_id='request-1'):
    return {'Authorization': 'Bearer ' + tokens[user], 'X-Request-ID': request_id}


def deposit(client, tokens, size='S', request_id='deposit-1', **overrides):
    payload = {'room_code': '001', 'recipient_phone': '0000000001', 'parcel_size': size}
    payload.update(overrides)
    return client.post('/api/v1/deposits', json=payload, headers=headers(tokens, request_id=request_id))
