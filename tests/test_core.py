import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import support
from skzp_control.client.client import SkzpClient
from skzp_control.client.transport import TcpTransport
from skzp_control.client.protocol import SkzpProtocol
from skzp_control.client.exceptions import NotConnectedError, MissingCredentialsError
from skzp_control.coordinator import SkzpCoordinator

class Writer:
    def __init__(self, *, stall=False, fail_at=None, close_stall=False):
        self.reader = asyncio.StreamReader()
        self.transport = self
        self.frames = []
        self.active = self.closed = self.aborted = False
        self.stall, self.fail_at, self.close_stall = stall, fail_at, close_stall
        self.started = asyncio.Event()
    def is_closing(self): return self.closed or self.aborted
    def write(self, payload):
        assert not self.active, 'overlapping writes'
        self.active = True
        self.frames.append(payload)
    async def drain(self):
        self.started.set()
        try:
            if self.stall: await asyncio.Event().wait()
            await asyncio.sleep(0)
            if len(self.frames) == self.fail_at: raise OSError('secret must not be logged')
        finally: self.active = False
    def close(self): self.closed = True; self.reader.feed_eof()
    def abort(self): self.aborted = True; self.reader.feed_eof()
    async def wait_closed(self):
        if self.close_stall: await asyncio.Event().wait()

def attach(t, w): t._reader, t._writer = w.reader, w

def make_client(w, ha=False):
    if ha:
        c = SkzpCoordinator(SimpleNamespace(bus=SimpleNamespace(async_fire=lambda *_: None), config=SimpleNamespace(language='en')), 'host', 1, 'id', {})
        c._set_connected(True)
        c._data_ready_event.set()
        import time
        c._last_data_received = time.monotonic()
    else: c = SkzpClient('host', 1)
    attach(c.transport, w)
    c.transport.write_timeout = c.transport.close_timeout = .02
    c.data.update(DevId=1, DevPin=2, Token='secret')
    return c

