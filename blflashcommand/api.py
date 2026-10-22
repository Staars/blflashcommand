"""Synchronous UART operations isolated from the legacy loader's global state."""

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Optional, Sequence, Union

from .profiles import resolve_chip

PathLike = Union[str, os.PathLike]


class FlashError(RuntimeError):
    """An operation failed or exceeded its timeout; log holds worker output."""

    def __init__(self, message: str, log: str = ''):
        super().__init__(message)
        self.log = log


@dataclass(frozen=True)
class FlashImage:
    """A source image and its byte offset in flash; source files stay unchanged."""

    path: PathLike
    address: int


@dataclass(frozen=True)
class FlashResult:
    log: str


@dataclass(frozen=True)
class ProbeResult:
    """Requested profile plus raw hardware evidence, without guessing chip identity.

    boot_info is the ROM's hexadecimal response when available. mac_address is
    the loader's efuse MAC, not a claim that BL616 and BL618 can be distinguished.
    jedec_id contains the three ID bytes; jedec_response retains the validity byte.
    """

    chip: str
    backend: str
    boot_info: str
    mac_address: str
    jedec_id: str
    jedec_response: str
    log: str


@dataclass(frozen=True)
class ReadResult:
    address: int
    data: bytes
    log: str
    probe: Optional[ProbeResult] = None


@dataclass(frozen=True)
class ResetResult:
    """The ROM acknowledged reset; application boot is not verified."""

    log: str


def _integer(value, name, minimum=0, maximum=0xffffffff):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer in {minimum}..{maximum}')
    return value


def _request(operation, chip, port, baudrate, timeout, **fields):
    profile = resolve_chip(chip)
    if not isinstance(port, str) or not port.strip():
        raise ValueError('port must be a nonempty serial device name')
    if baudrate is None:
        baudrate = 500000 if profile.backend == 'bl602' else 2000000
    _integer(baudrate, 'baudrate', 1)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be a finite positive number of seconds')
    request = dict(operation=operation, chip=profile.requested_name, port=port,
                   baudrate=baudrate, **fields)
    with tempfile.TemporaryDirectory(prefix='blflashcommand-') as temporary:
        root = Path(temporary)
        request_file = root / 'request.json'
        result_file = root / 'result.json'
        request_file.write_text(json.dumps(request), encoding='utf-8')
        # Source and editable installs must also resolve from an unrelated cwd.
        env = os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        import_root = str(Path(__file__).resolve().parent.parent)
        env['PYTHONPATH'] = import_root + os.pathsep + env.get('PYTHONPATH', '')
        try:
            process = subprocess.run(
                [sys.executable, '-m', 'blflashcommand._worker', str(request_file), str(result_file)],
                cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace', timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            output = error.stdout or b''
            if isinstance(output, bytes):
                output = output.decode('utf-8', errors='replace')
            raise FlashError(f'{operation} timed out after {timeout} seconds', output) from error
        log = process.stdout
        try:
            result = json.loads(result_file.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise FlashError(f'{operation} worker failed (exit {process.returncode})', log) from error
        if process.returncode or not result.get('ok'):
            raise FlashError(result.get('error', f'{operation} failed'), log)
        if operation == 'read':
            data = (root / 'read.bin').read_bytes()
            if len(data) != fields['length']:
                raise FlashError('Flash read returned an unexpected byte count', log)
            result['data'] = data
        result['log'] = log
        return result


def flash_uart(images: Sequence[FlashImage], *, chip: str, port: str,
               baudrate: Optional[int] = None, erase: bool = True, verify: bool = False,
               reset_after: bool = False, timeout: float = 1800) -> FlashResult:
    """Flash nonempty, nonoverlapping images through the selected UART profile.

    erase=True erases programmed regions using the existing loader. False assumes
    the caller has already erased them. No efuse writes or crypto are enabled.
    verify=True compares each input image's SHA-256 with bounded readback in
    the same loader session, before any requested reset. Input files stay unchanged.
    reset_after=True requests CPU reset in the same successful loader session
    and requires its acknowledgement; application boot is not verified.
    baudrate=None selects 500000 for BL602 and 2000000 for other profiles.
    """
    if not isinstance(erase, bool):
        raise ValueError('erase must be a boolean')
    if not isinstance(verify, bool):
        raise ValueError('verify must be a boolean')
    if not isinstance(reset_after, bool):
        raise ValueError('reset_after must be a boolean')
    entries = []
    for image in images:
        if not isinstance(image, FlashImage):
            raise TypeError('images must contain FlashImage instances')
        address = _integer(image.address, 'address')
        path = Path(image.path).expanduser().resolve(strict=True)
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'Image must be a nonempty regular file: {path}')
        end = address + path.stat().st_size
        if end > 0x100000000:
            raise ValueError('Image extends beyond the 32-bit flash address range')
        entries.append(dict(path=str(path), address=address, end=end))
    entries.sort(key=lambda entry: entry['address'])
    if not entries:
        raise ValueError('At least one flash image is required')
    if any(current['end'] > following['address'] for current, following in zip(entries, entries[1:])):
        raise ValueError('Flash image ranges overlap')
    result = _request('flash', chip, port, baudrate, timeout, images=entries,
                      erase=erase, verify=verify, reset_after=reset_after)
    return FlashResult(result['log'])


def probe_uart(*, chip: str, port: str, baudrate: Optional[int] = None,
               timeout: float = 120) -> ProbeResult:
    """Initialize the chosen profile and read ROM/MAC/JEDEC evidence.

    This may reset into download mode, load a RAM helper and configure flash.
    It does not program flash or efuses and does not autodetect a chip profile.
    baudrate=None selects 500000 for BL602 and 2000000 for other profiles.
    """
    result = _request('probe', chip, port, baudrate, timeout)
    return ProbeResult(**{name: result[name] for name in ProbeResult.__dataclass_fields__})


def read_flash_uart(address: int, length: int, *, chip: str, port: str,
                    baudrate: Optional[int] = None, include_probe: bool = False,
                    timeout: float = 600) -> ReadResult:
    """Read exactly length bytes from address using bounded transport chunks.

    include_probe=True captures JEDEC/MAC/boot evidence on the same UART session
    before reading; ReadResult.probe holds that evidence. No port reopen or reset
    occurs between probing and reading. The default leaves probe as None.
    baudrate=None selects 500000 for BL602 and 2000000 for other profiles.
    """
    _integer(address, 'address')
    _integer(length, 'length', 1)
    if not isinstance(include_probe, bool):
        raise ValueError('include_probe must be a boolean')
    if address + length > 0x100000000:
        raise ValueError('Read extends beyond the 32-bit flash address range')
    result = _request('read', chip, port, baudrate, timeout, address=address,
                      length=length, include_probe=include_probe)
    probe = ProbeResult(log=result['log'], **result['probe']) if include_probe else None
    return ReadResult(address, result['data'], result['log'], probe)


def reset_uart(*, chip: str, port: str, baudrate: int = 500000,
               timeout: float = 60) -> ResetResult:
    """Handshake with the ROM and request its existing reset command.

    Requires a reachable download-mode ROM and suitable boot straps. This does
    not promise application startup or implement board-specific GPIO controls.
    For a post-flash loader-session reset, use flash_uart(reset_after=True).
    """
    result = _request('reset', chip, port, baudrate, timeout)
    return ResetResult(result['log'])
