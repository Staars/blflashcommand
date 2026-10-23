"""Regression tests for the BL618 resource profile and flash timing."""

import configparser
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blflashcommand.profiles import resolve_chip  # noqa: E402


class Bl618ProfileTests(unittest.TestCase):
    def test_bl618_selects_its_own_resources_but_keeps_bl616_backend(self):
        profile = resolve_chip('BL618')
        self.assertEqual(profile.requested_name, 'bl618')
        self.assertEqual(profile.resource_name, 'bl618')
        self.assertEqual(profile.backend, 'bl616')
        self.assertEqual(profile.default_xtal, 'auto')

    def test_bl616_still_selects_bl616_resources(self):
        profile = resolve_chip('bl616')
        self.assertEqual(profile.resource_name, 'bl616')
        self.assertEqual(profile.backend, 'bl616')

    def test_bl618_dispatch_uses_bl616_loader_class(self):
        from libs.bflb_eflash_loader import BL616EflashLoader
        from libs.bflb_interface_eflash_loader import create_eflash_loader

        config = {'param': {}}
        loader = create_eflash_loader('bl618', mock.Mock(), config)
        self.assertIsInstance(loader, BL616EflashLoader)
        self.assertEqual(config['param']['chip_xtal'], 'auto')


class Bl618ConfigTests(unittest.TestCase):
    @staticmethod
    def read_config(relative):
        cfg = configparser.ConfigParser()
        cfg.read(ROOT / relative, encoding='utf-8')
        return cfg

    def test_bl618_uses_sdk_flash_clock_and_delay(self):
        cfg = self.read_config('chips/bl618/eflash_loader/eflash_loader_cfg.conf')
        self.assertEqual(cfg.get('LOAD_CFG', 'load_function'), '2')
        self.assertEqual(cfg.get('LOAD_CFG', 'speed_uart_boot'), '500000')
        self.assertEqual(cfg.get('LOAD_CFG', 'speed_uart_load'), '2000000')
        self.assertEqual(cfg.get('FLASH_CFG', 'flash_clock_cfg'), '0x43')
        self.assertEqual(cfg.get('FLASH_CFG', 'flash_clock_delay'), '1')
        self.assertEqual(cfg.get('FLASH_CFG', 'flash_io_mode'), '1')
        self.assertEqual(cfg.get('FLASH_CFG', 'flash_pin'), '0x80')
        self.assertEqual(cfg.get('FLASH_CFG', 'flash_para'),
                         'chips/bl618/efuse_bootheader/flash_para.bin')
        self.assertFalse(cfg.getboolean('FLASH_CFG', 'decompress_write'))

    def test_bl616_timing_is_unchanged(self):
        cfg = self.read_config('chips/bl616/eflash_loader/eflash_loader_cfg.conf')
        self.assertEqual(cfg.get('FLASH_CFG', 'flash_clock_cfg'), '0x41')
        self.assertEqual(cfg.get('FLASH_CFG', 'flash_clock_delay'), '0')
        self.assertEqual(cfg.get('FLASH_CFG', 'flash_para'),
                         'chips/bl616/efuse_bootheader/flash_para.bin')

    def test_bl618_flash_para_directory_exists(self):
        cfg = self.read_config('chips/bl618/eflash_loader/eflash_loader_cfg.conf')
        para = ROOT / cfg.get('FLASH_CFG', 'flash_para')
        self.assertTrue(para.parent.is_dir())
        self.assertTrue((para.parent / 'efuse_bootheader_cfg.conf').is_file())


class CliSelectionTests(unittest.TestCase):
    def test_cli_config_selects_bl618_resources(self):
        from argparse import Namespace
        from BLFlashCommand import MainClass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / 'firmware.bin'
            image.write_bytes(b'BL618 test image')
            config = root / 'flash_prog_cfg.ini'
            config.write_text(
                '[flash]\n'
                f'filedir = {image}\n'
                'address = 0x00000000\n',
                encoding='utf-8',
            )
            args = Namespace(
                interface='uart', port='COM1', chipname='bl618',
                baudrate=2000000, config=str(config), cpu_id='',
                efuse='', key='', iv='', pk=None, sk='',
            )
            with contextlib.redirect_stdout(io.StringIO()):
                parsed = MainClass().get_value(args)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed['param']['chip'], 'bl618')
        self.assertEqual(parsed['param']['chip_name'], 'bl618')
        self.assertEqual(parsed['param']['chip_type'], 'bl616')


