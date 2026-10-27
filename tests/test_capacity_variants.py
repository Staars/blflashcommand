"""Tests for flash images whose contents are chosen by the detected JEDEC capacity."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blflashcommand import FlashImage, flash_uart  # noqa: E402
from blflashcommand import _worker  # noqa: E402

MIB = 1024 * 1024


class FakeLoader:
    """Records what step 5 would write after step 4 stored the JEDEC ID."""

    def __init__(self, jedec_id, config):
        self.read_flash_id = jedec_id
        self.config = config
        self.written = None

    def fifth_run_step_write_flash_and_check(self):
        self.written = Path(self.config['image_0']['path']).read_bytes()
        return True, 'written'


class CapacityVariantTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.base = self.root / 'table.bin'
        self.base.write_bytes(b'base' * 32)

    def tearDown(self):
        self.directory.cleanup()

    def _variant(self, name):
        path = self.root / f'{name}.bin'
        path.write_bytes(name.encode().ljust(128, b'\0'))
        return path

    def _write(self, jedec_id, variants, minimum=None, verify=True):
        """Run the guarded write step on a staged copy, as the worker does."""
        staged = self.root / 'staged.bin'
        staged.write_bytes(self.base.read_bytes())
        config = {'param': {}, 'image_0': dict(path=str(staged), addr=hex(0xE000))}
        expected = _worker._expected_images(config) if verify else []
        options = {'image_0': [(capacity, path) for capacity, path in variants.items()]}
        loader = FakeLoader(jedec_id, config)
        _worker._guard_flash_capacity(loader, minimum, config, options, expected)
        loader.fifth_run_step_write_flash_and_check()
        return loader.written, expected

    def test_largest_variant_not_above_detected_capacity_is_written(self):
        variants = {4 * MIB: self._variant('four'), 16 * MIB: self._variant('sixteen')}
        for jedec, written_name in (('c84015', None), ('c84016', 'four'),
                                    ('c84017', 'four'), ('c84018', 'sixteen'),
                                    ('c84019', 'sixteen')):
            with self.subTest(jedec=jedec):
                written, expected = self._write(jedec, variants)
                source = self.base if written_name is None else variants[
                    4 * MIB if written_name == 'four' else 16 * MIB]
                self.assertEqual(written, source.read_bytes())
                self.assertEqual(expected[0][2], hashlib.sha256(written).digest())

    def test_minimum_capacity_is_still_enforced_with_variants(self):
        with self.assertRaisesRegex(RuntimeError, 'bytes are required'):
            self._write('c84015', {4 * MIB: self._variant('four')}, minimum=4 * MIB)

    def test_no_variants_keeps_the_previous_guard_behavior(self):
        written, _ = self._write('c84016', {}, minimum=2 * MIB)
        self.assertEqual(written, self.base.read_bytes())

    def test_api_rejects_variants_of_another_length_before_serial_access(self):
        wrong = self.root / 'wrong.bin'
        wrong.write_bytes(b'x' * 64)
        with mock.patch('blflashcommand.api._request') as request:
            with self.assertRaisesRegex(ValueError, 'image length'):
                flash_uart([FlashImage(self.base, 0xE000, {4 * MIB: wrong})],
                           chip='bl602', port='/dev/null')
            request.assert_not_called()

    def test_api_forwards_sorted_variants(self):
        four, eight = self._variant('four'), self._variant('eight')
        with mock.patch('blflashcommand.api._request', return_value={'log': ''}) as request:
            flash_uart([FlashImage(self.base, 0xE000, {8 * MIB: eight, 4 * MIB: four})],
                       chip='bl616', port='/dev/null')
        image = request.call_args.kwargs['images'][0]
        self.assertEqual([variant['capacity'] for variant in image['variants']], [4 * MIB, 8 * MIB])

    def test_api_forwards_no_variants_by_default(self):
        with mock.patch('blflashcommand.api._request', return_value={'log': ''}) as request:
            flash_uart([FlashImage(self.base, 0xE000)], chip='bl616', port='/dev/null')
        self.assertEqual(request.call_args.kwargs['images'][0]['variants'], [])


if __name__ == '__main__':
    unittest.main()
