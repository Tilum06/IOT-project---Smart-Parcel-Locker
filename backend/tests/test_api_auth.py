import time
from .conftest import deposit, headers
from .test_lifecycle import stored, start_pickup


def test_login_logout_password_storage(system):
    db, client, tokens = system
    response = client.post('/api/v1/auth/login', json={'username': 'alice', 'password': 'synthetic-test-password'})
    assert response.status_code == 200
    token = response.json()['access_token']
    auth = {'Authorization': 'Bearer ' + token}
    assert client.get('/api/v1/parcels', headers=auth).status_code == 200
    assert client.post('/api/v1/auth/logout', headers=auth).status_code == 200
    assert client.get('/api/v1/parcels', headers=auth).status_code == 401
    assert client.post('/api/v1/auth/login', json={'username': 'alice', 'password': 'wrong'}).status_code == 401
    with db.read() as conn:
        assert all(row[0].startswith('pbkdf2_sha256$') for row in conn.execute('SELECT password_hash FROM accounts'))
        assert not conn.execute('SELECT 1 FROM auth_sessions WHERE token_hash=?', (token,)).fetchone()


def test_permissions_and_resource_not_found(system):
    _, client, tokens = system
    item = deposit(client, tokens).json()
    assert client.get('/api/v1/lockers').status_code == 401
    assert client.get('/api/v1/lockers/missing', headers=headers(tokens)).status_code == 404
    assert deposit(client, {'kiosk': tokens['alice']}).status_code == 403
    for path in [f"/api/v1/parcels/{item['parcel_id']}", f"/api/v1/parcels/{item['parcel_id']}/history",
                 f"/api/v1/transactions/{item['transaction_id']}"]:
        assert client.get(path, headers=headers(tokens, 'bob')).status_code == 403
    assert client.get('/api/v1/parcels', headers=headers(tokens, 'bob')).json() == []
    assert client.get('/api/v1/transactions', headers=headers(tokens, 'bob')).json() == []
    assert client.get('/api/v1/parcels/missing', headers=headers(tokens, 'alice')).status_code == 404
    assert client.post('/api/v1/pickups/missing/confirm', headers=headers(tokens, 'alice')).status_code == 404


def test_admin_room_resident_creation_and_idempotency(system):
    db, client, tokens = system
    assert client.post('/api/v1/rooms', json={'room_code': '003'}, headers=headers(tokens, 'alice')).status_code == 403
    h = headers(tokens, 'admin', 'room')
    room = client.post('/api/v1/rooms', json={'room_code': '003'}, headers=h)
    assert room.status_code == 201 and room.json()['room_code'] == '003'
    assert client.post('/api/v1/rooms', json={'room_code': '003'}, headers=h).json() == room.json()
    data = dict(room_code='003', phone='0000000003', username='charlie', password='another-fake-password')
    h = headers(tokens, 'admin', 'resident')
    response = client.post('/api/v1/residents', json=data, headers=h)
    assert response.status_code == 201, response.text
    assert client.post('/api/v1/residents', json=data, headers=h).json() == response.json()
    assert client.post('/api/v1/residents', json={**data, 'password': 'changed-fake-password'}, headers=h).status_code == 409
    assert len(client.get('/api/v1/residents', headers=headers(tokens, 'alice')).json()) == 1
    assert client.get('/api/v1/rooms', headers=headers(tokens, 'alice')).json()[0]['room_code'] == '001'
    with db.read() as conn:
        assert 'another-fake-password' not in str([tuple(r) for r in conn.execute('SELECT * FROM request_records')])


def test_pickup_ownership_ttl_attempt_limit_and_code_one_use(system):
    db, client, tokens = system
    item = stored(system)
    payload = start_pickup(system, item)
    url = f"/api/v1/pickups/{item['parcel_id']}"
    assert client.post(url, json=payload, headers=headers(tokens, 'bob', 'other')).status_code == 403
    for index in range(5):
        assert client.post(url, json={**payload, 'code': 'wrong'}, headers=headers(tokens, 'alice', f'bad-{index}')).status_code == 403
    assert client.post(url, json=payload, headers=headers(tokens, 'alice', 'locked')).status_code == 403
    with db.write() as conn:
        assert conn.execute('SELECT attempts FROM kiosk_sessions').fetchone()[0] == 5
        conn.execute('UPDATE kiosk_sessions SET attempts=0,expires_at=?', (time.time()-1,))
    assert client.post(url, json=payload, headers=headers(tokens, 'alice', 'expired')).status_code == 403
    with db.write() as conn:
        conn.execute('UPDATE kiosk_sessions SET expires_at=?', (time.time()+30,))
    success = client.post(url, json=payload, headers=headers(tokens, 'alice', 'valid'))
    assert success.status_code == 200
    assert client.post(url, json=payload, headers=headers(tokens, 'alice', 'reuse')).status_code == 409
    assert client.post(url, json={**payload, 'code': 'different'}, headers=headers(tokens, 'alice', 'valid')).status_code == 409
    with db.read() as conn:
        assert conn.execute('SELECT used FROM kiosk_sessions').fetchone()[0] == 1
        assert payload['code'] not in str([tuple(r) for r in conn.execute('SELECT * FROM request_records')])


def test_invalid_transition_kiosk_session_duplicate_and_history(system):
    _, client, tokens = system
    pending = deposit(client, tokens).json()
    assert client.post(f"/api/v1/pickups/{pending['parcel_id']}/confirm", headers=headers(tokens, 'alice', 'early')).status_code == 409
    assert client.post('/api/v1/kiosk-sessions', json={'parcel_id': pending['parcel_id']}, headers=headers(tokens)).status_code == 409
    assert len(client.get(f"/api/v1/parcels/{pending['parcel_id']}/history", headers=headers(tokens, 'alice')).json()) >= 3


def test_expired_auth_and_openapi(system):
    db, client, tokens = system
    with db.write() as conn:
        conn.execute('UPDATE auth_sessions SET expires_at=0')
    assert client.get('/api/v1/lockers', headers=headers(tokens)).status_code == 401
    response = client.get('/openapi.json')
    assert response.status_code == 200
    assert '/api/v1/deposits' in response.json()['paths']
    assert client.get('/docs').status_code == 200
