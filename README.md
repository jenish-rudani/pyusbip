# pyusbip

A pure-Python USB/IP server backed by libusb. Shares USB devices from
macOS (or any libusb host) into a Linux USB/IP client — e.g. Docker
Desktop's VM, a remote Linux box, or a CI runner.

> Forked from [tumayt/pyusbip](https://github.com/tumayt/pyusbip).

## Install

```bash
uv tool install git+https://github.com/jenish-rudani/pyusbip
```

## Run

```bash
sudo pyusbip                 # USB/IP on :3240, HTTP control on :3241
sudo pyusbip --vid 0x0483    # only export ST devices
sudo pyusbip --help
```

`sudo` is required for libusb to claim devices on macOS.

## HTTP control plane

JSON over HTTP on `127.0.0.1:3241`. CORS allow-all.

- `GET /health` — uptime, device counts, API version.
- `GET /devices` — list of libusb-visible devices with `bus_id`, VID/PID,
  strings, and `bind_state` (`"shared"` or `"attached"`).

## Library use

```python
import asyncio, usb1
from pyusbip import USBIPServer, ControlPlane

async def main():
    ctx = usb1.USBContext(); ctx.open()
    loop = asyncio.get_event_loop()
    server = USBIPServer(loop, ctx, vid_filter={0x0483})
    control = ControlPlane(loop, ctx, server)
    await server.start(); await control.start()
    await asyncio.Event().wait()

asyncio.run(main())
```

## Development

```bash
uv tool install ruff pre-commit
pre-commit install
ruff format . && ruff check . --fix
```

Version lives in `pyusbip/__init__.py`; `pyproject.toml` reads it
dynamically. Bump, tag, push.

## Requirements

- Python 3.8+
- libusb1
- Root to claim devices

## License

MIT — see [LICENSE](LICENSE).
