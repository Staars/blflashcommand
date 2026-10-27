# Bouffalo Lab Flash Command

A standalone Python command-line utility for flashing Bouffalo Lab microcontrollers
through UART, with optional debug probe transports and image cryptography.
Python 3.9 or later is required. Ordinary UART flashing requires only PySerial.

## Quick start

From this checkout, install dependencies with [uv](https://docs.astral.sh/uv/):

```sh
uv sync
cp flash_prog_cfg.ini.example flash_prog_cfg.ini
```

Edit `flash_prog_cfg.ini` to select your firmware files and the addresses required
by your board's flash layout, then run:

```sh
uv run python BLFlashCommand.py --interface uart --chipname bl616 \
    --port /dev/ttyACM0 --baudrate 2000000 --config flash_prog_cfg.ini
```

`BLFlashCommand.py` also has a shebang and PEP 723 inline metadata, so these
equivalent forms are available:

```sh
./BLFlashCommand.py --interface uart --chipname bl616 \
    --port /dev/ttyACM0 --baudrate 2000000 --config flash_prog_cfg.ini

uv run BLFlashCommand.py --interface uart --chipname bl616 \
    --port /dev/ttyACM0 --baudrate 2000000 --config flash_prog_cfg.ini
```

Set the port, chip and baud rate for your board, and enter its download mode before
flashing. Run `./BLFlashCommand.py --help` for CLI options.

## BL618 quick check

BL618 cannot be reliably distinguished from BL616 by the boot ROM, so select it
explicitly. With the board in download mode, probe it first without writing:

```sh
uv run python - <<'PY'
from blflashcommand import probe_uart, read_flash_uart

port = "/dev/cu.usbserial-XXXX"  # use your actual serial device
info = probe_uart(chip="bl618", port=port, baudrate=2000000)
print(info)
# Use the flash size reported by probe: 0x400000 for 4 MB, 0x800000 for 8 MB.
backup = read_flash_uart(0, 0x800000, chip="bl618", port=port, baudrate=2000000)
with open("bl618-backup-8m.bin", "wb") as output:
    output.write(backup.data)
print("wrote", len(backup.data), "bytes")
PY
```

The ROM handshake uses `speed_uart_boot` from the selected profile, which is
500 kbaud for BL618. `--baudrate` selects the later loader transfer rate; it does
not change the handshake speed. If transfers at 2 Mbaud are unreliable, retry
with `--baudrate 500000` before changing anything else. A probe verifies the UART
protocol and flash access; it does not prove the SoC is a BL618, because BL616 and
BL618 share the boot ROM marker on some units.

To write a complete image at `0x00000000`, point a config at it:

```ini
[flash]
filedir = /absolute/path/to/bl618-full.bin
address = 0x00000000
```

```sh
uv run python BLFlashCommand.py --interface uart --chipname bl618 \
    --port /dev/cu.usbserial-XXXX --baudrate 2000000 \
    --config flash_prog_cfg.ini
```

The same CLI also supports a read-only probe and a short read, without a config
file:

```sh
./BLFlashCommand.py --interface uart --chipname bl618 \
    --port /dev/cu.usbserial-XXXX --baudrate 2000000 --probe

./BLFlashCommand.py --interface uart --chipname bl618 \
    --port /dev/cu.usbserial-XXXX --baudrate 2000000 --read 0x0:16
```

`--probe` reads boot information, MAC and JEDEC ID. `--read ADDR:LENGTH`
prints the requested bytes as hex. Combining `--read 0x0:16 --probe` captures
both in one session. These options do not write flash. The installed
`blflashcommand` command accepts the same arguments.

The log should show `set flash cfg: 10143...` (the last two digits are the flash
pin read from boot information) and `load_function` handling for the bootrom path.
The loader also prints `chiptype: bl616`: that is the protocol backend, not a
fallback from BL618. The selected configuration path is
`chips/bl618/eflash_loader/eflash_loader_cfg.ini`. A full image is required: this
tool does not build BL618 boot2, partition, app or DTS images. Use the web
flasher for image assembly if needed.

For pip, create and activate a virtual environment, then install
`python -m pip install -r requirements.txt` and run `python BLFlashCommand.py ...`.
The required dependency is `pyserial==3.5`; `uv.lock` also records the optional
packages listed below.

## Installable package and Python API

Install this project with `python -m pip install .` or `uv pip install .`.
The installed `blflashcommand` command and `python -m blflashcommand` accept the
same arguments as `python BLFlashCommand.py`; the source script remains available.
Wheels and source distributions include the UART loader binaries and configuration
templates, excluding generated images, live INI files and local firmware artifacts.

```python
from blflashcommand import (
    FlashError, FlashImage, flash_uart, probe_uart, read_flash_uart, reset_uart,
)

options = dict(chip="bl602", port="/dev/ttyUSB0", baudrate=500000)
try:
    info = probe_uart(**options)
    print(info.jedec_id, info.mac_address)
    backup = read_flash_uart(0, 0x200000, **options)
    with open("backup.bin", "wb") as output:
        output.write(backup.data)
    # Set offsets for the actual board/image layout before flashing.
    written = flash_uart(
        [FlashImage("factory.bin", 0)], verify=True, reset_after=True, **options,
    )
except FlashError as error:
    print(error, error.log)
```

All operations are synchronous and accept `chip`, `port`, `baudrate` and a timeout
in seconds. `flash_uart` accepts one or more nonempty, nonoverlapping images and
returns `FlashResult(log)`. Its `erase=True` default erases programmed regions;
`erase=False` assumes they were already erased. `reset_after=False` is the default;
set it to `True` to request CPU reset before closing the successful flash session.
The existing loader reset command must acknowledge the request; application boot
is not verified.

`verify=False` is the default. With `verify=True`, each image's SHA-256 is captured
from its private input copy before writing and compared with bounded flash reads
on that same active loader session. The order is write, verify all requested image
ranges, then optional reset. Incomplete or mismatched readback raises `FlashError`
and prevents reset. Input files remain unchanged. Verification uses the requested
image bytes, so backend image patching that changes those bytes causes a mismatch.

`FlashImage(path, address, variants={capacity: path})` lets the write session choose
an image by flash size, for example a partition table whose last entry reaches the
end of a larger chip. Each variant has the image's length. After the loader reads
JEDEC, and before the first erase or write in that same session, the variant with
the largest capacity not above the detected capacity replaces the image; with none,
the image itself is written. Verification compares the bytes actually written. An
image without variants behaves exactly as before.

`flash_uart(..., inspect=[(address, length)], approve=callback)` lets the caller
check the device inside the write session instead of a separate probe session, so
a board whose loader does not survive a port reopen needs only one handshake. After
the JEDEC check and before the first erase, `callback(probe, ranges)` receives the
`ProbeResult` and a `{address: bytes}` map of the inspected ranges. Returning lets
the write continue; raising declines it, nothing is erased, and the same exception
reaches the caller. Without `approve`, the session runs exactly as before.

`flash_uart`, `probe_uart` and `read_flash_uart` accept `on_event=callback` and
`wait_seconds=N`. `callback(event)` receives dictionaries while the session runs:
`connecting` before the first ROM handshake, `waiting` with `remaining` seconds after
each failed handshake, `connected`, then `write` (`address`, `done`, `total` bytes;
`done=0, total=0` when an image starts), `written` (`address`), `verify` and `read`
(`done`, `total`). Event lines are removed from `log`. `wait_seconds` (up to 600)
repeats a failed initial ROM handshake until that time has passed, so a user can
enter download mode by hand; a first successful handshake is not delayed. Without
either option, the session runs exactly as before.

`read_flash_uart(address, length, ...)` returns `ReadResult(address, data, log, probe)`
with raw `bytes`, using bounded reads and rejecting incomplete responses. Set
`include_probe=True` to capture JEDEC/MAC/boot evidence in `result.probe` before
reading on that same UART session. It defaults to `False`, with `probe=None`.

`probe_uart` returns `ProbeResult(chip, backend, boot_info, mac_address, jedec_id,
jedec_response, log)`. Hexadecimal evidence is returned as strings. `chip` is the
requested profile, not an autodetected identity; the API does not distinguish
BL616 from BL618 because they share the protocol, so select the profile
explicitly. `reset_uart` uses the existing ROM reset command and returns
`ResetResult(log)` after acknowledgement. It requires
a reachable download-mode ROM and suitable boot straps; application boot is not
verified. It is a ROM-only operation; use `flash_uart(reset_after=True)` to reset
from the active RAM/ROM loader session after writing. Probe/read can reset into
download mode, load a helper into RAM and
configure flash, without programming flash or efuses.

The Python API defaults to 500 kbaud for BL602 and 2 Mbaud for other profiles;
reset defaults to 500 kbaud. Pass an explicit baud rate for the target board.
A firmware console's baud rate is independent of the flashing baud rate. BL602,
BL616 and BL618 first try UART sync without changing RTS/DTR, allowing consecutive
operations to reuse an already running loader and matching the web flasher's
behavior on generic USB-UART adapters. If the chip does not answer,
initialization falls back to the configured reset sequence. Invalid arguments
raise `ValueError`, `TypeError` or a filesystem exception before serial access;
operation failures and timeouts raise `FlashError` with captured output in `log`.

Importing `blflashcommand` opens no port and imports no legacy transport modules.
Each operation runs in a private subprocess and temporary workspace so the
legacy backend can modify its own configs and image copies without changing
caller files or the installed package. Do not run simultaneous operations on
the same serial device. The public API currently covers unencrypted UART
operations; crypto and debug probe transport options remain available through the CLI.
This packaging/API has no new hardware qualification; the evidence below applies.

## Chip selection and evidence

Chip names are case-insensitive. The requested name identifies firmware files;
the backend and resource directory select the existing flashing implementation.

| `--chipname` | Backend | Resources | Default crystal |
| --- | --- | --- | --- |
| BL602 | BL602 | `chips/bl602` | 40 MHz |
| BL702 | BL702 | `chips/bl702` | 32 MHz |
| BL702L | BL702L | `chips/bl702l` | 32 MHz |
| BL808 | BL808 | `chips/bl808` | auto |
| BL606P | BL808 | `chips/bl606p` | auto |
| BL616 | BL616 | `chips/bl616` | auto |
| BL618 | BL616 | `chips/bl618` | auto |

BL618 keeps the BL616 loader and bootrom protocol, so no BL618 eflash-loader
binary is needed (`load_function = 2`). It has its own `chips/bl618` configuration
because the Bouffalo SDK uses different flash timing for BL618:
`flash_clock_cfg = 0x43` and `flash_clock_delay = 1`, versus `0x41`/`0` for BL616.
The two chips share the same `flash_para.bin` contents, but the BL618 profile
generates its own copy under `chips/bl618/efuse_bootheader/`.

The upstream project reported BL616 flashing on Linux. Hardware qualification of
this standalone CLI in the current checkout remains open. BL602 board activity
has been reported, but does not establish qualification of this Python flashing
path. BL618 has a BL618-specific timing profile now, but remains unqualified on
hardware; BL702 has not been tested and no board is available. Flashing, readback,
boot and recovery need qualification with the intended board, images and layout.

## Flash configuration

`--config` is required and must name a readable INI file with at least one image
section. Each image section needs a nonempty `filedir` and hexadecimal `address`.
The following address is illustrative; use your board's declared image layout.

```ini
[cfg]
erase = 1
skip_mode = 0x0, 0x0
boot2_isp_mode = 0

[app]
filedir = firmware_$(CHIPNAME).bin
address = 0x00010000
```

Relative paths resolve beside the config file. `$(CHIPNAME)` expands to the
requested lowercase name: BL618 selects `firmware_bl618.bin`, while
`--chipname bl618 --cpu_id foo` selects `firmware_bl618_foo.bin`. BL618 uses the
`chips/bl618` configuration while retaining the BL616 loader backend.

This is a raw image writer. It does not assemble a BL618 boot2, partition table,
application or factory DTS image. Supply a complete flash image at `0x00000000`,
or provide each component and its address. If you use the web flasher to build a
complete BL618 image, write that image at `0x00000000`.

Use one section per file for bootloader, partition and application images. A
complete flash dump starts at `0x00000000`; app-only offsets depend on the layout.
See [flash_prog_cfg.ini.example](flash_prog_cfg.ini.example) for more examples.
Invalid configs and reported flashing failures produce a nonzero exit status.

## Optional features

Install an extra with `uv sync --extra <name>`; repeat `--extra` to combine
features. For pip environments, install the packages in the table. Dependencies
load when the corresponding feature is requested, and missing packages produce
an error with uv and pip installation commands.

| Extra | Feature | Additional packages and requirements |
| --- | --- | --- |
| `aes` | AES image encryption and data decryption | `pycryptodome>=3.20` |
| `ecc` | ECC signing and ECDH authentication | `ecdsa==0.18.0`, `pycryptodome>=3.20`; operation-specific PEM keys |
| `xts` | AES-XTS image encryption for BL616/BL618, BL808 and WB03 helpers | `pycryptoplus==0.0.1`; legacy implementation, host/Python compatibility unqualified |
| `jlink` | J-Link transport | `pylink-square==0.5.0`; compatible SEGGER J-Link software/library |
| `cklink` | CKLink transport | `pycklink==0.1.1`; compatible CKLink native library |
| `openocd` | OpenOCD on Python 3.13 or later | `telnetlib-313-and-up==3.13.1`; older Python supplies `telnetlib`; OpenOCD executable/configuration required |

For example:

```sh
uv sync --extra ecc
# Or, inside a pip virtual environment:
python -m pip install 'ecdsa==0.18.0' 'pycryptodome>=3.20'
```

BL618 uses the BL616 AES-XTS helper. Optional packages can have transitive
dependencies; debug probe native libraries are unnecessary for UART operation.

## Troubleshooting and limitations

- If the port is missing or the handshake fails, check the selected serial device,
  access permissions, download mode and baud rate. For BL616/BL618, hold
  BOOT/IO28 high while resetting to enter the boot ROM; the CLI first attempts a
  sync without toggling RTS/DTR, but a cold start still requires download mode.
  A working firmware console does not establish that the ROM loader is ready for
  flashing.
- If image files cannot be found, check paths relative to the config and the
  requested `$(CHIPNAME)` expansion. Confirm addresses against the actual layout.
- Install the indicated extra if a cryptographic feature or transport reports a
  missing package. J-Link enumeration returns an empty list when its backend or
  native library is unavailable.
- Use an arm64 Python interpreter and matching native libraries on Apple Silicon.
  Python installation alone does not qualify a board, transport or crypto path;
  inherited protocol and decompiler issues may still require investigation.

## Origin and license

This project derives from the Bouffalo Lab Flash Command executable, recovered
from its PyInstaller bundle using decompyle3 and unpyc3, with pycdc used for
comparison. The original reconstruction replaced the config parser with standard
Python INI parsing and added diagnostics. The upstream source is hosted at
[git.lerch.org/lobo/blflashcommand](https://git.lerch.org/lobo/blflashcommand).
See [LICENSE](LICENSE) for the Apache 2.0 license.
