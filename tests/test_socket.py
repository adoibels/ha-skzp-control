"""Testuje gniazda asyncio w cyklu życia integracji HA."""
import asyncio
import json
import unittest
from types import SimpleNamespace
import support
from skzp_control.coordinator import SkzpCoordinator

class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_fresh_token_and_stop(self):
        connections = []
        commands = asyncio.Queue()
        tasks = set()
        async def controller(reader, writer):
            task = asyncio.current_task(); tasks.add(task)
            connections.append(writer)
            token = len(connections)
            frame = json.dumps(dict(FrameType='SkzpData', DevType='SKZP-02T', DevId=1, DevPin=2, Token=token)).encode()
            try:
                writer.write(frame[:12]); await writer.drain(); await asyncio.sleep(.001)
                writer.write(frame[12:]+b'{"BoilerTempCmd":"60"}'); await writer.drain()
                while line := await reader.readline(): await commands.put(json.loads(line))
            finally:
                writer.close(); await writer.wait_closed(); tasks.discard(task)
        server = await asyncio.start_server(controller,'127.0.0.1',0)
        c = SkzpCoordinator(SimpleNamespace(bus=SimpleNamespace(async_fire=lambda *_:None),config=SimpleNamespace(language='en')), '127.0.0.1',server.sockets[0].getsockname()[1], 'test', {})
        c.reconnect_delay=.001
        try:
            await c.start(); self.assertTrue(await c.wait_for_data(1))
            await c.set_parameter('BoilerTempCmd','65')
            first=await asyncio.wait_for(commands.get(),1); self.assertEqual(first['Token'],1)
            connections[0].close()
            async def recovered():
                while c.data.get('Token') != 2 or not c.available: await asyncio.sleep(.001)
            await asyncio.wait_for(recovered(),1)
            await c.execute_command('test')
            second=await asyncio.wait_for(commands.get(),1); self.assertEqual(second['Token'],2)
            self.assertEqual(len(connections),2)
        finally:
            await c.stop(); server.close(); await server.wait_closed()
            for w in connections: w.close()
            if tasks: await asyncio.gather(*tasks)
        self.assertFalse(c.available)
        self.assertIsNone(c._task); self.assertIsNone(c._watchdog_task)

    async def test_valid_frame_before_invalid_frame_is_retained(self):
        from test_core import Writer, make_client
        w=Writer(); c=make_client(w)
        w.reader.feed_data(b'{"x":1}{"Token":secret}')
        messages=await c.receive()
        self.assertEqual(next(messages).fields,{'x':1})
        with self.assertRaises(ValueError): next(messages)
        self.assertEqual(c.data['x'],1)
