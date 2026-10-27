"""Synchronous UART operations isolated from the legacy loader's global state."""

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence, Tuple, Union

from .profiles import resolve_chip

PathLike = Union[str, os.PathLike]


class FlashError(RuntimeError):
    """An operation failed or exceeded its timeout; log holds worker output."""

    def __init__(self, message: str, log: str = ''):
        super().__init__(message)
        self.log = log


@dataclass(frozen=True)
class FlashImage:
    """A source image and its byte offset in flash; source files stay unchanged.

    variants maps a flash capacity in bytes to an alternative file of the same length. The
    write session writes the variant with the largest capacity not above the JEDEC capacity
    it detects, or path when none qualifies.
    """

    path: PathLike
    address: int
    variants: Mapping[int, PathLike] = field(default_factory=dict)


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


def _inspection(root):
    """Load the worker's pre-write evidence and inspected ranges."""
    evidence = json.loads((root / 'inspect.json').read_text(encoding='utf-8'))
    probe = ProbeResult(log='', **evidence['probe'])
    ranges = {item['address']: (root / item['path']).read_bytes() for item in evidence['ranges']}
    return probe, ranges


# Must match the worker's prefix; these stdout lines are events, not log text.
EVENT_PREFIX = '@@blflashcommand-event '


def _run_streaming(command, root, env, timeout, operation, approve, on_event):
    """Run the worker, passing its events to on_event and answering its one approval."""
    env = dict(env if env is not None else os.environ, PYTHONUNBUFFERED='1')
    process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace', bufsize=1)
    expired = threading.Event()

    def expire():
        expired.set()
        process.kill()

    timer = threading.Timer(timeout, expire)
    timer.start()
    log = []
    declined = None
    try:
        for line in process.stdout:
            event = None
            if line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX):])
                except ValueError:
                    event = None
            if not isinstance(event, dict):
                log.append(line)
            elif event.get('event') == 'approve':
                decision = 'abort'
                if approve is not None:
                    try:
                        approve(*_inspection(root))
                        decision = 'continue'
                    except Exception as error:
                        declined = error
                try:
                    process.stdin.write(decision + '\n')
                    process.stdin.flush()
                except OSError:
                    pass
            elif on_event is not None:
                on_event(event)
        process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        try:
            process.stdin.close()
        except OSError:
            pass
    text = ''.join(log)
    if declined is not None:
        raise declined
    if expired.is_set():
        raise FlashError(f'{operation} timed out after {timeout} seconds', text)
    return process.returncode, text


def _streaming_fields(on_event, wait_seconds):
    """Validate event/wait options and return their request fields."""
    if on_event is not None and not callable(on_event):
        raise TypeError('on_event must be callable')
    fields = {}
    if on_event is not None:
        fields['events'] = True
    if wait_seconds is not None:
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float)) or \
                not math.isfinite(wait_seconds) or not 0 < wait_seconds <= 600:
            raise ValueError('wait_seconds must be a number of seconds in 0..600')
        fields['wait_seconds'] = wait_seconds
    return fields


