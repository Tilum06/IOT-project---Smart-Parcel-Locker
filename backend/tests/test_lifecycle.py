import time
import pytest
from backend.devices import ingest
from backend.state import BusinessError
from .conftest import deposit, headers
from .mock_device import MockDeviceAdapter


def stored(system):
    db, client, tokens = system
    item = deposit(client, tokens).json()
    device = MockDeviceAdapter(db, item)
    device.cycle()
    assert client.post(f"/api/v1/deposits/{item['parcel_id']}/confirm", headers=headers(tokens, request_id='confirm')).status_code == 200
    device.apply('LOCK')
    return item


def start_pickup(system, item):
    _, client, tokens = system
    response = client.post('/api/v1/kiosk-sessions', json={'parcel_id': item['parcel_id']}, headers=headers(tokens, request_id='session'))
    assert response.status_code == 201, response.text
    session = response.json()
    payload = {key: session[key] for key in ('session_id', 'code')}
    return payload


def test_confirmation_ack_no_physical_success(system):
    db, client, tokens = system
    item = deposit(client, tokens).json()
    device = MockDeviceAdapter(db, item)
    device.emit('ACK')
    url = f"/api/v1/deposits/{item['parcel_id']}/confirm"
    first = client.post(url, headers=headers(tokens, request_id='confirm'))
    assert first.status_code == 200
    assert first.json()['status'] == 'CREATED'
    assert client.post(url, headers=headers(tokens, request_id='confirm')).json() == first.json()
    with db.read() as conn:
        assert conn.execute('SELECT state FROM lockers WHERE id=?', (item['locker_id'],)).fetchone()[0] == 'RESERVED'
        assert conn.execute("SELECT count(*) FROM commands WHERE action='LOCK'").fetchone()[0] == 0


def test_servo_and_fresh_closed_complete_without_latch_sensor(system):
    db, client, tokens = system
    item = deposit(client, tokens).json()
    device = MockDeviceAdapter(db, item)
    device.cycle()
    client.post(f"/api/v1/deposits/{item['parcel_id']}/confirm", headers=headers(tokens, request_id='confirm'))
    device.apply('LOCK')
    assert client.get(f"/api/v1/parcels/{item['parcel_id']}", headers=headers(tokens, 'alice')).json()['status'] == 'STORED'


def test_complete_deposit_and_pickup(system):
    db, client, tokens = system
    item = stored(system)
    payload = start_pickup(system, item)
    url = f"/api/v1/pickups/{item['parcel_id']}"
    response = client.post(url, json=payload, headers=headers(tokens, 'alice', 'pickup'))
    assert response.status_code == 200, response.text
    assert client.post(url, json=payload, headers=headers(tokens, 'alice', 'pickup')).json() == response.json()
    device = MockDeviceAdapter(db, response.json())
    device.cycle()
    client.post(url + '/confirm', headers=headers(tokens, 'alice', 'pickup-confirm'))
    with db.read() as conn:
        assert conn.execute('SELECT state FROM lockers WHERE id=?', (item['locker_id'],)).fetchone()[0] == 'COLLECTING'
    device.apply('LOCK')
    assert client.get(f"/api/v1/parcels/{item['parcel_id']}", headers=headers(tokens, 'alice')).json()['status'] == 'COMPLETED'
    with db.read() as conn:
        assert conn.execute('SELECT state FROM lockers WHERE id=?', (item['locker_id'],)).fetchone()[0] == 'AVAILABLE'
        assert conn.execute('SELECT count(*) FROM transactions WHERE active=1').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['ERROR', 'OFFLINE'])
def test_error_never_releases(system, kind):
    db, client, tokens = system
    item = deposit(client, tokens).json()
    MockDeviceAdapter(db, item).emit(kind)
    with db.read() as conn:
        assert conn.execute('SELECT state FROM lockers WHERE id=?', (item['locker_id'],)).fetchone()[0] == 'UNKNOWN'
        assert conn.execute('SELECT active FROM transactions WHERE id=?', (item['transaction_id'],)).fetchone()[0] == 1


