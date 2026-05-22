# pyusbip

A pure-Python USB/IP server backed by libusb, with a persistent bind
allowlist and an HTTP control plane. Lets you share USB devices from
macOS (or any libusb-capable host) into a Linux USB/IP client — e.g.
the kernel client inside Docker Desktop's VM, a remote Linux box, or
a CI runner.

> Forked from [tumayt/pyusbip](https://github.com/tumayt/pyusbip).
> Adds: package layout, HTTP+SSE control plane, persistent bind
> allowlist, libusb hotplug, post-disconnect device cleanup
> (release/reset/close) — see [Changes](#changes-from-upstream).

## What it does

Three things, layered:

1. **USB/IP wire server** on `:3240` — speaks the standard Linux
   USB/IP protocol so any `usbip` kernel client (Linux, the LinuxKit
   VM inside Docker Desktop, Windows usbip-win-client, …) can list
   and import devices.
2. **Bind allowlist** — Windows usbipd-win-style "Not shared / Shared
   / Attached" semantics. Persistent across restarts. Entries match
   by `{vid, pid?, serial?}` so a single bind row can cover a model
   ("every ST-Link V3") or one physical probe (`{vid, pid, serial}`).
3. **HTTP control plane** on `:3241` — JSON over HTTP/1.1 for
   querying state and managing binds from a GUI. Server-Sent Events
   stream device-add/remove/attach/detach/bind-changed live so
   consumers don't have to poll.

## Install

### With [uv](https://docs.astral.sh/uv/) (recommended)

uv installs the CLI in an isolated environment without touching
system Python and without needing `--break-system-packages`. The
`uv tool` workflow puts the `pyusbip` binary on your PATH:

```bash
# From a release tag:
uv tool install git+https://github.com/jenish-rudani/pyusbip

# Or from a local checkout (editable):
git clone https://github.com/jenish-rudani/pyusbip
cd pyusbip
uv tool install --editable .

# Upgrade:
uv tool upgrade pyusbip
```

After `uv tool install`, `which pyusbip` should show
`~/.local/bin/pyusbip` (or wherever `uv tool dir` reports).

### With pip

The traditional path, less isolated:

```bash
# Recommended: in a venv
python3 -m venv .venv
source .venv/bin/activate
pip install git+https://github.com/jenish-rudani/pyusbip

# Or system-wide on macOS (needs --break-system-packages on
# Homebrew Python since PEP 668):
pip3 install --break-system-packages git+https://github.com/jenish-rudani/pyusbip
```

## Run

```bash
sudo pyusbip                    # default: bind 127.0.0.1:3240,
                                # control plane on 127.0.0.1:3241,
                                # export every libusb-visible device
```

You'll see:

```
16:30:45 [INFO] USB/IP serving on 127.0.0.1:3240
16:30:45 [INFO] control plane on http://127.0.0.1:3241 (GET /devices, /events, POST /bind, /unbind)
```

`sudo` is required because libusb on macOS needs root to claim USB
devices the kernel hasn't already given up.

### Common flags

```
--vid 0x0483 --vid 0x1366    Only export devices from these VIDs
                              (repeat or comma-separate). Default:
                              export everything libusb sees.

--require-bind                Windows-like opt-in semantics. Only
                              devices matching the persistent
                              allowlist (see /bind) are exported.

--log-level info|debug        'debug' adds per-URB callbacks — useful
                              for protocol debugging, firehose during
                              firmware flashing.

--no-control-plane            Disable the HTTP control plane. The
                              USB/IP server still runs.

--shared-file PATH            Where the allowlist lives. Default:
                              /etc/pyusbip/shared.json.

--control-host / --control-port  Bind address for the control plane
                              (default 127.0.0.1:3241).
```

Full list:

```bash
pyusbip --help
```

## HTTP control plane

All endpoints are JSON. CORS allow-all is set so localhost frontends
(Wails, Electron, browsers) can reach it without proxying.

### `GET /health`

```json
{
  "ok": true,
  "api_version": 1,
  "uptime_s": 142,
  "devices_total": 18,
  "attached_total": 1,
  "bind_entries": 2,
  "require_bind": false
}
```

### `GET /devices`

Returns every libusb-visible device (filtered by `--vid` if set):

```json
{
  "devices": [
    {
      "bus_id": "2-19",
      "vid": 1155,
      "pid": 14164,
      "vid_pid": "0483:3754",
      "manufacturer": "STMicroelectronics",
      "product": "STLINK-V3",
      "serial": "004C00323434511634313937",
      "attached_by": "('127.0.0.1', 53871)",
      "bound": true,
      "bind_state": "attached"
    }
  ]
}
```

`bind_state` is one of `"not_shared"`, `"shared"`, `"attached"`.
`attached_by` is `null` when no USB/IP client has the device imported.

### `POST /bind`, `POST /unbind`

Add or remove an entry in the persistent allowlist. Bodies accept
either a `bus_id` (pyusbip resolves it to vid/pid/serial) or an
explicit match (any subset of `vid`, `pid`, `serial`):

```bash
# Bind the device at bus 2-19:
curl -X POST -H 'Content-Type: application/json' \
     -d '{"bus_id":"2-19"}' \
     http://127.0.0.1:3241/bind

# Bind every ST-Link V3 (model-wide rule):
curl -X POST -H 'Content-Type: application/json' \
     -d '{"vid":"0x0483","pid":"0x3754"}' \
     http://127.0.0.1:3241/bind

# Bind one specific probe by iSerial:
curl -X POST -H 'Content-Type: application/json' \
     -d '{"vid":"0x0483","pid":"0x3754","serial":"004C00323434..."}' \
     http://127.0.0.1:3241/bind

# Remove it:
curl -X POST -H 'Content-Type: application/json' \
     -d '{"vid":"0x0483","pid":"0x3754"}' \
     http://127.0.0.1:3241/unbind
```

Response:

```json
{
  "ok": true,
  "changed": true,
  "match": {"vid": "0x0483", "pid": "0x3754"},
  "entries": [{"vid": "0x0483", "pid": "0x3754"}]
}
```

`changed` is `false` when the operation was a no-op (binding an
already-bound match or unbinding a non-existent one).

### `GET /events` (Server-Sent Events)

```bash
curl -N http://127.0.0.1:3241/events
```

Streams events as they happen — `device_added`, `device_removed`
(libusb hotplug), `device_attached`, `device_detached` (USB/IP
client imports/disconnects), `bind_changed` (allowlist mutated).

```
data: {"type":"hello","api_version":1}

data: {"type":"device_attached","bus_id":"2-19","peer":"('127.0.0.1', 53871)"}

data: {"type":"device_detached","bus_id":"2-19","peer":"('127.0.0.1', 53871)"}
```

A `: keepalive` comment is sent every 25s so reverse proxies don't
close idle streams.

## Use as a library

```python
import asyncio
import usb1
from pyusbip import USBIPServer, Registry, EventBus, ControlPlane, Match

async def run():
    usbctx = usb1.USBContext()
    usbctx.open()

    loop = asyncio.get_event_loop()
    bus = EventBus(loop)

    registry = Registry(path="/etc/pyusbip/shared.json")
    registry.load()
    registry.bind(Match(vid=0x0483, pid=0x3754))   # ST-Link V3 MINIE

    server = USBIPServer(loop, usbctx,
                         vid_filter={0x0483},
                         registry=registry,
                         require_bind=True,
                         event_bus=bus)
    control = ControlPlane(loop, usbctx, server, registry, bus)

    await server.start()
    await control.start()

    queue = bus.subscribe()
    try:
        while True:
            event = await queue.get()
            print("event:", event)
    finally:
        bus.unsubscribe(queue)

asyncio.run(run())
```

Public API (re-exported from `pyusbip/__init__.py`):
`USBIPServer`, `ControlPlane`, `Registry`, `Match`, `EventBus`,
`USBIPConnection`, `USBIPDevice`, `USBIPPending`,
`USBIPProtocolErrorException`, `USBIPUnimplementedException`, `main`.

## Package layout

```
pyusbip/
├── __init__.py     # public API + CLI entry (main)
├── __main__.py     # `python -m pyusbip`
├── protocol.py     # wire constants + exception types
├── events.py       # thread-safe EventBus (libusb thread → asyncio loop)
├── registry.py     # Match + Registry (in-memory + JSON persistence)
├── server.py       # USBIPServer + per-connection USBIPConnection
└── control.py      # HTTP/1.1 + JSON + SSE control plane
```

Each module has one responsibility. `protocol.py` has no asyncio /
libusb imports so testing the wire layer doesn't require either.
`server.py` is the only module that touches libusb.

## How it integrates with Docker Desktop on macOS

The macOS host runs `pyusbip` as a USB/IP server. Docker Desktop's
LinuxKit VM acts as the USB/IP client:

```
┌──────────── macOS host ────────────┐    ┌─────── Docker VM ────────┐
│                                    │    │                          │
│  USB device                        │    │  usbip kernel client     │
│       │                            │    │       │                  │
│       ▼                            │    │       ▼                  │
│  libusb (IOKit) ──► pyusbip ─────TCP/3240──► /dev/bus/usb/...       │
│                       │            │    │       │                  │
│                       └─HTTP/3241──┼────┼──► your dev container    │
│                       (control)    │    │     (via /dev bind mount)│
│                                    │    │                          │
└────────────────────────────────────┘    └──────────────────────────┘
```

Inside the VM, run `usbip attach -r host.docker.internal -b <busid>`
(e.g. via a sidecar container). The device appears at
`/dev/bus/usb/...` in the VM and, if your dev container bind-mounts
`/dev`, in your container too. The control plane (`3241`) is for
the GUI — listing devices and managing the allowlist without needing
a sidecar at all.

## Changes from upstream `tumayt/pyusbip`

- **Package layout.** Single 500-line `pyusbip.py` → modular package
  (`protocol`, `events`, `registry`, `server`, `control`).
- **HTTP control plane.** Adds `GET /health`, `GET /devices`,
  `POST /bind`, `POST /unbind`, `GET /events` (SSE).
- **Persistent bind allowlist** with VID/PID/serial wildcards.
- **`--require-bind` mode** for Windows-usbipd-win-style semantics.
- **`--vid` filter** so the server can be narrowed to specific
  vendors without touching the allowlist.
- **`--log-level`** + `logging` module — the upstream's per-URB
  `print()` firehose is now at DEBUG, INFO shows lifecycle only.
- **Disconnect cleanup.** On client disconnect, releases interfaces
  → `resetDevice` → `close`. The old `close()`-only path left macOS
  IOKit in a stuck state where subsequent libusb consumers
  (probe-rs, STM32_Programmer_CLI) failed to open the device until
  physical replug.
- **libusb hotplug → SSE.** Hotplug events arrive in the control
  plane stream without polling.

## Requirements

- Python 3.8+
- libusb1 (`pip install libusb1`)
- macOS, Linux, or any host with libusb. Tested heavily on
  macOS 14/15 + Apple Silicon.
- Root / administrator privileges to claim USB devices via libusb.

## Development setup

Lint + format is handled by [ruff](https://docs.astral.sh/ruff/)
(replaces black + flake8 + isort). Hooks are wired through
[pre-commit](https://pre-commit.com/) so every commit auto-runs them.

```bash
# One-time setup after cloning:
uv tool install ruff                # or: pip install --user ruff
uv tool install pre-commit          # or: pip install --user pre-commit
pre-commit install                  # wires .git/hooks/pre-commit

# Manual invocations:
ruff format .                       # reformat in place
ruff check . --fix                  # lint + autofix
pre-commit run --all-files          # run all hooks against the whole repo
```

Ruff configuration lives in `pyproject.toml` under `[tool.ruff]`.
Hook versions are pinned in `.pre-commit-config.yaml`; bump them with
`pre-commit autoupdate`.

## Releasing

The version is the single string `__version__` in
`pyusbip/__init__.py`. `pyproject.toml` reads it dynamically via
`[tool.setuptools.dynamic]`, so a release bump is one edit:

```bash
# Edit pyusbip/__init__.py: __version__ = "X.Y.Z"
git commit -am "release: X.Y.Z"
git tag vX.Y.Z
git push --tags
```

## License

MIT. See [LICENSE](LICENSE).
