import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest
from backend.db import Database
from backend.schemas import DepositInput
from backend.services import allocate
from backend.state import BusinessError, transition
from .conftest import deposit, headers


def test_initialize_and_foreign_keys(tmp_path):
    db = Database(str(tmp_path / 'nested' / 'init.db'))
    db.initialize()
    db.initialize()
    with db.read() as conn:
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 1
        assert [tuple(row) for row in conn.execute('SELECT id,size,online FROM lockers ORDER BY id')] == [('L01', 'S', 0), ('L02', 'M', 0)]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO residents(room_id,phone) VALUES (999,'0000000003')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO rooms(room_code) VALUES ('A01')")


@pytest.mark.parametrize('size,unavailable,expected', [('S', None, 'L01'), ('S', 'L01', 'L02'), ('M', None, 'L02')])
def test_best_fit(system, size, unavailable, expected):
    db, client, tokens = system
    if unavailable:
        with db.write() as conn:
            conn.execute("UPDATE lockers SET state='MAINTENANCE' WHERE id=?", (unavailable,))
    response = deposit(client, tokens, size)
    assert response.status_code == 201, response.text
    assert response.json()['locker_id'] == expected


def test_m_never_uses_s(system):
    db, client, tokens = system
    with db.write() as conn:
        conn.execute("UPDATE lockers SET online=0 WHERE id='L02'")
    assert deposit(client, tokens, 'M').status_code == 409


@pytest.mark.parametrize('state', ['RESERVED', 'DEPOSITING', 'OCCUPIED', 'COLLECTING', 'UNKNOWN', 'FAULT', 'MAINTENANCE'])
def test_unavailable_states(system, state):
    db, client, tokens = system
    with db.write() as conn:
        conn.execute('UPDATE lockers SET state=?', (state,))
    assert deposit(client, tokens).status_code == 409


def test_full(system):
    _, client, tokens = system
    assert deposit(client, tokens).status_code == 201
    assert deposit(client, tokens, request_id='d2').status_code == 201
    assert deposit(client, tokens, request_id='d3').status_code == 409


@pytest.mark.parametrize('overrides', [{'room_code': '999'}, {'recipient_phone': '0000000009'}, {'room_code': '1'}])
def test_wrong_recipient_no_creation(system, overrides):
    db, client, tokens = system
    assert deposit(client, tokens, **overrides).status_code == 404
    with db.read() as conn:
        assert conn.execute('SELECT count(*) FROM parcels').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM residents').fetchone()[0] == 2


def test_idempotency_and_conflict(system):
    db, client, tokens = system
    first = deposit(client, tokens)
    assert deposit(client, tokens).json() == first.json()
    assert deposit(client, tokens, 'M').status_code == 409
    with db.read() as conn:
        assert conn.execute('SELECT count(*) FROM transactions').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM commands').fetchone()[0] == 1


def test_concurrent_allocation(system):
    db, _, _ = system
    data = DepositInput(room_code='001', recipient_phone='0000000001', parcel_size='M')
    def run(_):
        try:
            with db.write() as conn:
                return allocate(conn, data)['locker_id']
        except BusinessError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(results) == ['L02', 'no_suitable_locker']
    with db.read() as conn:
        assert conn.execute('SELECT count(*) FROM transactions').fetchone()[0] == 1


def test_concurrent_http_duplicate_requests(system):
    db, client, tokens = system
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: deposit(client, tokens, 'M'), range(2)))
    assert all(result.status_code == 201 for result in results)
    assert results[0].json() == results[1].json()
    with db.read() as conn:
        assert conn.execute('SELECT count(*) FROM transactions').fetchone()[0] == 1


def test_concurrent_http_distinct_requests(system):
    db, client, tokens = system
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda i: deposit(client, tokens, 'M', request_id=f'race-{i}'), range(2)))
    assert sorted(result.status_code for result in results) == [201, 409]


def test_same_size_id_tiebreak(system):
    db, client, tokens = system
    with db.write() as conn:
        conn.execute("INSERT INTO lockers(id,size,state,online,node_id) VALUES ('L00','S','AVAILABLE',1,2)")
    assert deposit(client, tokens).json()['locker_id'] == 'L00'


def test_active_constraint_and_state_guard(system):
    db, client, tokens = system
    item = deposit(client, tokens).json()
    with db.write() as conn:
        with pytest.raises(BusinessError):
            transition(conn, 'lockers', item['locker_id'], 'AVAILABLE', item['parcel_id'], item['transaction_id'], item['locker_id'])
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO transactions(id,parcel_id,locker_id,kind,status,active,created_at) '
                         "VALUES ('duplicate',?,?,'DEPOSIT','PENDING',1,0)", (item['parcel_id'], item['locker_id']))
        # Even a corrupted AVAILABLE state cannot bypass the active reservation.
        conn.execute("UPDATE lockers SET state='AVAILABLE' WHERE id=?", (item['locker_id'],))
    assert deposit(client, tokens, request_id='next').json()['locker_id'] == 'L02'


@pytest.mark.parametrize('overrides', [{'parcel_size': 'L'}, {'room_code': 1}, {'room_code': 'A1'}, {'room_code': ' 001'}])
def test_input_validation(system, overrides):
    _, client, tokens = system
    assert deposit(client, tokens, **overrides).status_code == 422