def _request(operation, chip, port, baudrate, timeout, approve=None, on_event=None, **fields):
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
        command = [sys.executable, '-m', 'blflashcommand._worker', str(request_file), str(result_file)]
        if approve is not None or on_event is not None:
            returncode, log = _run_streaming(command, root, env, timeout, operation, approve,
                                             on_event)
        else:
            try:
                process = subprocess.run(
                    command,
                    cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace', timeout=timeout,
                )
            except subprocess.TimeoutExpired as error:
                output = error.stdout or b''
                if isinstance(output, bytes):
                    output = output.decode('utf-8', errors='replace')
                raise FlashError(f'{operation} timed out after {timeout} seconds', output) from error
            returncode, log = process.returncode, process.stdout
        try:
            result = json.loads(result_file.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise FlashError(f'{operation} worker failed (exit {returncode})', log) from error
        if returncode or not result.get('ok'):
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
               reset_after: bool = False, minimum_flash_bytes: Optional[int] = None,
               inspect: Sequence[Tuple[int, int]] = (),
               approve: Optional[Callable[['ProbeResult', Mapping[int, bytes]], None]] = None,
               on_event: Optional[Callable[[dict], None]] = None,
               wait_seconds: Optional[float] = None,
               timeout: float = 1800) -> FlashResult:
    """Flash nonempty, nonoverlapping images through the selected UART profile.

    erase=True erases programmed regions using the existing loader. False assumes
    the caller has already erased them. No efuse writes or crypto are enabled.
    verify=True compares each input image's SHA-256 with bounded readback in
    the same loader session, before any requested reset. Input files stay unchanged.
    reset_after=True requests CPU reset in the same successful loader session
    and requires its acknowledgement; application boot is not verified.
    minimum_flash_bytes requires at least that much JEDEC capacity before any
    erase/write, in the same loader session used for writing and verification.
    FlashImage.variants are selected by that same JEDEC capacity, and verification
    compares the variant actually written.
    approve, when given, is called once in that session after the JEDEC check and before
    the first erase, with the probe evidence and the bytes of each (address, length) in
    inspect. Raising from it declines the write: nothing is erased and the exception is
    re-raised to the caller. Without approve, the session runs as before.
    on_event and wait_seconds are described in the module README; without them the
    session runs as before.
    Images must fit the required span before serial access and the detected
    capacity before writing. None omits this minimum-capacity check.
    baudrate=None selects 500000 for BL602 and 2000000 for other profiles.
    """
    if minimum_flash_bytes is not None:
        _integer(minimum_flash_bytes, 'minimum_flash_bytes', 1, 0x80000000)
    if approve is not None and not callable(approve):
        raise TypeError('approve must be callable')
    if inspect and approve is None:
        raise ValueError('inspect requires approve')
    ranges = []
    for item in inspect:
        address, length = item
        _integer(address, 'inspect address')
        _integer(length, 'inspect length', 1, 0x100000)
        if address + length > 0x100000000:
            raise ValueError('Inspected range extends beyond the 32-bit flash address range')
        ranges.append([address, length])
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
        if minimum_flash_bytes is not None and end > minimum_flash_bytes:
            raise ValueError('Image extends beyond the required flash span')
        if not isinstance(image.variants, Mapping):
            raise TypeError('variants must map flash capacities to image paths')
        variants = []
        for capacity, variant_path in sorted(image.variants.items()):
            _integer(capacity, 'variant capacity', 1, 0x100000000)
            if end > capacity:
                raise ValueError('Image extends beyond its variant capacity')
            variant = Path(variant_path).expanduser().resolve(strict=True)
            if not variant.is_file() or variant.stat().st_size != path.stat().st_size:
                raise ValueError(f'Variant must be a regular file of the image length: {variant}')
            variants.append(dict(capacity=capacity, path=str(variant)))
        entries.append(dict(path=str(path), address=address, end=end, variants=variants))
    entries.sort(key=lambda entry: entry['address'])
    if not entries:
        raise ValueError('At least one flash image is required')
    if any(current['end'] > following['address'] for current, following in zip(entries, entries[1:])):
        raise ValueError('Flash image ranges overlap')
    fields = dict(images=entries, erase=erase, verify=verify, reset_after=reset_after,
                  minimum_flash_bytes=minimum_flash_bytes,
                  **_streaming_fields(on_event, wait_seconds))
    if approve is not None:
        fields.update(approve=approve, inspect=ranges)
    if on_event is not None:
        fields['on_event'] = on_event
    result = _request('flash', chip, port, baudrate, timeout, **fields)
    return FlashResult(result['log'])


def probe_uart(*, chip: str, port: str, baudrate: Optional[int] = None,
               on_event: Optional[Callable[[dict], None]] = None,
               wait_seconds: Optional[float] = None,
               timeout: float = 120) -> ProbeResult:
    """Initialize the chosen profile and read ROM/MAC/JEDEC evidence.

    This may reset into download mode, load a RAM helper and configure flash.
    It does not program flash or efuses and does not autodetect a chip profile.
    baudrate=None selects 500000 for BL602 and 2000000 for other profiles.
    """
    result = _request('probe', chip, port, baudrate, timeout, on_event=on_event,
                      **_streaming_fields(on_event, wait_seconds))
    return ProbeResult(**{name: result[name] for name in ProbeResult.__dataclass_fields__})


def read_flash_uart(address: int, length: int, *, chip: str, port: str,
                    baudrate: Optional[int] = None, include_probe: bool = False,
                    on_event: Optional[Callable[[dict], None]] = None,
                    wait_seconds: Optional[float] = None,
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
                      length=length, include_probe=include_probe, on_event=on_event,
                      **_streaming_fields(on_event, wait_seconds))
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
