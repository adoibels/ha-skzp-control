import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import support
from skzp_control.client.command_manager import CommandManager


class CommandManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = CommandManager()
        self.send = AsyncMock()
        self.wait = AsyncMock(return_value=False)
        self.current = True

    async def execute(self, **overrides):
        options = dict(send=self.send, wait=self.wait,
                       is_current=lambda: self.current, retry_count=2,
                       retry_delay=0)
        options.update(overrides)
        return await self.manager.execute(**options)

    async def test_confirmed_without_retry(self):
        self.wait.return_value = True
        self.assertTrue(await self.execute())
        self.send.assert_awaited_once_with(0)

    async def test_retry_exhaustion(self):
        self.assertFalse(await self.execute())
        self.assertEqual(self.send.await_count, 3)
        self.assertEqual([call.args[0] for call in self.send.await_args_list], [0, 1, 2])

    async def test_zero_retries(self):
        self.assertFalse(await self.execute(retry_count=0))
        self.send.assert_awaited_once_with(0)

    async def test_second_attempt_confirmation(self):
        self.wait.side_effect = [False, True]
        self.assertTrue(await self.execute())
        self.assertEqual(self.send.await_count, 2)

    async def test_superseded_during_confirmation(self):
        async def wait(): self.current = False; return False
        self.assertTrue(await self.execute(wait=wait))
        self.send.assert_awaited_once_with(0)

    async def test_superseded_during_retry_delay(self):
        async def delay(_seconds):
            self.current = False
        with patch('skzp_control.client.command_manager.asyncio.sleep', delay):
            self.assertTrue(await self.execute(retry_delay=1))
        self.send.assert_awaited_once_with(0)

    async def test_transport_error_is_not_retried(self):
        self.send.side_effect = OSError()
        with self.assertRaises(OSError): await self.execute()
        self.wait.assert_not_awaited()
        self.send.assert_awaited_once_with(0)
        self.assertEqual(self.manager.active_count, 0)

    async def test_confirmation_during_retry_delay_skips_send(self):
        confirmed = asyncio.Event()
        async def delay(_seconds):
            confirmed.set()
        with patch('skzp_control.client.command_manager.asyncio.sleep', delay):
            self.assertTrue(await self.execute(retry_delay=1, is_confirmed=confirmed.is_set))
        self.send.assert_awaited_once_with(0)
        self.wait.assert_awaited_once()

    async def test_unconfirmed_state_still_retries(self):
        self.assertFalse(await self.execute(is_confirmed=lambda: False))
        self.assertEqual(self.send.await_count, 3)

    async def test_cancellation_during_confirmation_and_delay(self):
        for phase in ('confirmation', 'delay'):
            started = asyncio.Event()
            async def blocking(*_args):
                started.set()
                await asyncio.Event().wait()
            options = dict(wait=blocking) if phase == 'confirmation' else dict(retry_delay=60)
            with patch('skzp_control.client.command_manager.asyncio.sleep', blocking):
                task = asyncio.create_task(self.execute(**options))
                await started.wait(); task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(self.manager.active_count, 0)

    async def test_event_requires_value_predicate(self):
        event = asyncio.Event(); event.set()
        self.assertFalse(await self.manager.wait_for_confirmation(event, .01, lambda: False))
        event.set()
        self.assertTrue(await self.manager.wait_for_confirmation(event, .01, lambda: True))
        event.clear()
        self.assertFalse(await self.manager.wait_for_confirmation(event, .001, lambda: True))

    async def test_old_signal_does_not_end_wait_for_fresh_confirmation(self):
        event = asyncio.Event(); event.set()
        matches = False
        task = asyncio.create_task(self.manager.wait_for_confirmation(event, 1, lambda: matches))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertFalse(event.is_set())
        matches = True
        event.set()
        self.assertTrue(await task)

    async def test_superseded_confirmation_wakes_without_waiting_for_timeout(self):
        event = asyncio.Event()
        task = asyncio.create_task(self.manager.wait_for_confirmation(
            event, 60, lambda: False, is_current=lambda: self.current))
        await asyncio.sleep(0)
        self.current = False
        event.set()
        self.assertFalse(await asyncio.wait_for(task, .2))

    async def test_independent_commands_confirm_concurrently(self):
        first_waiting = asyncio.Event(); release = asyncio.Event()
        async def waiting(): first_waiting.set(); await release.wait(); return True
        task = asyncio.create_task(self.execute(wait=waiting))
        await first_waiting.wait()
        self.assertTrue(await asyncio.wait_for(self.execute(wait=AsyncMock(return_value=True)), .1))
        release.set(); self.assertTrue(await task)
