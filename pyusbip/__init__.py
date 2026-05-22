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
import signal

import usb1

from .control import ControlPlane
from .events import EventBus
from .protocol import (
    USBIPProtocolErrorException,
    USBIPUnimplementedException,
)
from .registry import Match, Registry
from .server import USBIPConnection, USBIPDevice, USBIPPending, USBIPServer

# SINGLE SOURCE OF TRUTH for the package version.
# pyproject.toml reads this via [tool.setuptools.dynamic].
# Bump here when releasing — nothing else to update.
__version__ = "1.0.8"

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
    "__version__",
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

    # Load the registry early — operator may have pre-populated it via
    # a previous run or an out-of-band edit, and we want hotplug events
    # to observe the correct allowlist from the first device scan.
    registry = Registry(path=args.shared_file)
    registry.load()
    if args.require_bind:
        if registry:
            logger.info(
                "require-bind mode ON; %d allowlist entr%s loaded from %s",
                len(registry),
                "y" if len(registry) == 1 else "ies",
                args.shared_file,
            )
        else:
            logger.warning(
                "require-bind mode ON but allowlist is empty (%s); "
                "no devices will be exported until POST /bind is called",
                args.shared_file,
            )
    elif registry:
        logger.info(
            "allowlist recorded at %s (%d entr%s, NOT enforced — pass "
            "--require-bind to gate exports)",
            args.shared_file,
            len(registry),
            "y" if len(registry) == 1 else "ies",
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

    control: ControlPlane | None = None
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
        logger.info("USB/IP server ready: %s:%d", args.host, args.port)
        if control is not None:
            await control.start()
            logger.info(
                "control plane ready: http://%s:%d  (GET /health /devices /events, "
                "POST /bind /unbind)",
                args.control_host,
                args.control_port,
            )
        logger.info("ready — Ctrl-C to exit")

    loop.run_until_complete(_startup())

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass

    logger.info("Ctrl-C received, shutting down (max 4s)")

    async def _shutdown():
        if control is not None:
            await control.stop()
        await server.stop()

    # A second Ctrl-C while _shutdown() runs (or during loop.close /
    # usbctx.close) used to surface as a KeyboardInterrupt traceback.
    # Each cleanup step is wrapped independently so any failure is
    # logged and the others still execute — the goal is "exit
    # quickly, no traceback" once the operator has signalled shutdown.
    try:
        loop.run_until_complete(_shutdown())
    except KeyboardInterrupt:
        logger.info("second Ctrl-C — forcing exit (OS will reap sockets)")
    except BaseException as e:
        logger.warning("shutdown step raised: %s", e)

    try:
        loop.close()
    except BaseException:
        pass

    try:
        usbctx.close()
    except BaseException:
        pass

    logger.info("bye")

    # Past this point, Python will run atexit handlers (notably
    # libusb1's weakref finalizer, which calls context.handleEvents).
    # A Ctrl-C landing in one of those used to surface as
    # "Exception ignored in atexit callback ... KeyboardInterrupt".
    # Block further SIGINT so the finalizers can complete in peace.
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
