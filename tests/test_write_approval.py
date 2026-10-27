"""Tests for the caller's approval inside the write session, before the first erase."""

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

FLASH = bytes(range(256)) * 64


class FakeLoader:
    """A loader after step 4: JEDEC known, flash readable, step 5 not yet run."""

    read_flash_id = 'c84016'
    bootinfo = b'01000000'
    _mac_addr = b'\x01\x02\x03\x04\x05\x06'
    _bflb_com_tx_size = 4104

    def __init__(self):
        self.written = False

    def flash_read_jedec_id_process(self):
        return True, bytearray.fromhex('c8401600')

    def flash_read_main_process(self, address, count):
        return True, FLASH[address:address + count]

    def fifth_run_step_write_flash_and_check(self):
        self.written = True
        return True, 'written'


class WorkerApprovalTests(unittest.TestCase):
    def _session(self, decision):
        loader = FakeLoader()
        request = dict(chip='bl602', inspect=[[0x100, 0x20], [0x1000, 0x10]])
        config = {'param': {'chip_type': 'bl602'}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _worker._guard_flash_capacity(
                loader, None, config, approval=lambda: _worker._await_approval(
                    loader, request, config, root))
            with mock.patch('sys.stdin', io.StringIO(decision)):
                try:
                    loader.fifth_run_step_write_flash_and_check()
                finally:
                    evidence = json.loads((root / 'inspect.json').read_text())
                    ranges = {item['address']: (root / item['path']).read_bytes()
                              for item in evidence['ranges']}
        return loader, evidence, ranges

    def test_approved_write_sees_inspected_bytes_before_writing(self):
        loader, evidence, ranges = self._session('continue\n')
        self.assertTrue(loader.written)
        self.assertEqual(evidence['probe']['jedec_id'], 'c84016')
        self.assertEqual(ranges, {0x100: FLASH[0x100:0x120], 0x1000: FLASH[0x1000:0x1010]})

    def test_declined_or_closed_answer_prevents_the_write(self):
        for decision in ('abort\n', ''):
            with self.subTest(decision=decision):
                loader = FakeLoader()
                with self.assertRaisesRegex(RuntimeError, 'declined'):
                    self._session_with(loader, decision)
                self.assertFalse(loader.written)

    def _session_with(self, loader, decision):
        request = dict(chip='bl602', inspect=[[0, 4]])
        config = {'param': {'chip_type': 'bl602'}}
        with tempfile.TemporaryDirectory() as directory:
            _worker._guard_flash_capacity(
                loader, None, config, approval=lambda: _worker._await_approval(
                    loader, request, config, Path(directory)))
            with mock.patch('sys.stdin', io.StringIO(decision)):
                loader.fifth_run_step_write_flash_and_check()


CHILD = r'''
import json, sys
from pathlib import Path
root = Path.cwd()
(root / 'inspect_0.bin').write_bytes(b'table')
probe = dict(chip='bl602', backend='bl602', boot_info='', mac_address='',
             jedec_id='c84016', jedec_response='c8401600')
(root / 'inspect.tmp').write_text(json.dumps(
    dict(probe=probe, ranges=[dict(address=57344, path='inspect_0.bin')])))
(root / 'inspect.tmp').replace(root / 'inspect.json')
print('@@blflashcommand-event ' + json.dumps(dict(event='approve')), flush=True)
print('decision:', sys.stdin.readline().strip())
'''


class ParentApprovalTests(unittest.TestCase):
    def _run(self, approve):
        with tempfile.TemporaryDirectory() as directory:
            return api._run_streaming([sys.executable, '-c', CHILD], Path(directory),
                                      None, 30, 'flash', approve, None)

    def test_approval_receives_evidence_and_continues_the_worker(self):
        seen = []
        returncode, log = self._run(lambda probe, ranges: seen.append((probe.jedec_id, ranges)))
        self.assertEqual((returncode, log.strip()), (0, 'decision: continue'))
        self.assertEqual(seen, [('c84016', {0xE000: b'table'})])

    def test_raising_approval_aborts_the_worker_and_reaches_the_caller(self):
        def decline(probe, ranges):
            raise LookupError('foreign partition table')

        with self.assertRaisesRegex(LookupError, 'foreign partition table'):
            self._run(decline)


class ApiApprovalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.image = Path(self.directory.name) / 'app.bin'
        self.image.write_bytes(b'app')

    def tearDown(self):
        self.directory.cleanup()

    def test_without_approve_the_request_is_unchanged(self):
        with mock.patch('blflashcommand.api._request', return_value={'log': ''}) as request:
            flash_uart([FlashImage(self.image, 0x10000)], chip='bl616', port='/dev/null')
        self.assertNotIn('approve', request.call_args.kwargs)
        self.assertNotIn('inspect', request.call_args.kwargs)

    def test_inspect_requires_approve(self):
        with self.assertRaisesRegex(ValueError, 'inspect requires approve'):
            flash_uart([FlashImage(self.image, 0x10000)], chip='bl602', port='/dev/null',
                       inspect=[(0xE000, 0x2000)])


if __name__ == '__main__':
    unittest.main()
