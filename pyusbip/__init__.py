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
import signal
import sys

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
__version__ = "1.1.1"

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
        "(default: %(default)s). Created automatically when the first "
        "entry is added via POST /bind on the control plane.",
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
    else:
        logger.info("exporting all libusb-visible devices (no --vid filter)")

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

    # SIGHUP → reload the allowlist file. Lets the operator edit
    # /etc/pyusbip/shared.json by hand and apply changes without
    # restarting the server. The handler runs on whatever thread
    # delivered the signal; Registry.load() is internally locked, so
    # it's safe to call from anywhere. We log on the main logger so
    # the reload is visible.
    def _reload_registry(*_):
        logger.info("SIGHUP received — reloading registry from %s", args.shared_file)
        registry.load()

    try:
        loop.add_signal_handler(signal.SIGHUP, _reload_registry)
    except (NotImplementedError, AttributeError):
        # SIGHUP isn't on Windows; loop.add_signal_handler isn't on
        # ProactorEventLoop. Either way, silently skip — operators
        # on those platforms can restart pyusbip to apply edits.
        pass

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

    # Force-exit via os._exit. We deliberately skip:
    #
    #   * Python's atexit handlers — notably libusb1's weakref
    #     finalizer (context.handleEvents()), which blocks when
    #     a device is held by another process. Without this skip,
    #     the join of the default asyncio ThreadPoolExecutor in
    #     concurrent.futures._python_exit hangs on the abandoned
    #     _cleanup_devices_async work, forcing multiple Ctrl-Cs.
    #
    #   * usbctx.close()        — same hang, called explicitly.
    #   * loop.close()          — Python-internal bookkeeping;
    #                              process exit handles it.
    #
    # We previously avoided os._exit because skipping libusb's
    # graceful shutdown risked leaving stale IOKit claims for
    # the *next* pyusbip launch. That risk is now mitigated by
    # handle_op_import's smoke-test (server.py), which catches
    # stale handles at IMPORT time and issues a reset to unstick
    # them. So fast exit here is the right trade: bounded
    # shutdown of OUR work, OS reaps everything else.
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
