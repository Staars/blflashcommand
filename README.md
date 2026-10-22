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

Set the port, chip and baud rate for your board, and enter its download mode before
flashing. Run `uv run python BLFlashCommand.py --help` for CLI options.

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

`read_flash_uart(address, length, ...)` returns `ReadResult(address, data, log)`
with raw `bytes`, using bounded reads and rejecting incomplete responses.

`probe_uart` returns `ProbeResult(chip, backend, boot_info, mac_address, jedec_id,
jedec_response, log)`. Hexadecimal evidence is returned as strings. `chip` is the
requested profile, not an autodetected identity; the API does not distinguish
BL616 from BL618 from their shared protocol. `reset_uart` uses the existing ROM
reset command and returns `ResetResult(log)` after acknowledgement. It requires
a reachable download-mode ROM and suitable boot straps; application boot is not
verified. It is a ROM-only operation; use `flash_uart(reset_after=True)` to reset
from the active RAM/ROM loader session after writing. Probe/read can reset into
download mode, load a helper into RAM and
configure flash, without programming flash or efuses.

The Python API defaults to 500 kbaud for BL602 and 2 Mbaud for other profiles;
reset defaults to 500 kbaud. Pass an explicit baud rate for the target board.
A firmware console's baud rate is independent of the flashing baud rate. Invalid
arguments raise `ValueError`, `TypeError` or a filesystem exception before serial access;
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
| BL618 | BL616 | `chips/bl616` | auto |

BL618 shares the BL616 loader, bootheader and image helpers; a `chips/bl618`
directory is unnecessary. BL702 retains its separate loader and resources.

The upstream project reported BL616 flashing on Linux. Hardware qualification of
this standalone CLI in the current checkout remains open. BL602 board activity
has been reported, but does not establish qualification of this Python flashing
path. BL618 remains unqualified; BL702 has not been tested and no board is
available. Flashing, readback, boot and recovery need qualification with the
intended board, images and layout.

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
`--chipname bl618 --cpu_id foo` selects `firmware_bl618_foo.bin`. This does not
change the shared `chips/bl616` resource path.

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
  access permissions, download mode and baud rate. A working firmware console
  does not establish that the ROM loader is ready for flashing.
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
