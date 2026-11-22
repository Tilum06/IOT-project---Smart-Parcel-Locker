import time
import uuid
from backend.devices import DeviceEvent, ingest


class MockDeviceAdapter:
    """Test-only trusted connectivity, servo results and MC-38 door snapshots."""
    def __init__(self, db, item):
        self.db, self.item = db, item
        with db.read() as conn:
            self.sequence = conn.execute('SELECT last_sequence FROM lockers WHERE id=?', (item['locker_id'],)).fetchone()[0]

    @staticmethod
    def connect(db):
        with db.write() as conn:
            conn.execute('UPDATE lockers SET online=1')

    def send(self, command_id, transaction_id, locker_id, action):
        assert transaction_id == self.item['transaction_id']
        assert locker_id == self.item['locker_id']
        self.emit(action + '_APPLIED', command_id=command_id, door='CLOSED')

    def emit(self, kind, **kwargs):
        self.sequence += 1
        event = DeviceEvent(event_id=str(uuid.uuid4()), transaction_id=self.item['transaction_id'],
                            locker_id=self.item['locker_id'], boot_id='test-boot', sequence=self.sequence,
                            observed_at=time.time(), kind=kind, **kwargs)
        ingest(self.db, event)
        return event

    def apply(self, action):
        with self.db.read() as conn:
            command = conn.execute('SELECT id FROM commands WHERE transaction_id=? AND action=?',
                                   (self.item['transaction_id'], action)).fetchone()
        assert command is not None
        self.send(command[0], self.item['transaction_id'], self.item['locker_id'], action)

    def cycle(self):
        self.apply('UNLOCK')
        self.emit('DOOR_OPEN')
        self.emit('DOOR_CLOSED')