def test_missing_open_stale_out_of_order_and_event_dedup(system):
    db, client, tokens = system
    item = deposit(client, tokens).json()
    device = MockDeviceAdapter(db, item)
    device.apply('UNLOCK')
    with pytest.raises(BusinessError):
        device.emit('DOOR_CLOSED')
    event = device.emit('DOOR_OPEN')
    ingest(db, event)
    from dataclasses import replace
    for changed in [replace(event, kind='DOOR_CLOSED'),
                    replace(event, event_id='stale', observed_at=time.time()-31),
                    replace(event, event_id='order', sequence=0)]:
        with pytest.raises(BusinessError):
            ingest(db, changed)
    with pytest.raises(BusinessError):
        ingest(db, replace(event, event_id='wrong-session', transaction_id='missing'))


def test_boot_change_fail_safe(system):
    from dataclasses import replace
    db, client, tokens = system
    item = deposit(client, tokens).json()
    device = MockDeviceAdapter(db, item)
    device.apply('UNLOCK')
    event = device.emit('DOOR_OPEN')
    ingest(db, replace(event, event_id='reboot', boot_id='new-boot', sequence=0, observed_at=time.time()))
    with db.read() as conn:
        assert conn.execute('SELECT status FROM parcels').fetchone()[0] == 'EXCEPTION'
        assert conn.execute('SELECT active FROM transactions').fetchone()[0] == 1


def test_restart_keeps_reservation(system):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    db, client, tokens = system
    item = deposit(client, tokens).json()
    with TestClient(create_app(db)) as restarted:
        row = restarted.get('/api/v1/lockers/' + item['locker_id'], headers=headers(tokens)).json()
        assert row['state'] == 'RESERVED' and row['online'] == 0
        assert deposit(restarted, tokens, request_id='after-restart').status_code == 409


def test_no_production_device_success_route(system):
    _, client, _ = system
    paths = client.get('/openapi.json').json()['paths']
    assert all('mock' not in path and 'device' not in path for path in paths)


@pytest.mark.parametrize('snapshot', ['DOOR_CLOSED', 'SENSOR'])
def test_no_lock_from_old_closed_confirmation(system, snapshot):
    db, client, tokens = system
    item = deposit(client, tokens).json()
    device = MockDeviceAdapter(db, item)
    device.cycle()
    with db.write() as conn:
        conn.execute('UPDATE transactions SET closed_at=?', (time.time()-11,))
    client.post(f"/api/v1/deposits/{item['parcel_id']}/confirm", headers=headers(tokens, request_id='late'))
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM commands WHERE action='LOCK'").fetchone()[0] == 0
    device.emit(snapshot, door='CLOSED')
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM commands WHERE action='LOCK'").fetchone()[0] == 1


def test_reopened_door_after_lock_intent_cannot_complete(system):
    db, client, tokens = system
    item = deposit(client, tokens).json()
    device = MockDeviceAdapter(db, item)
    device.cycle()
    with db.read() as conn:
        assert conn.execute('SELECT status FROM parcels').fetchone()[0] == 'DEPOSITING'
    client.post(f"/api/v1/deposits/{item['parcel_id']}/confirm", headers=headers(tokens, request_id='confirmed'))
    device.emit('DOOR_OPEN')
    with db.read() as conn:
        assert conn.execute('SELECT status FROM parcels').fetchone()[0] == 'EXCEPTION'
        assert conn.execute('SELECT active FROM transactions').fetchone()[0] == 1


def test_pending_adapter_does_not_generate_evidence(system):
    from backend.devices import dispatch_pending, PendingDeviceAdapter
    db, client, tokens = system
    deposit(client, tokens)
    dispatch_pending(db, PendingDeviceAdapter())
    with db.read() as conn:
        assert conn.execute('SELECT status FROM parcels').fetchone()[0] == 'CREATED'
        assert conn.execute('SELECT status FROM commands').fetchone()[0] == 'PENDING'
        assert conn.execute('SELECT count(*) FROM device_events').fetchone()[0] == 0


