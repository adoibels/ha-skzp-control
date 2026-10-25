import asyncio
import errno
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import support
from skzp_control.client.transport import TcpTransport
from skzp_control.client.protocol import SkzpProtocol
from skzp_control.client.exceptions import ConnectionClosedError, NotConnectedError, MissingCredentialsError
from skzp_control.coordinator import SkzpCoordinator, format_communication_error
from skzp_control.frame_parser import FrameTooLargeError, InvalidFrameError

from helpers import Writer, attach, make_client


class ProtocolTests(unittest.TestCase):
    def test_safe_error_descriptions_and_codes(self):
        windows_error = OSError('secret')
        windows_error.winerror = 10054
        for error, expected in (
            (TimeoutError('secret'), 'Operation timed out'),
            (OSError(errno.ECONNREFUSED, 'secret'), f'Connection refused (error {errno.ECONNREFUSED})'),
            (OSError(errno.EHOSTUNREACH, 'secret'), f'Host unreachable (error {errno.EHOSTUNREACH})'),
            (OSError(99999, 'secret'), 'Network operation failed (error 99999)'),
            (OSError('secret'), 'Network operation failed'),
            (windows_error, 'Connection reset by peer (error 10054)'),
            (ConnectionResetError('secret'), 'Connection reset by peer'),
            (socket.gaierror(socket.EAI_AGAIN, 'secret'), f'Address lookup failed (error {socket.EAI_AGAIN})'),
            (ConnectionClosedError('secret'), 'TCP connection closed by peer'),
            (NotConnectedError('secret'), 'Connection unavailable or interrupted'),
            (InvalidFrameError('secret'), 'Invalid JSON or UTF-8 data received'),
            (FrameTooLargeError('secret'), 'Received data exceeds the size limit'),
            (ValueError('secret'), 'Invalid value or data format'),
            (RuntimeError('secret'), 'RuntimeError'),
        ):
            with self.subTest(error=type(error).__name__, expected=expected):
                self.assertEqual(format_communication_error(error), expected)
                self.assertNotIn('secret', format_communication_error(error))

    def test_one_debug_entry_per_command_attempt(self):
        client = SkzpCoordinator(SimpleNamespace(), 'host', 1, 'entry', {})
        client.command_retry_count = 2
        with self.assertLogs('skzp_control.coordinator', level='DEBUG') as logs:
            for attempt in range(3):
                client.log_command_sent('CH1 Mode', 'Active', attempt)
        self.assertEqual([record.levelname for record in logs.records], ['DEBUG'] * 3)
        self.assertEqual([record.getMessage() for record in logs.records], [
            '[SKZP Control] host:1 — Command sent: CH1 Mode = Active.',
            '[SKZP Control] host:1 — Command resent: CH1 Mode = Active (retry 1/2).',
            '[SKZP Control] host:1 — Command resent: CH1 Mode = Active (retry 2/2).',
        ])

    def test_firmware_update_refreshes_registry_once_and_requires_reload(self):
        client = SkzpCoordinator(SimpleNamespace(), 'host', 1, 'entry', {})
        old = 'SKZP-05S_V5.65_2025-06-17'
        new = 'SKZP-05S_V5.71_2026-10-04'
        client.platform_device_type = old
        client.data['DevType'] = old
        self.assertFalse(client.requires_platform_reload)
        registry = Mock()
        registry.async_get_device_by_identifier.return_value = SimpleNamespace(
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
        registry.async_get_device_by_identifier.assert_called_once_with(
            ('skzp_control', 'entry'), 'entry'
        )
        registry.async_get_device.assert_not_called()
        self.assertTrue(client.requires_platform_reload)
        client.platform_device_type = new
        self.assertFalse(client.requires_platform_reload)

    def test_firmware_update_waits_for_device_registration(self):
        client = SkzpCoordinator(SimpleNamespace(), 'host', 1, 'entry', {})
        fields = {'DevType': 'SKZP-05S_V5.71_2026-10-04'}
        registry = Mock()
        registry.async_get_device_by_identifier.side_effect = [None, SimpleNamespace(
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

    async def test_receive_error_logs_have_address_and_safe_reason(self):
        for payload, reason in (
            (b'', 'TCP connection closed by peer'),
            (b'{"Token": SECRET}', 'Invalid JSON or UTF-8 data received'),
            (b'{"Token":"' + b'SECRET' * 11000, 'Received data exceeds the size limit'),
        ):
            with self.subTest(reason=reason):
                writer = Writer(); client = make_client(writer, True)
                client._connect = AsyncMock()
                async def reconnect(message, *args):
                    client._log_connection_issue(message, *args)
                    raise asyncio.CancelledError()
                client._reconnect = reconnect
                if payload:
                    writer.reader.feed_data(payload)
                else:
                    writer.reader.feed_eof()
                with self.assertLogs('skzp_control', level='DEBUG') as logs:
                    with self.assertRaises(asyncio.CancelledError):
                        await client._read_loop()
                    await client.disconnect()
                messages = [record.getMessage() for record in logs.records]
                self.assertTrue(all('[SKZP Control] host:1 — ' in text for text in messages))
                self.assertIn(reason, '\n'.join(messages))
                self.assertNotIn('SECRET', '\n'.join(messages))
                self.assertEqual(sum(record.levelname == 'WARNING' for record in logs.records), 1)


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def ticks(self, client, values):
        positions = iter(values)
        clock = SimpleNamespace(value=0)
        async def step(_delay):
            try:
                clock.value = next(positions)
            except StopIteration:
                raise asyncio.CancelledError() from None
        with patch('skzp_control.coordinator.time', SimpleNamespace(monotonic=lambda: clock.value)), \
             patch('skzp_control.coordinator.asyncio.sleep', step):
            with self.assertRaises(asyncio.CancelledError):
                await client._watchdog_loop()

    async def test_restarts_only_active_silent_connection_once(self):
        for name, connected, last_data, connected_at, times, expected in (
            ('healthy', True, 100, 100, [127, 128, 129], 0),
            ('silent', True, 100, 100, [130, 131, 132], 1),
            ('offline with old data', False, 100, None, [131, 132, 133], 0),
            ('fresh connection', True, 100, 200, [201, 215, 229], 0),
            ('fresh but silent', True, 100, 200, [230, 231, 232], 1),
            ('first connection', True, None, 200, [229, 230, 231], 1),
            ('before first connection', False, None, None, [230, 231], 0),
        ):
            with self.subTest(scenario=name):
                client = make_client(Writer(), True)
                client.no_data_timeout = 30
                client._last_data_received = last_data
                if not connected:
                    client.transport.abort()
                    client._set_connected(False)
                client._connected_at = connected_at
                with patch.object(client.transport, 'abort', wraps=client.transport.abort) as abort, \
                     patch('skzp_control.coordinator._LOGGER.log') as log, \
                     patch('skzp_control.coordinator.persistent_notification.async_create') as notify:
                    await self.ticks(client, times)
                    self.assertEqual(abort.call_count, expected)
                    self.assertEqual(log.call_count, expected)
                    self.assertEqual(notify.call_count, expected if client.notify_connection_lost else 0)
                await client.disconnect()

    async def test_pending_reconnect_is_not_aborted(self):
        client = make_client(Writer(), True)
        client.transport.abort(); client._set_connected(False)
        client._last_data_received = 100
        entered = asyncio.Event(); release = asyncio.Event(); writer = Writer()
        async def opening(*_args):
            entered.set()
            await release.wait()
            return writer.reader, writer
        with patch('asyncio.open_connection', opening), \
             patch.object(client.transport, 'abort', wraps=client.transport.abort) as abort:
            task = asyncio.create_task(client.connect())
            await entered.wait()
            await self.ticks(client, [131, 132, 133])
            abort.assert_not_called()
            release.set()
            await task
        self.assertTrue(client.transport.connected)
        self.assertFalse(writer.aborted)
        await client.disconnect()

    async def test_reconnected_outage_keeps_fresh_deadline_and_one_notification(self):
        client = make_client(Writer(), True)
        client.no_data_timeout = 30
        client._last_data_received = client._connected_at = 100
        client.notify_connection_lost = client.notify_connection_restored = True
        with patch('skzp_control.coordinator._LOGGER.log'), \
             patch('skzp_control.coordinator.persistent_notification.async_create') as notify:
            await self.ticks(client, [130, 131])
            self.assertEqual(notify.call_count, 1)
            attach(client.transport, Writer())
            with patch('skzp_control.coordinator.time', SimpleNamespace(monotonic=lambda: 200)):
                client._set_connected(True)
            await self.ticks(client, [201, 229])
            self.assertTrue(client.connected)
            await self.ticks(client, [230, 231])
            self.assertFalse(client.connected)
            self.assertEqual(notify.call_count, 1)
            attach(client.transport, Writer())
            client._set_connected(True)
            client._mark_data_received()
            client._mark_data_received()
            self.assertEqual(notify.call_count, 2)
        await client.disconnect()

if __name__=='__main__': unittest.main()
