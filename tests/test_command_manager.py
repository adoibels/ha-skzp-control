import asyncio
import unittest
from unittest.mock import AsyncMock, Mock
import support
from skzp_control.client.command_manager import CommandManager


class CommandManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = CommandManager()
        self.send = AsyncMock()
        self.wait = AsyncMock(return_value=False)
        self.current = True
        self.retry = Mock()

    async def execute(self, **overrides):
        options = dict(send=self.send, wait=self.wait,
                       is_current=lambda: self.current, retry_count=2,
                       retry_delay=0, on_retry=self.retry)
        options.update(overrides)
        return await self.manager.execute(**options)

    async def test_confirmed_without_retry(self):
        self.wait.return_value = True
        self.assertTrue(await self.execute())
        self.send.assert_awaited_once(); self.retry.assert_not_called()

    async def test_retry_exhaustion(self):
        self.assertFalse(await self.execute())
        self.assertEqual(self.send.await_count, 3)
        self.assertEqual([call.args[0] for call in self.retry.call_args_list], [1, 2])

    async def test_zero_retries(self):
        self.assertFalse(await self.execute(retry_count=0))
        self.send.assert_awaited_once(); self.retry.assert_not_called()

    async def test_second_attempt_confirmation(self):
        self.wait.side_effect = [False, True]
        self.assertTrue(await self.execute())
        self.assertEqual(self.send.await_count, 2)

    async def test_superseded_during_confirmation(self):
        async def wait(): self.current = False; return False
        self.assertTrue(await self.execute(wait=wait))
        self.send.assert_awaited_once(); self.retry.assert_not_called()

    async def test_superseded_during_retry_delay(self):
        retry_started = asyncio.Event()
        task = asyncio.create_task(self.execute(retry_delay=.02, on_retry=lambda _: retry_started.set()))
        await retry_started.wait(); self.current = False
        self.assertTrue(await task); self.send.assert_awaited_once()

    async def test_transport_error_is_not_retried(self):
        self.send.side_effect = OSError()
        with self.assertRaises(OSError): await self.execute()
        self.wait.assert_not_awaited(); self.retry.assert_not_called()

    async def test_cancellation_during_confirmation_and_delay(self):
        for phase in ('confirmation', 'delay'):
            started = asyncio.Event()
            async def wait(): started.set(); await asyncio.Event().wait()
            options = (dict(wait=wait) if phase == 'confirmation' else
                       dict(retry_delay=60, on_retry=lambda _: started.set()))
            task = asyncio.create_task(self.execute(**options))
            await started.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task

    async def test_event_requires_value_predicate(self):
        event = asyncio.Event(); event.set()
        self.assertFalse(await self.manager.wait_for_confirmation(event, .01, lambda: False))
        self.assertTrue(await self.manager.wait_for_confirmation(event, .01, lambda: True))
        event.clear()
        self.assertFalse(await self.manager.wait_for_confirmation(event, .001, lambda: True))

    async def test_independent_commands_confirm_concurrently(self):
        first_waiting = asyncio.Event(); release = asyncio.Event()
        async def waiting(): first_waiting.set(); await release.wait(); return True
        task = asyncio.create_task(self.execute(wait=waiting))
        await first_waiting.wait()
        self.assertTrue(await asyncio.wait_for(self.execute(wait=AsyncMock(return_value=True)), .1))
        release.set(); self.assertTrue(await task)