def pending_phase(system, kind):
    if kind == 'DEPOSIT':
        _, client, tokens = system
        return deposit(client, tokens).json(), 'kiosk', 'deposits'
    item = stored(system)
    payload = start_pickup(system, item)
    _, client, tokens = system
    result = client.post(f"/api/v1/pickups/{item['parcel_id']}", json=payload,
                         headers=headers(tokens, 'alice', 'begin-pickup'))
    assert result.status_code == 200
    return result.json(), 'alice', 'pickups'


@pytest.mark.parametrize('kind', ['DEPOSIT', 'PICKUP'])
@pytest.mark.parametrize('missing', ['confirmation', 'cycle', 'lock_result'])
def test_each_required_evidence_is_independent(system, kind, missing):
    db, client, tokens = system
    item, user, group = pending_phase(system, kind)
    device = MockDeviceAdapter(db, item)
    device.apply('UNLOCK')
    if missing != 'cycle':
        device.emit('DOOR_OPEN')
        device.emit('DOOR_CLOSED')
    if missing != 'confirmation':
        response = client.post(f"/api/v1/{group}/{item['parcel_id']}/confirm",
                               headers=headers(tokens, user, 'phase-confirm'))
        assert response.status_code == 200
    device.emit('SENSOR', door='CLOSED')
    with db.read() as conn:
        row = conn.execute('SELECT * FROM transactions WHERE id=?', (item['transaction_id'],)).fetchone()
        assert row['status'] == 'PENDING' and row['active'] == 1
        assert row['locked_sequence'] is None


@pytest.mark.parametrize('kind', ['DEPOSIT', 'PICKUP'])
@pytest.mark.parametrize('door', ['OPEN', 'UNKNOWN'])
def test_invalid_sensor_snapshot_cannot_complete(system, kind, door):
    db, client, tokens = system
    item, user, group = pending_phase(system, kind)
    device = MockDeviceAdapter(db, item)
    device.cycle()
    client.post(f"/api/v1/{group}/{item['parcel_id']}/confirm", headers=headers(tokens, user, 'phase-confirm'))
    device.emit('SENSOR', door=door)
    with db.read() as conn:
        assert conn.execute('SELECT status FROM transactions WHERE id=?', (item['transaction_id'],)).fetchone()[0] == 'EXCEPTION'
        assert conn.execute('SELECT state FROM lockers WHERE id=?', (item['locker_id'],)).fetchone()[0] == 'UNKNOWN'


@pytest.mark.parametrize('kind', ['DEPOSIT', 'PICKUP'])
@pytest.mark.parametrize('snapshot', ['SENSOR', 'DOOR_CLOSED'])
def test_stale_closed_cannot_complete_until_fresh_snapshot(system, kind, snapshot):
    db, client, tokens = system
    item, user, group = pending_phase(system, kind)
    device = MockDeviceAdapter(db, item)
    device.cycle()
    client.post(f"/api/v1/{group}/{item['parcel_id']}/confirm", headers=headers(tokens, user, 'phase-confirm'))
    with db.write() as conn:
        conn.execute('UPDATE transactions SET closed_at=? WHERE id=?', (time.time()-11, item['transaction_id']))
    device.apply('LOCK')
    with db.read() as conn:
        assert conn.execute('SELECT status FROM transactions WHERE id=?', (item['transaction_id'],)).fetchone()[0] == 'PENDING'
    device.emit(snapshot, door='CLOSED')
    with db.read() as conn:
        assert conn.execute('SELECT status FROM transactions WHERE id=?', (item['transaction_id'],)).fetchone()[0] == 'COMPLETED'


@pytest.mark.parametrize('kind', ['DEPOSIT', 'PICKUP'])
def test_offline_blocks_completion(system, kind):
    db, client, tokens = system
    item, user, group = pending_phase(system, kind)
    device = MockDeviceAdapter(db, item)
    device.cycle()
    client.post(f"/api/v1/{group}/{item['parcel_id']}/confirm", headers=headers(tokens, user, 'phase-confirm'))
    device.emit('OFFLINE')
    with pytest.raises(BusinessError):
        device.apply('LOCK')
    with db.read() as conn:
        assert conn.execute('SELECT active FROM transactions WHERE id=?', (item['transaction_id'],)).fetchone()[0] == 1
