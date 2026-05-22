"""pyusbip — USB/IP server backed by libusb, with an HTTP control plane.

Importable as a library:

    from pyusbip import USBIPServer, ControlPlane

Or runnable as a CLI:

    sudo pyusbip [--vid 0x0483] [--log-level info]

The CLI entry point is `pyusbip.main` (kept here so existing
console_scripts installs from pyproject.toml resolve).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import usb1

from .control import ControlPlane
from .protocol import (
    USBIPProtocolErrorException,
    USBIPUnimplementedException,
)
from .server import USBIPConnection, USBIPDevice, USBIPPending, USBIPServer

# SINGLE SOURCE OF TRUTH for the package version.
# pyproject.toml reads this via [tool.setuptools.dynamic].
# Bump here when releasing — nothing else to update.
__version__ = "2.0.0"

__all__ = [
    "ControlPlane",
    "USBIPConnection",
    "USBIPDevice",
    "USBIPPending",
    "USBIPProtocolErrorException",
    "USBIPServer",
    "USBIPUnimplementedException",
    "__version__",
    "main",
]

# Defaults kept here so existing tooling that did `from pyusbip import
# USBIP_HOST` still resolves.
USBIP_HOST = "127.0.0.1"
USBIP_PORT = 3240
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 3241

logger = logging.getLogger("pyusbip")


def _parse_vid_list(values: list[str]) -> set | None:
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
                raise SystemExit(f"invalid --vid value: {part!r}") from e
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
        "--control-host:--control-port for GET /health and GET /devices.",
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
    return parser


def main(argv: list[str] | None = None) -> None:
    """CLI entry point. Wired in pyproject.toml as `pyusbip = pyusbip:main`."""
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info("pyusbip %s starting (log-level=%s)", __version__, args.log_level)

    vid_filter = _parse_vid_list(args.vid)
    if vid_filter is not None:
        logger.info(
            "exporting only VID(s): %s",
            ", ".join(f"0x{v:04x}" for v in sorted(vid_filter)),
        )
    else:
        logger.info("exporting all libusb-visible devices (no --vid filter)")

    usbctx = usb1.USBContext()
    usbctx.open()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    server = USBIPServer(
        loop,
        usbctx,
        host=args.host,
        port=args.port,
        vid_filter=vid_filter,
    )

    control: ControlPlane | None = None
    if not args.no_control_plane:
        control = ControlPlane(
            loop,
            usbctx,
            server,
            host=args.control_host,
            port=args.control_port,
        )

    async def _startup():
        await server.start()
        logger.info("USB/IP server ready: %s:%d", args.host, args.port)
        if control is not None:
            await control.start()
            logger.info(
                "control plane ready: http://%s:%d  (GET /health /devices)",
                args.control_host,
                args.control_port,
            )
        logger.info("ready — Ctrl-C to exit")

    loop.run_until_complete(_startup())

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    logger.info("Ctrl-C received, shutting down (max 6s)")

    async def _shutdown():
        if control is not None:
            await control.stop()
        await server.stop()

    # Bounded asyncio shutdown.
    try:
        loop.run_until_complete(asyncio.wait_for(_shutdown(), timeout=6.0))
    except asyncio.TimeoutError:
        logger.warning("shutdown timed out after 6s — proceeding")
    except KeyboardInterrupt:
        logger.info("second Ctrl-C during shutdown — proceeding")
    except BaseException as e:
        logger.warning("shutdown step raised: %s", e)

    logger.info("bye")

    # Force-exit via os._exit. We deliberately skip Python's atexit
    # handlers — notably libusb1's weakref finalizer, which can hang
    # when a device is held by another process (serial monitor open
    # on /dev/ttyACM* in a container). The OS reaps file descriptors
    # and kernel-side libusb state at process exit.
    try:
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)
