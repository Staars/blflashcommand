"""Private subprocess bridge to the legacy implementation; not a public API."""

import hashlib
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


def _stage_variants(request, workspace):
    """Copy capacity variants beside their staged images; config keys map to options."""
    from blflashcommand.profiles import resolve_chip

    directory = workspace / 'chips' / resolve_chip(request['chip']).resource_name / 'variants'
    variants = {}
    for index, image in enumerate(request.get('images', [])):
        options = []
        for variant in image.get('variants', []):
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f'image_{index}_{variant["capacity"]}.bin'
            shutil.copyfile(variant['path'], target)
            options.append((variant['capacity'], target))
        if options:
            variants[f'image_{index}'] = options
    return variants


def _select_variants(config, variants, flash_bytes, expected):
    """Stage the largest variant not above the detected capacity over its base copy."""
    for key, options in variants.items():
        fitting = [option for option in options if option[0] <= flash_bytes]
        if not fitting:
            continue
        capacity, source = max(fitting)
        image = config[key]
        shutil.copyfile(source, image['path'])
        address = int(image['addr'], 16)
        print(f'Selected {capacity}-byte capacity variant for image at {address:#x}')
        for index, (expected_address, length, _) in enumerate(expected):
            if expected_address == address:
                expected[index] = (address, length, hashlib.sha256(source.read_bytes()).digest())


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


def _probe_info(loader, chip, backend):
    """Capture chip/flash evidence on the caller's active loader session."""
    result, response = loader.flash_read_jedec_id_process()
    if result is not True or response is None or len(response) < 3:
        raise RuntimeError('JEDEC probe failed')
    return dict(chip=chip, backend=backend, boot_info=_hex(loader.bootinfo),
                mac_address=bytes(loader._mac_addr).hex(),
                jedec_id=bytes(response[:3]).hex(), jedec_response=bytes(response).hex())


def _await_approval(loader, request, config, root):
    """Publish probe evidence and inspected ranges, then wait for the caller's decision."""
    evidence = _probe_info(loader, request['chip'], config['param']['chip_type'])
    ranges = []
    for index, (address, length) in enumerate(request['inspect']):
        name = f'inspect_{index}.bin'
        with (root / name).open('wb') as output:
            for data in _read_chunks(loader, address, length):
                output.write(data)
        ranges.append(dict(address=address, path=name))
    staged = root / 'inspect.tmp'
    staged.write_text(json.dumps(dict(probe=evidence, ranges=ranges)), encoding='utf-8')
    staged.replace(root / 'inspect.json')
    if sys.stdin.readline().strip() != 'continue':
        raise RuntimeError('Write declined by the caller before the first erase')


def _guard_flash_capacity(loader, minimum_flash_bytes, config, variants=None, expected=None,
                          approval=None):
    """Check step 4's JEDEC evidence immediately before step 5 can erase/write.

    Capacity variants and the caller's approval run here, so the session that writes
    also measures and inspects.
    """
    write_step = loader.fifth_run_step_write_flash_and_check

    def checked_write_step():
        try:
            jedec_id = bytes.fromhex(loader.read_flash_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise RuntimeError('Missing or invalid JEDEC evidence before flash write') from error
        if len(jedec_id) != 3 or not 16 <= jedec_id[2] <= 31:
            raise RuntimeError('Unsupported JEDEC capacity before flash write')
        flash_bytes = 1 << jedec_id[2]
        if minimum_flash_bytes is not None and flash_bytes < minimum_flash_bytes:
            raise RuntimeError(
                f'JEDEC reports {flash_bytes} bytes of flash, '
                f'but {minimum_flash_bytes} bytes are required'
            )
        _select_variants(config, variants or {}, flash_bytes, expected if expected is not None else [])
        for image in config.values():
            if not isinstance(image, dict) or 'addr' not in image or 'path' not in image:
                continue
            end = int(image['addr'], 16) + Path(image['path']).stat().st_size
            if (minimum_flash_bytes is not None and end > minimum_flash_bytes) or end > flash_bytes:
                raise RuntimeError('Image extends beyond required or detected flash bounds')
        print(f'JEDEC capacity checked before write: {jedec_id.hex()} ({flash_bytes} bytes)')
        if approval is not None:
            approval()
        return write_step()

    # Keep the chip-specific run_step order, UART object and baud configuration.
    # Its step 4 reads JEDEC; step 5 owns the first possible erase/write.
    loader.fifth_run_step_write_flash_and_check = checked_write_step


def _read_chunks(loader, address, length):
    """Bound calls to the inherited helper and reject incomplete responses."""
    maximum = loader._bflb_com_tx_size - 8
    if maximum <= 0:
        raise RuntimeError('Invalid loader read payload size')
    while length:
        count = min(length, maximum)
        result, data = loader.flash_read_main_process(address, count)
        if result is not True or data is None or len(data) != count:
            raise RuntimeError(f'Incomplete flash read at {address:#x}')
        yield data
        address += count
        length -= count


def _expected_images(config):
    """Hash the requested copies before the backend can patch staged images."""
    images = []
    for value in config.values():
        if not isinstance(value, dict) or 'addr' not in value or 'path' not in value:
            continue
        digest = hashlib.sha256()
        length = 0
        with open(value['path'], 'rb') as image:
            for data in iter(lambda: image.read(1024 * 1024), b''):
                digest.update(data)
                length += len(data)
        images.append((int(value['addr'], 16), length, digest.digest()))
    return images


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
        expected = _expected_images(config) if operation == 'flash' and request.get('verify', False) else []
        variants = _stage_variants(request, workspace) if operation == 'flash' else {}
        approval = None
        if operation == 'flash' and 'inspect' in request:
            def approval():
                _await_approval(loader, request, config, workspace.parent)
        if operation == 'flash' and (request.get('minimum_flash_bytes') is not None or variants
                                     or approval is not None):
            _guard_flash_capacity(loader, request.get('minimum_flash_bytes'), config, variants,
                                  expected, approval)
        result, detail = loader.run_step()
        if result is not True:
            raise RuntimeError(f'Loader initialization or operation failed: {detail}')
        if operation == 'flash':
            for address, length, expected_digest in expected:
                digest = hashlib.sha256()
                for data in _read_chunks(loader, address, length):
                    digest.update(data)
                if digest.digest() != expected_digest:
                    raise RuntimeError(f'Flash readback differs from requested image at {address:#x}')
            if request.get('reset_after', False):
                # Use the same reset opcode/path as the existing chip loaders,
                # while this UART session still owns the running RAM/ROM loader.
                command = hexstr_to_bytearray(loader._com_cmds['reset']['cmd_id'])
                status, _ = loader.com_process_one_cmd('reset', command, bytearray(0))
                if not isinstance(status, str) or not status.startswith('OK'):
                    raise RuntimeError('Flash succeeded but loader CPU reset was not acknowledged')
            return {}
        if operation == 'probe':
            return _probe_info(loader, request['chip'], config['param']['chip_type'])
        if operation == 'read':
            evidence = {}
            if request.get('include_probe', False):
                evidence['probe'] = _probe_info(loader, request['chip'], config['param']['chip_type'])
            with (workspace.parent / 'read.bin').open('wb') as output:
                for data in _read_chunks(loader, request['address'], request['length']):
                    output.write(data)
            return evidence
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
