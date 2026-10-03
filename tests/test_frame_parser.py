import json
import unittest
import support
from skzp_control.frame_parser import FrameParser

class ParserTests(unittest.TestCase):
    def test_normal_fragmentation_is_not_logged(self):
        parser = FrameParser(65536)
        with self.assertNoLogs('skzp_control.frame_parser', level='DEBUG'):
            list(parser.feed(b'{"Token":"secret'))
            list(parser.feed(b' more'))
        self.assertGreater(parser.buffered_bytes, 0)
        self.assertEqual(list(parser.feed(b'"}')), [{"Token": "secret more"}])
        with self.assertNoLogs('skzp_control.frame_parser', level='DEBUG'):
            list(parser.feed(b'{'))

    def test_rejected_frame_log_without_content(self):
        with self.assertLogs('skzp_control.frame_parser', level='DEBUG') as logs:
            with self.assertRaises(ValueError):
                list(FrameParser(65536).feed(b'{"Token": SECRET}'))
        self.assertNotIn('SECRET', '\n'.join(logs.output))

    def test_all_packet_boundaries(self):
        obj = {'FrameType': 'SkzpData', 'text': 'żółć } { " \\', 'nested': {'list': [1, {'x': 2}]}}
        raw = json.dumps(obj, ensure_ascii=False).encode()
        for split in range(len(raw) + 1):
            p = FrameParser(65536)
            self.assertEqual(list(p.feed(raw[:split])) + list(p.feed(raw[split:])), [obj])
        p = FrameParser(65536)
        self.assertEqual([x for b in raw for x in p.feed(bytes([b]))], [obj])

    def test_multiple_frames_and_noise(self):
        p = FrameParser(20)
        self.assertEqual(list(p.feed(b'noise\r\n{} {"a":1}{"b":2}\n')), [{}, {'a':1}, {'b':2}])
        self.assertEqual(list(p.feed(b'{}' * 100)), [{}] * 100)

    def test_exact_limit(self):
        self.assertEqual(list(FrameParser(7).feed(b'{"x":1}')), [{'x':1}])
        p = FrameParser(6)
        with self.assertRaisesRegex(ValueError, 'size limit'):
            list(p.feed(b'{"x":1}'))
        self.assertLessEqual(len(p._buffer), 6)

    def test_incomplete_and_noise_bounded(self):
        p = FrameParser(32)
        self.assertEqual(list(p.feed(b'x' * 100000)), [])
        self.assertEqual(len(p._buffer), 0)
        with self.assertRaisesRegex(ValueError, 'size limit'):
            list(p.feed(b'{"x":"' + b'x' * 100000))
        self.assertLessEqual(len(p._buffer), 32)

    def test_invalid_frames_do_not_leak_contents(self):
        for raw in (b'{"Token": SECRET}', b'{"x":"\xff"}', b'{"x": [1,]}'):
            with self.assertRaisesRegex(ValueError, '^Received invalid JSON frame$'):
                list(FrameParser(65536).feed(raw))


