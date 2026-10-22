"""Private subprocess bridge to the legacy implementation; not a public API."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def _legacy_root():
    packaged = Path(__file__).resolve().parent / '_legacy'
    return packaged if packaged.is_dir() else Path(__file__).resolve().parent.parent


def _prepare(workspace):
    """Copy only runtime inputs, excluding generated images and user artifacts."""
    source = _legacy_root()
    patterns = (
        'chips/*/eflash_loader/*.conf',
        'chips/*/eflash_loader/eflash_loader_*m.bin',
        'chips/*/efuse_bootheader/*.conf',
        'utils/flash/*/*.conf',
        'utils/flash/*/*.csv',
    )
    for pattern in patterns:
        for path in source.glob(pattern):
            target = workspace / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    os.environ['BLFLASHCOMMAND_WORKSPACE'] = str(workspace)
    sys.path.insert(0, str(source))
    os.chdir(workspace)


def _config(request, workspace):
    from blflashcommand.profiles import resolve_chip

    profile = resolve_chip(request['chip'])
    config = {
        'param': dict(interface_type='uart', comport_uart=request['port'],
                      chip=profile.requested_name, chip_name=profile.resource_name,
                      chip_type=profile.backend, speed_uart=str(request['baudrate']),
                      speed_jlink='1000', erase='1' if request.get('erase', False) else '0',
                      skip_mode='0x0, 0x0', boot2_isp_mode='0', aes_key='', aes_iv=''),
        'check_box': dict(efuse=False, encrypt=False, sign=False),
        'input_path': dict(efuse='', publickey='', privatekey=''),
    }
    for index, image in enumerate(request.get('images', [])):
        directory = workspace / 'chips' / profile.resource_name / 'img_create'
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f'image_{index}.bin'
        shutil.copyfile(image['path'], target)
        config[f'image_{index}'] = dict(path=str(target), addr=hex(image['address']))
    return config


def _hex(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ''
    # The ROM loader stores boot information as ASCII hexadecimal bytes.
    if isinstance(value, bytes):
        try:
            text = value.decode('ascii')
            bytes.fromhex(text)
            return text
        except (UnicodeDecodeError, ValueError):
            pass
    return bytes(value).hex()


def _operation(request, workspace):
    from libs.bflb_interface_eflash_loader import create_eflash_loader
    from libs.bflb_utils import clear_global, eflash_loader_parser_init, hexstr_to_bytearray

    clear_global()
    config = _config(request, workspace)
    operation = request['operation']
    if operation == 'reset':
        from libs.base.bflb_img_loader import BflbImgLoader
        image_loader = BflbImgLoader(request['port'], request['baudrate'],
                                    request['baudrate'], 'uart',
                                    config['param']['chip_type'], config['param']['chip_name'])
        try:
            result, _ = image_loader.img_get_bootinfo(
                request['baudrate'], request['baudrate'], do_reset=True,
                reset_hold_time=5, shake_hand_delay=100, reset_revert=False,
                cutoff_time=100, boot_load=True,
            )
            if result is not True or image_loader.img_loader_reset_cpu() is not True:
                raise RuntimeError('ROM reset was not acknowledged')
            return {}
        finally:
            image_loader.bflb_serial_object.close()
    options = ['--write', '--flash'] if operation == 'flash' else ['--none', '--flash']
    args = eflash_loader_parser_init().parse_args(options)
    loader = create_eflash_loader(config['param']['chip_type'], args, config)
    try:
        result, detail = loader.run_step()
        if result is not True:
            raise RuntimeError(f'Loader initialization or operation failed: {detail}')
        if operation == 'flash':
            if request.get('reset_after', False):
                # Use the same reset opcode/path as the existing chip loaders,
                # while this UART session still owns the running RAM/ROM loader.
                command = hexstr_to_bytearray(loader._com_cmds['reset']['cmd_id'])
                status, _ = loader.com_process_one_cmd('reset', command, bytearray(0))
                if not isinstance(status, str) or not status.startswith('OK'):
                    raise RuntimeError('Flash succeeded but loader CPU reset was not acknowledged')
            return {}
        if operation == 'probe':
            result, response = loader.flash_read_jedec_id_process()
            if result is not True or response is None or len(response) < 3:
                raise RuntimeError('JEDEC probe failed')
            return dict(chip=request['chip'], backend=config['param']['chip_type'],
                        boot_info=_hex(loader.bootinfo), mac_address=bytes(loader._mac_addr).hex(),
                        jedec_id=bytes(response[:3]).hex(), jedec_response=bytes(response).hex())
        if operation == 'read':
            # The inherited helper only advances when a call fits its payload.
            # Bound every call here and reject partial reads explicitly.
            maximum = loader._bflb_com_tx_size - 8
            if maximum <= 0:
                raise RuntimeError('Invalid loader read payload size')
            remaining = request['length']
            address = request['address']
            with (workspace.parent / 'read.bin').open('wb') as output:
                while remaining:
                    length = min(remaining, maximum)
                    result, data = loader.flash_read_main_process(address, length)
                    if result is not True or data is None or len(data) != length:
                        raise RuntimeError(f'Incomplete flash read at {address:#x}')
                    output.write(data)
                    address += length
                    remaining -= length
            return {}
        raise ValueError('Unknown operation: ' + operation)
    finally:
        loader.close_serial()


def _cli(arguments):
    # Convert user paths before changing cwd to the isolated resource workspace.
    paths = {'--config', '--efuse', '--pk', '--sk'}
    arguments = list(arguments)
    for index, argument in enumerate(arguments):
        key, separator, value = argument.partition('=')
        if key in paths:
            if separator and value:
                arguments[index] = key + '=' + str(Path(value).resolve())
            elif not separator and index + 1 < len(arguments) and arguments[index + 1]:
                arguments[index + 1] = str(Path(arguments[index + 1]).resolve())
    with tempfile.TemporaryDirectory(prefix='blflashcommand-cli-') as temporary:
        workspace = Path(temporary)
        _prepare(workspace)
        from BLFlashCommand import MainClass

        app = MainClass()
        original = app.get_value

        def copied_config(args):
            config = original(args)
            if config:
                directory = workspace / 'inputs'
                directory.mkdir()
                for index, value in enumerate(config.values()):
                    if isinstance(value, dict) and 'addr' in value and 'path' in value:
                        target = directory / f'{index}_{Path(value["path"]).name}'
                        shutil.copyfile(value['path'], target)
                        value['path'] = str(target)
            return config

        app.get_value = copied_config
        sys.argv[0] = 'blflashcommand'
        return app.main(arguments)


def main():
    if sys.argv[1:2] == ['--cli']:
        return _cli(sys.argv[2:])
    request_file, result_file = map(Path, sys.argv[1:])
    request = json.loads(request_file.read_text(encoding='utf-8'))
    workspace = request_file.parent / 'workspace'
    workspace.mkdir()
    try:
        _prepare(workspace)
        result = dict(ok=True, **_operation(request, workspace))
    except Exception as error:
        result = dict(ok=False, error=f'{type(error).__name__}: {error}')
    result_file.write_text(json.dumps(result), encoding='utf-8')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
