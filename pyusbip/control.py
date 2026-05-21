"""HTTP control plane: GET /health, GET /devices, POST /bind,
POST /unbind, GET /events (SSE).

Minimal HTTP/1.1 over asyncio.start_server — we don't take a dependency
on aiohttp / starlette because the surface is tiny (5 routes) and the
parser only needs to handle GET + POST with a Content-Length-bounded
JSON body. ~250 lines is cheaper than carrying a web framework.

Threading model: everything runs in the asyncio loop. The EventBus
dispatches via call_soon_threadsafe, so hotplug events fired from
libusb's event thread still arrive here on the loop thread.

Auth: none. The control plane binds to 127.0.0.1 by default so only
local processes can reach it. If you ever expose this to a network,
add a token check here first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import usb1

from . import events as ev
from .registry import Match, Registry
from .server import USBIPServer, _busid_for, _read_serial_safely

logger = logging.getLogger("pyusbip.control")

# Bumped manually when the HTTP API surface changes in an
# incompatible way. Surfaced via GET /health.
API_VERSION = 1


def _hex(n: Optional[int]) -> Optional[str]:
    return None if n is None else "0x{:04x}".format(n)


def _read_string_safely(hnd_open_fn, getter_name: str) -> str:
    """Open the device briefly and read a string descriptor.
    Returns "" on any error. Shared with _read_serial_safely in
    server.py but kept separate so control.py doesn't import the
    helper directly (one-way dependency: control -> server, not
    server -> control)."""
    try:
        hnd = hnd_open_fn()
        try:
            return getattr(hnd, getter_name)() or ""
        finally:
            hnd.close()
    except Exception:
        return ""


def _describe_device(
    dev,
    *,
    attached_by_busid: Dict[str, str],
    registry: Optional[Registry],
) -> Dict[str, Any]:
    """Build the JSON record for one libusb device. Reads strings
    lazily via brief open/close; if the device is already in use we
    fall back to empty strings rather than failing the whole list."""
    busid = _busid_for(dev)
    vid = dev.getVendorID()
    pid = dev.getProductID()
    serial = _read_serial_safely(dev)
    manufacturer = _read_string_safely(dev.open, "getManufacturer")
    product = _read_string_safely(dev.open, "getProduct")
    attached_by = attached_by_busid.get(busid)

    bound = False
    if registry is not None:
        bound = registry.is_allowed(vid, pid, serial)

    if attached_by:
        bind_state = "attached"
    elif bound:
        bind_state = "shared"
    else:
        bind_state = "not_shared"

    return {
        "bus_id": busid,
        "vid": vid,
        "pid": pid,
        "vid_pid": "{:04x}:{:04x}".format(vid, pid),
        "manufacturer": manufacturer,
        "product": product,
        "serial": serial,
        "attached_by": attached_by,
        "bound": bound,
        "bind_state": bind_state,
    }


class ControlPlane:
    """HTTP control plane. Wire with `await plane.start()` after the
    event bus and registry are constructed."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        usbctx: usb1.USBContext,
        usbip_server: USBIPServer,
        registry: Registry,
        event_bus: ev.EventBus,
        *,
        host: str = "127.0.0.1",
        port: int = 3241,
    ):
        self.loop = loop
        self.usbctx = usbctx
        self.usbip_server = usbip_server
        self.registry = registry
        self.event_bus = event_bus
        self.host = host
        self.port = port
        self._server: Optional[asyncio.base_events.Server] = None
        self._started_at = time.time()
        # When the registry mutates (bind/unbind), re-emit a generic
        # event so SSE subscribers refresh their device tables.
        registry.add_listener(self._on_registry_change)

    def _on_registry_change(self) -> None:
        self.event_bus.publish(ev.BIND_CHANGED, {"count": len(self.registry)})

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        logger.info("control plane listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ---- per-connection handler -----------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            req = await self._read_request(reader)
            if req is None:
                return
            method, path, query, headers, body = req
            await self._route(method, path, query, headers, body, writer)
        except Exception:
            logger.exception("control plane request crashed")
            try:
                await self._write_json(writer, 500, {"error": "internal error"})
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> Optional[Tuple[str, str, Dict[str, str], Dict[str, str], bytes]]:
        # Read the request line + headers. We bound the read at 8KiB
        # to defend against a slow-loris-style attack on an unbounded
        # readuntil — for a localhost-only control plane this is paranoid
        # but cheap.
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return None

        lines = head.split(b"\r\n")
        if not lines:
            return None
        request_line = lines[0].decode("latin-1", errors="replace")
        try:
            method, raw_path, _ = request_line.split(" ", 2)
        except ValueError:
            return None

        path, _, query_str = raw_path.partition("?")
        query: Dict[str, str] = {}
        if query_str:
            for pair in query_str.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                else:
                    k, v = pair, ""
                query[k] = v

        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            try:
                k, v = line.decode("latin-1").split(":", 1)
                headers[k.strip().lower()] = v.strip()
            except ValueError:
                continue

        body = b""
        cl = headers.get("content-length")
        if cl:
            try:
                n = int(cl)
                if 0 < n <= 1 << 20:  # 1 MiB max
                    body = await reader.readexactly(n)
            except (ValueError, asyncio.IncompleteReadError):
                return None

        return method.upper(), path, query, headers, body

    async def _route(
        self,
        method: str,
        path: str,
        query: Dict[str, str],
        headers: Dict[str, str],
        body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        if method == "GET" and path == "/health":
            await self._write_json(writer, 200, self._health_payload())
        elif method == "GET" and path == "/devices":
            await self._write_json(writer, 200, self._devices_payload())
        elif method == "POST" and path == "/bind":
            await self._handle_bind(body, writer, add=True)
        elif method == "POST" and path == "/unbind":
            await self._handle_bind(body, writer, add=False)
        elif method == "GET" and path == "/events":
            await self._handle_events(writer)
        else:
            await self._write_json(writer, 404, {"error": "not found", "path": path})

    # ---- handlers -------------------------------------------------------

    def _health_payload(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "api_version": API_VERSION,
            "uptime_s": int(time.time() - self._started_at),
            "devices_total": sum(1 for _ in self.usbctx.getDeviceList()),
            "attached_total": len(self.usbip_server.attached_by_busid),
            "bind_entries": len(self.registry),
            "require_bind": self.usbip_server.require_bind,
        }

    def _devices_payload(self) -> Dict[str, Any]:
        attached = dict(self.usbip_server.attached_by_busid)
        devices = []
        for dev in self.usbctx.getDeviceList():
            # Honor --vid filter so the GUI sees the same set the
            # USB/IP DEVLIST would surface.
            vf = self.usbip_server.vid_filter
            if vf is not None and dev.getVendorID() not in vf:
                continue
            try:
                devices.append(
                    _describe_device(
                        dev,
                        attached_by_busid=attached,
                        registry=self.registry,
                    )
                )
            except Exception:
                logger.exception("describe_device crashed")
        return {"devices": devices}

    async def _handle_bind(
        self, body: bytes, writer: asyncio.StreamWriter, *, add: bool
    ) -> None:
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError as e:
            await self._write_json(writer, 400, {"error": "invalid json", "detail": str(e)})
            return

        # Accept either explicit vid/pid/serial fields or a bus_id we
        # have to resolve to a current device. bus_id is convenient for
        # the GUI ("user clicked Bind on this row"); explicit fields are
        # the durable form (survives replug across different busids).
        match: Optional[Match]
        if "bus_id" in data:
            bus_id = str(data["bus_id"])
            resolved = self._lookup_device(bus_id)
            if resolved is None:
                await self._write_json(writer, 404, {"error": "bus_id not found", "bus_id": bus_id})
                return
            vid, pid, serial = resolved
            match = Match(vid=vid, pid=pid, serial=serial or None)
        else:
            try:
                match = Match(
                    vid=_parse_opt_int(data.get("vid")),
                    pid=_parse_opt_int(data.get("pid")),
                    serial=data.get("serial"),
                )
            except ValueError as e:
                await self._write_json(writer, 400, {"error": "invalid match", "detail": str(e)})
                return
            if match.vid is None and match.pid is None and match.serial is None:
                await self._write_json(
                    writer,
                    400,
                    {"error": "match must set at least one of vid/pid/serial"},
                )
                return

        if add:
            changed = self.registry.bind(match)
        else:
            changed = self.registry.unbind(match)

        await self._write_json(
            writer,
            200,
            {
                "ok": True,
                "changed": changed,
                "match": match.to_dict(),
                "entries": [m.to_dict() for m in self.registry.list()],
            },
        )

    def _lookup_device(self, bus_id: str) -> Optional[Tuple[int, int, str]]:
        for dev in self.usbctx.getDeviceList():
            if _busid_for(dev) == bus_id:
                return dev.getVendorID(), dev.getProductID(), _read_serial_safely(dev)
        return None

    async def _handle_events(self, writer: asyncio.StreamWriter) -> None:
        """Server-Sent Events stream. We hold the connection open and
        write one `data: {...}\\n\\n` block per published event until
        the client disconnects."""
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"Access-Control-Allow-Origin: *\r\n"
            b"\r\n"
        )
        await writer.drain()

        queue = self.event_bus.subscribe()
        # Send an initial 'hello' event so clients can confirm the
        # stream is live before any real events occur.
        await self._sse_write(writer, {"type": "hello", "api_version": API_VERSION})

        try:
            while True:
                # 25s keepalive: send a comment line so proxies don't
                # close the idle connection.
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                    await self._sse_write(writer, event)
                except asyncio.TimeoutError:
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self.event_bus.unsubscribe(queue)

    # ---- response writers -----------------------------------------------

    async def _write_json(
        self, writer: asyncio.StreamWriter, status: int, payload: Dict[str, Any]
    ) -> None:
        body = json.dumps(payload).encode()
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}.get(
            status, "OK"
        )
        head = (
            "HTTP/1.1 {} {}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: {}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).format(status, reason, len(body))
        writer.write(head.encode() + body)
        await writer.drain()

    async def _sse_write(
        self, writer: asyncio.StreamWriter, payload: Dict[str, Any]
    ) -> None:
        line = "data: " + json.dumps(payload) + "\n\n"
        writer.write(line.encode())
        await writer.drain()


def _parse_opt_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, int):
        return v
    return int(str(v), 0)  # auto-detect 0x prefix
