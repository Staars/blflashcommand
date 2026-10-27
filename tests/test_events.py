"""Tests for streamed progress/wait events and the bounded ROM handshake wait."""

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blflashcommand import FlashImage, flash_uart  # noqa: E402
from blflashcommand import _worker, api  # noqa: E402

CHILD = r'''
import json
prefix = '@@blflashcommand-event '
print('loader log line', flush=True)
print(prefix + json.dumps(dict(event='write', address=4096, done=512, total=1024)), flush=True)
print(prefix + 'not json', flush=True)
print('second log line', flush=True)
'''


class StreamingTests(unittest.TestCase):
    def test_events_reach_the_callback_and_stay_out_of_the_log(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            returncode, log = api._run_streaming([sys.executable, '-c', CHILD], Path(directory),
                                                 None, 30, 'flash', None, events.append)
        self.assertEqual(returncode, 0)
        self.assertEqual(events, [dict(event='write', address=4096, done=512, total=1024)])
        self.assertIn('loader log line', log)
        self.assertIn('second log line', log)
        self.assertIn('not json', log)
        self.assertNotIn('"done"', log)

    def test_a_silent_worker_is_killed_at_the_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(api.FlashError, 'timed out'):
                api._run_streaming([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   Path(directory), None, 0.5, 'probe', None, lambda event: None)

    def test_without_streaming_options_the_request_is_unchanged(self):
        image = Path(tempfile.mkdtemp()) / 'app.bin'
        image.write_bytes(b'app')
        with mock.patch('blflashcommand.api._request', return_value={'log': ''}) as request:
            flash_uart([FlashImage(image, 0x10000)], chip='bl616', port='/dev/null')
        for key in ('on_event', 'events', 'wait_seconds'):
            self.assertNotIn(key, request.call_args.kwargs)

    def test_wait_seconds_is_bounded(self):
        image = Path(tempfile.mkdtemp()) / 'app.bin'
        image.write_bytes(b'app')
        for value in (0, -1, 601, float('nan'), True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    flash_uart([FlashImage(image, 0x10000)], chip='bl602', port='/dev/null',
                               wait_seconds=value)


class FakeImgLoader:
    """Stands in for BflbImgLoader; fails the handshake a given number of times."""

    failures = 0
    calls = 0

    def img_load_shake_hand(self, *args, **kwargs):
        FakeImgLoader.calls += 1
        if FakeImgLoader.calls <= FakeImgLoader.failures:
            return 'shake hand fail'
        return 'OK'


def _events(output):
    return [json.loads(line[len(_worker.EVENT_PREFIX):]) for line in output.splitlines()
            if line.startswith(_worker.EVENT_PREFIX)]


class HandshakeWaitTests(unittest.TestCase):
    def _handshake(self, failures, seconds, clock):
        FakeImgLoader.failures, FakeImgLoader.calls = failures, 0
        module = mock.Mock(BflbImgLoader=FakeImgLoader)
        original = FakeImgLoader.img_load_shake_hand
        output = io.StringIO()
        try:
            with mock.patch.dict(sys.modules, {'libs.base.bflb_img_loader': module}), \
                    mock.patch('blflashcommand._worker.time.monotonic', side_effect=clock), \
                    mock.patch('sys.stdout', output):
                _worker._wait_for_handshake(dict(events=True, wait_seconds=seconds))
                status = FakeImgLoader().img_load_shake_hand(500000, 500000)
        finally:
            FakeImgLoader.img_load_shake_hand = original
        return status, FakeImgLoader.calls, _events(output.getvalue())

    def test_retries_until_the_board_enters_download_mode(self):
        status, calls, events = self._handshake(2, 15, iter([0, 3, 6, 9]).__next__)
        self.assertEqual((status, calls), ('OK', 3))
        self.assertEqual(events, [dict(event='connecting'), dict(event='waiting', remaining=12),
                                  dict(event='waiting', remaining=9), dict(event='connected')])

    def test_gives_up_after_the_deadline(self):
        status, calls, events = self._handshake(99, 5, iter([0, 3, 6]).__next__)
        self.assertEqual((status, calls), ('shake hand fail', 2))
        self.assertEqual([event['event'] for event in events], ['connecting', 'waiting'])

    def test_no_wait_requested_leaves_the_handshake_alone(self):
        original = FakeImgLoader.img_load_shake_hand
        with mock.patch.dict(sys.modules, {'libs.base.bflb_img_loader':
                                           mock.Mock(BflbImgLoader=FakeImgLoader)}):
            _worker._wait_for_handshake(dict(events=True))
        self.assertIs(FakeImgLoader.img_load_shake_hand, original)


class WriteProgressTests(unittest.TestCase):
    def test_each_image_reports_start_chunks_and_completion(self):
        class Loader:
            def flash_load_main_process(self, file, start_addr, callback=None):
                callback(2048, 4096, 'APP_WR')
                callback(4096, 4096, 'APP_WR')
                return True

        loader = Loader()
        output = io.StringIO()
        with mock.patch('sys.stdout', output):
            _worker._report_writes(loader, dict(events=True))
            loader.flash_load_main_process('app.bin', 0x80010000, None)
        self.assertEqual(_events(output.getvalue()), [
            dict(event='write', address=0x10000, done=0, total=0),
            dict(event='write', address=0x10000, done=2048, total=4096),
            dict(event='write', address=0x10000, done=4096, total=4096),
            dict(event='written', address=0x10000),
        ])


if __name__ == '__main__':
    unittest.main()