class CliProbeTests(unittest.TestCase):
    def test_probe_option_prints_identity_without_flashing(self):
        from BLFlashCommand import MainClass
        from blflashcommand import ProbeResult

        result = ProbeResult(
            chip='bl618', backend='bl616', boot_info='01001606',
            mac_address='001122334455', jedec_id='c84017',
            jedec_response='c8401700',
            log='set flash cfg: 1014324\nchiptype: bl616\n',
        )
        output = io.StringIO()
        with mock.patch('blflashcommand.probe_uart', return_value=result) as probe:
            with contextlib.redirect_stdout(output):
                self.assertEqual(MainClass().main([
                    '--interface', 'uart', '--chipname', 'bl618',
                    '--port', '/dev/fake', '--baudrate', '2000000', '--probe',
                ]), 0)
        probe.assert_called_once_with(
            chip='bl618', port='/dev/fake', baudrate=2000000,
        )
        text = output.getvalue()
        self.assertIn('jedec_id: c84017', text)
        self.assertIn('set flash cfg: 1014324', text)

    def test_short_read_option_prints_hex_and_optional_probe(self):
        from BLFlashCommand import MainClass
        from blflashcommand import ProbeResult, ReadResult

        probe = ProbeResult(
            chip='bl618', backend='bl616', boot_info='01001606',
            mac_address='001122334455', jedec_id='c84017',
            jedec_response='c8401700', log='set flash cfg: 1014324\n',
        )
        read = ReadResult(0, bytes.fromhex('01020304'), 'log', probe)
        output = io.StringIO()
        with mock.patch('blflashcommand.read_flash_uart', return_value=read) as read_mock:
            with contextlib.redirect_stdout(output):
                self.assertEqual(MainClass().main([
                    '--interface', 'uart', '--chipname', 'bl618',
                    '--port', '/dev/fake', '--baudrate', '2000000',
                    '--read', '0x0:4', '--probe',
                ]), 0)
        read_mock.assert_called_once_with(
            0, 4, chip='bl618', port='/dev/fake', baudrate=2000000,
            include_probe=True,
        )
        text = output.getvalue()
        self.assertIn('address: 0x0', text)
        self.assertIn('01 02 03 04', text)
        self.assertIn('jedec_id: c84017', text)


class WorkspacePreparationTests(unittest.TestCase):
    def test_api_workspace_contains_bl618_configs(self):
        from blflashcommand import _worker

        original_cwd = os.getcwd()
        original_workspace = os.environ.get('BLFLASHCOMMAND_WORKSPACE')
        try:
            with tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                with contextlib.redirect_stdout(io.StringIO()):
                    _worker._prepare(workspace)
                self.assertTrue(
                    (workspace / 'chips/bl618/eflash_loader/eflash_loader_cfg.conf').is_file()
                )
                self.assertTrue(
                    (workspace / 'chips/bl618/efuse_bootheader/efuse_bootheader_cfg.conf').is_file()
                )
        finally:
            os.chdir(original_cwd)
            if original_workspace is None:
                os.environ.pop('BLFLASHCOMMAND_WORKSPACE', None)
            else:
                os.environ['BLFLASHCOMMAND_WORKSPACE'] = original_workspace


class HandshakeResetTests(unittest.TestCase):
    @staticmethod
    def make_loader():
        from libs.base.bflb_img_loader import BflbImgLoader

        loader = BflbImgLoader(
            '/dev/fake', 500000, 500000, interface='uart',
            chip_type='bl616', chip_name='bl616',
        )
        loader.bflb_serial_object = mock.Mock()
        return loader

    def test_bl616_syncs_without_toggling_rts_dtr_first(self):
        loader = self.make_loader()
        with mock.patch.object(loader, 'boot_install_cmds_callback'), \
                mock.patch.object(
                    loader, '_sync_without_reset', return_value='OK'
                ) as sync, \
                mock.patch.object(loader, 'toggle_boot_or_shake_hand') as reset:
            result = loader.img_load_shake_hand(
                500000, 500000, do_reset=True, reset_hold_time=50,
                shake_hand_delay=100, reset_revert=True, cutoff_time=50,
                shake_hand_retry=2,
            )

        self.assertEqual(result, 'OK')
        sync.assert_called_once_with(500000, wait=1.0, read_len=2)
        reset.assert_not_called()

    def test_bl616_falls_back_to_reset_when_direct_sync_fails(self):
        loader = self.make_loader()
        with mock.patch.object(loader, 'boot_install_cmds_callback'), \
                mock.patch.object(
                    loader, '_sync_without_reset', return_value='FL'
                ) as sync, \
                mock.patch.object(
                    loader, 'toggle_boot_or_shake_hand', return_value='OK'
                ) as reset:
            result = loader.img_load_shake_hand(
                500000, 500000, do_reset=True, reset_hold_time=50,
                shake_hand_delay=100, reset_revert=True, cutoff_time=50,
                shake_hand_retry=2,
            )

        self.assertEqual(result, 'OK')
        sync.assert_called_once_with(500000, wait=1.0, read_len=2)
        reset.assert_called_once()


class FlashConfigRetryTests(unittest.TestCase):
    def test_retry_uses_bl616_backend_not_bl618_resource_name(self):
        from libs.bflb_configobj import BFConfigParser
        from libs.bflb_eflash_loader import BL616EflashLoader

        class FakeCfg:
            def has_option(self, section, key):
                return True

            def set(self, section, key, value):
                pass

        loader = BL616EflashLoader('bl616', mock.Mock(), {'param': {}})
        loader.chip_name = 'bl618'
        loader.is_conf_exist = lambda flash_id: True
        loader.error_code_print = lambda code: None
        calls = []

        def fake_check(chip_type, flash_id, binfile, parafile):
            calls.append(chip_type)
            return False

        with mock.patch('libs.base.bflb_flash_select.flash_bootheader_config_check',
                        side_effect=fake_check):
            result = loader.flash_cfg_option(
                'c84017', '', 0, '00', 'image.bin', BFConfigParser(), FakeCfg(),
            )

        self.assertFalse(result)
        self.assertEqual(calls, ['bl616', 'bl616'])


if __name__ == '__main__':
    unittest.main()