class ProtocolTests(unittest.TestCase):
    def test_firmware_update_refreshes_registry_once_and_requires_reload(self):
        client = SkzpCoordinator(SimpleNamespace(), 'host', 1, 'entry', {})
        old = 'SKZP-05S_V5.65_2025-06-17'
        new = 'SKZP-05S_V5.71_2026-10-04'
        client.platform_device_type = old
        client.data['DevType'] = old
        self.assertFalse(client.requires_platform_reload)
        registry = Mock()
        registry.async_get_device.return_value = SimpleNamespace(
            id='device', sw_version='5.65 (2025-06-17)'
        )
        with patch('skzp_control.coordinator.dr.async_get', return_value=registry,
                   create=True):
            client.data['DevType'] = new
            client._update_device_firmware({'DevType': new})
            client._update_device_firmware({'DevType': new})
            client._update_device_firmware({})
            client._update_device_firmware({'DevType': 'invalid'})
        registry.async_update_device.assert_called_once_with(
            'device', sw_version='5.71 (2026-10-04)'
        )
        self.assertTrue(client.requires_platform_reload)
        client.platform_device_type = new
        self.assertFalse(client.requires_platform_reload)

    def test_firmware_update_waits_for_device_registration(self):
        client = SkzpCoordinator(SimpleNamespace(), 'host', 1, 'entry', {})
        fields = {'DevType': 'SKZP-05S_V5.71_2026-10-04'}
        registry = Mock()
        registry.async_get_device.side_effect = [None, SimpleNamespace(
            id='device', sw_version='5.71 (2026-10-04)')]
        with patch('skzp_control.coordinator.dr.async_get', return_value=registry,
                   create=True):
            client._update_device_firmware(fields)
            self.assertIsNone(client._registered_firmware_version)
            client._update_device_firmware(fields)
        registry.async_update_device.assert_not_called()
        self.assertEqual(client._registered_firmware_version, '5.71 (2026-10-04)')

    def test_alarm_changes_logged_once_and_missing_data_ignored(self):
        client = object.__new__(SkzpCoordinator)
        client.host, client.port = 'host', 1
        client._logged_alarms = set()
        client.translate = lambda key, **kwargs: key
        with self.assertLogs('skzp_control.coordinator', level='DEBUG') as logs:
            client._log_alarm_changes({"Alarms": "A000"})
            client._log_alarm_changes({"Alarms": "A000"})
            client._log_alarm_changes({})
            client._log_alarm_changes({"Alarms": ""})
            client._log_alarm_changes({"Alarms": "0000"})
        self.assertEqual(sum('Alarm active:' in line for line in logs.output), 1)
        self.assertEqual(sum('Alarm cleared:' in line for line in logs.output), 1)
        self.assertEqual(client._logged_alarms, set())

    def test_wire_compatibility(self):
        state = dict(DevId=0, DevPin='', Token='secret')
        expected = (json.dumps({**state, 'x': '42'})+'\n').encode()
        self.assertEqual(SkzpProtocol().encode_command({'x':'42'}, state), expected)
    def test_missing_credentials(self):
        with self.assertRaises(MissingCredentialsError) as cm:
            SkzpProtocol().encode_command({}, {'Token':'secret'})
        self.assertEqual(cm.exception.fields, ['DevId','DevPin'])
        self.assertNotIn('secret', str(cm.exception))
    def test_decode_preserves_unknown_fields(self):
        obj = {'FrameType':'SkzpData', 'Token':'secret', 'future': [1]}
        msg = SkzpProtocol().decode_message(obj)
        self.assertEqual(msg.fields, obj); self.assertTrue(msg.is_data)
        self.assertNotIn('secret', repr(msg))
        self.assertFalse(SkzpProtocol().decode_message({'FrameType':'Other'}).is_data)

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_commands(self):
        w = Writer(); c = make_client(w)
        await asyncio.gather(*(c.send_command({'x':i}) for i in range(10)))
        self.assertEqual([json.loads(x)['x'] for x in w.frames], list(range(10)))
    async def test_failure_blocks_queued_commands(self):
        w = Writer(fail_at=5); c = make_client(w)
        results = await asyncio.gather(*(c.send_command({'x':i}) for i in range(10)),return_exceptions=True)
        self.assertEqual(len(w.frames), 5)
        self.assertIsInstance(results[4], OSError)
        self.assertTrue(all(isinstance(x,NotConnectedError) for x in results[5:]))
        attach(c.transport, Writer()); await c.send_command({'x':10})
    async def test_timeout(self):
        w = Writer(stall=True); c = make_client(w, True)
        with self.assertRaises(support.HAError) as cm: await c.send_command({'x':1})
        self.assertEqual(cm.exception.key,'command_send_timeout')
        self.assertTrue(w.aborted); self.assertFalse(c.connected)
        self.assertFalse(c._command_lock.locked())
    async def test_cancel_write(self):
        w = Writer(stall=True); c = make_client(w)
        task = asyncio.create_task(c.send_command({'x':1})); await w.started.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(w.aborted); self.assertFalse(c._command_lock.locked())
    async def test_cancel_queued_write(self):
        w = Writer(); c = make_client(w)
        await c._command_lock.acquire()
        task=asyncio.create_task(c.send_command({'x':1})); await asyncio.sleep(0); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        c._command_lock.release()
        self.assertTrue(c.connected); self.assertEqual(w.frames, [])
    async def test_revalidate_after_wait(self):
        w=Writer(); c=make_client(w)
        await c._command_lock.acquire()
        task=asyncio.create_task(c.send_command({'x':1})); await asyncio.sleep(0)
        c.transport.abort(); c._command_lock.release()
        with self.assertRaises(NotConnectedError): await task
        self.assertEqual(w.frames,[])
    async def test_old_failure_preserves_new_connection(self):
        old=Writer(stall=True); c=make_client(old, True)
        task=asyncio.create_task(c.send_command({'x':1})); await old.started.wait()
        new=Writer(); attach(c.transport,new)
        with self.assertRaises(support.HAError): await task
        self.assertTrue(c.connected); self.assertFalse(new.aborted)
    async def test_bounded_close_and_cancellation(self):
        for cancel in (False,True):
            w=Writer(close_stall=True); c=make_client(w)
            task=asyncio.create_task(c.disconnect()); await asyncio.sleep(.001)
            if cancel: task.cancel()
            try: await task
            except asyncio.CancelledError: self.assertTrue(cancel)
            self.assertTrue(w.aborted); self.assertFalse(c.connected)
    async def test_connect_timeout_and_cancel(self):
        async def hanging(*_): await asyncio.Event().wait()
        for cancel in (False,True):
            t=TcpTransport('host',1,connect_timeout=.02)
            with patch('asyncio.open_connection', hanging):
                task=asyncio.create_task(t.connect()); await asyncio.sleep(.001)
                if cancel: task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError): await task
            self.assertFalse(t.connected)
    async def test_disconnect_during_connect(self):
        gate=asyncio.Event(); w=Writer(); t=TcpTransport('host',1)
        async def opening(*_): await gate.wait(); return w.reader,w
        with patch('asyncio.open_connection',opening):
            task=asyncio.create_task(t.connect()); await asyncio.sleep(0)
            await t.disconnect(); gate.set()
            with self.assertRaises(NotConnectedError): await task
        self.assertTrue(w.aborted); self.assertFalse(t.connected)
    async def test_semantic_api(self):
        w=Writer(); c=make_client(w)
        await c.set_parameter('BoilerTempCmd','65'); await c.execute_command('test')
        self.assertEqual(json.loads(w.frames[0])['BoilerTempCmd'],'65')
        self.assertEqual(json.loads(w.frames[1])['CommandToDo'],'test')
    async def test_missing_credentials_translation(self):
        c=make_client(Writer(),True); c.data.pop('Token')
        with self.assertRaises(support.HAError) as cm: await c.send_command({'x':1})
        self.assertEqual(cm.exception.key,'missing_command_credentials')
    async def test_reconnect_resets_parser_and_credentials(self):
        for failure in (b'', b'bad}', b'x'*65536):
            w=Writer(); c=make_client(w,True); c.reconnect_delay=.001
            c._connect=lambda: asyncio.sleep(0)
            c._mark_data_received=lambda: None
            w.reader.feed_data(b'{"old":')
            if failure: w.reader.feed_data(failure)
            else: w.reader.feed_eof()
            new=Writer()
            async def opening(*_): return new.reader,new
            original_connect=SkzpCoordinator._connect.__get__(c)
            calls=[]
            async def reconnect(*args):
                calls.append(args); await c.disconnect(); await original_connect()
            c._reconnect=reconnect
            with patch('asyncio.open_connection',opening):
                task=asyncio.create_task(c._read_loop())
                for _ in range(100):
                    if calls: break
                    await asyncio.sleep(.001)
                self.assertEqual(len(calls),1)
                for _ in range(100):
                    if c.transport._writer is new: break
                    await asyncio.sleep(.001)
                self.assertNotIn('Token',c.data)
                new.reader.feed_data(b'{"FrameType":"SkzpData","DevType":"SKZP-02T","Token":"fresh"}')
                await asyncio.sleep(.01)
                self.assertEqual(c.data['Token'],'fresh'); self.assertNotIn('old',c.data)
                task.cancel(); await task
                await c.disconnect()
    async def test_read_timeout_reconnect(self):
        c=make_client(Writer(),True); c.no_data_timeout=.01
        c._connect=lambda: asyncio.sleep(0)
        async def reconnect(*_): raise asyncio.CancelledError()
        c._reconnect=reconnect
        with self.assertRaises(asyncio.CancelledError): await c._read_loop()
        self.assertTrue(c._outage_active)
        await c.disconnect()
    async def test_receive_split_and_multiple(self):
        w=Writer(); c=make_client(w)
        w.reader.feed_data(b'{"x":'); self.assertEqual(list(await c.receive()),[])
        w.reader.feed_data(b'1}{"y":2}')
        self.assertEqual([m.fields for m in await c.receive()],[{'x':1},{'y':2}])
    async def test_send_logs_do_not_leak_exception_text(self):
        c=make_client(Writer(fail_at=1),True)
        with self.assertLogs('skzp_control.coordinator',level='WARNING') as logs:
            with self.assertRaises(support.HAError): await c.send_command({'Token':'secret'})
        self.assertNotIn('secret',''.join(logs.output))

if __name__=='__main__': unittest.main()
