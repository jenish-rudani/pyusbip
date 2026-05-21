"""pyusbip — USB/IP server backed by libusb, with persistent bind
allowlist and an HTTP control plane.

Importable as a library:

    from pyusbip import USBIPServer, Registry, Match, ControlPlane, EventBus

Or runnable as a CLI:

    sudo pyusbip [--vid 0x0483] [--require-bind] [--log-level info]

The CLI entry point is `pyusbip.main` (kept here so existing
console_scripts installs from setup.py / pyproject.toml resolve).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import List, Optional

import usb1

from . import events as ev_mod
from .events import EventBus
from .control import ControlPlane
from .protocol import (
    USBIPProtocolErrorException,
    USBIPUnimplementedException,
)
from .registry import Match, Registry
from .server import USBIPServer, USBIPConnection, USBIPDevice, USBIPPending

__all__ = [
    "ControlPlane",
    "EventBus",
    "Match",
    "Registry",
    "USBIPConnection",
    "USBIPDevice",
    "USBIPPending",
    "USBIPProtocolErrorException",
    "USBIPServer",
    "USBIPUnimplementedException",
    "main",
]

# Defaults kept here so existing tooling that did `from pyusbip import
# USBIP_HOST` still resolves.
USBIP_HOST = "127.0.0.1"
USBIP_PORT = 3240
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 3241
DEFAULT_SHARED_FILE = "/etc/pyusbip/shared.json"

logger = logging.getLogger("pyusbip")


def _parse_vid_list(values: List[str]) -> Optional[set]:
    """['0x0483', '1155,0x1366'] → {0x0483, 0x0483, 0x1366}.
    Returns None when the list is empty so callers can distinguish
    'no filter' from 'an empty filter that matches nothing'."""
    vids: set = set()
    for v in values or []:
        for part in v.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                vids.add(int(part, 0))  # auto-detect 0x prefix
            except ValueError as e:
                raise SystemExit("invalid --vid value: {!r}".format(part)) from e
    return vids or None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyusbip",
        description=(
            "USB/IP server backed by libusb. Exports macOS USB devices "
            "to remote clients (e.g. Linux usbip kernel client inside "
            "Docker Desktop's VM)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["error", "warning", "info", "debug"],
        help="Verbosity. 'info' shows lifecycle events (connect, disconnect, "
             "IMPORT, DEVLIST, set-configuration). 'debug' adds per-URB "
             "callbacks (firehose during firmware flashing).",
    )
    parser.add_argument(
        "--vid",
        action="append",
        default=[],
        help="Restrict exported devices to these USB vendor IDs (hex or "
             "decimal). Repeat or comma-separate. Example: "
             "--vid 0x0483 --vid 0x1366. Default: export everything.",
    )
    parser.add_argument(
        "--host",
        default=USBIP_HOST,
        help="USB/IP TCP bind address (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        default=USBIP_PORT,
        type=int,
        help="USB/IP TCP bind port (default: %(default)s)",
    )
    parser.add_argument(
        "--no-control-plane",
        action="store_true",
        help="Disable the HTTP control plane. By default it listens on "
             "--control-host:--control-port for GET /devices, POST /bind, "
             "POST /unbind, and SSE /events.",
    )
    parser.add_argument(
        "--control-host",
        default=CONTROL_HOST,
        help="HTTP control plane bind address (default: %(default)s)",
    )
    parser.add_argument(
        "--control-port",
        default=CONTROL_PORT,
        type=int,
        help="HTTP control plane bind port (default: %(default)s)",
    )
    parser.add_argument(
        "--shared-file",
        default=DEFAULT_SHARED_FILE,
        help="JSON file holding the persistent bind allowlist "
             "(default: %(default)s). Created on first --bind.",
    )
    parser.add_argument(
        "--require-bind",
        action="store_true",
        help="Only export devices that match an entry in the bind "
             "allowlist (Windows usbipd-win semantics). Without this, "
             "every device passing --vid is exported (legacy behavior).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entry point. Wired in pyproject.toml as `pyusbip = pyusbip:main`."""
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    vid_filter = _parse_vid_list(args.vid)
    if vid_filter is not None:
        logger.info(
            "filtering DEVLIST/IMPORT to vendor IDs: %s",
            ", ".join("0x{:04x}".format(v) for v in sorted(vid_filter)),
        )

    # Load the registry early — operator may have pre-populated it via
    # a previous run or an out-of-band edit, and we want hotplug events
    # to observe the correct allowlist from the first device scan.
    registry = Registry(path=args.shared_file)
    registry.load()
    if args.require_bind and not registry:
        logger.warning(
            "--require-bind set but registry is empty; no devices will be "
            "exported until you bind one (POST /bind on the control plane)"
        )

    usbctx = usb1.USBContext()
    usbctx.open()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    event_bus = EventBus(loop)

    server = USBIPServer(
        loop,
        usbctx,
        host=args.host,
        port=args.port,
        vid_filter=vid_filter,
        registry=registry,
        require_bind=args.require_bind,
        event_bus=event_bus,
    )

    control: Optional[ControlPlane] = None
    if not args.no_control_plane:
        control = ControlPlane(
            loop,
            usbctx,
            server,
            registry,
            event_bus,
            host=args.control_host,
            port=args.control_port,
        )

    async def _startup():
        await server.start()
        if control is not None:
            await control.start()
        logger.info(
            "USB/IP serving on %s:%d", args.host, args.port
        )
        if control is not None:
            logger.info(
                "control plane on http://%s:%d (GET /devices, /events, POST /bind, /unbind)",
                args.control_host,
                args.control_port,
            )

    loop.run_until_complete(_startup())

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    logger.info("shutting down...")

    async def _shutdown():
        if control is not None:
            await control.stop()
        await server.stop()

    loop.run_until_complete(_shutdown())
    loop.close()
    usbctx.close()
