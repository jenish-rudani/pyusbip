"""HTTP control plane: GET /health and GET /devices.

Minimal HTTP/1.1 over asyncio.start_server — we don't take a dependency
on aiohttp / starlette because the surface is tiny (2 routes) and the
parser only needs to handle GET requests. ~150 lines is cheaper than
carrying a web framework.

Threading model: everything runs in the asyncio loop.

Auth: none. The control plane binds to 127.0.0.1 by default so only
local processes can reach it. If you ever expose this to a network,
add a token check here first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import usb1

from .server import USBIPServer, _busid_for

logger = logging.getLogger("pyusbip.control")

# Bumped manually when the HTTP API surface changes in an
# incompatible way. Surfaced via GET /health.
API_VERSION = 2


def _read_device_strings(dev) -> tuple[str, str, str]:
    """Open the device once and read all three string descriptors we
    surface in /devices: (manufacturer, product, serial). Returns
    ("", "", "") on any failure. Single open/close cycle — reading
    each string separately would multiply IOKit roundtrips."""
    try:
        hnd = dev.open()
        try:
            return (
                hnd.getManufacturer() or "",
                hnd.getProduct() or "",
                hnd.getSerialNumber() or "",
            )
        finally:
            hnd.close()
    except Exception:
        return ("", "", "")


def _describe_device(
    dev,
    *,
    attached_by_busid: dict[str, str],
) -> dict[str, Any]:
    """Build the JSON record for one libusb device. Reads all string
    descriptors in a single device-open cycle; if the device is
    already in use we fall back to empty strings rather than failing
    the whole list."""
    busid = _busid_for(dev)
    vid = dev.getVendorID()
    pid = dev.getProductID()
    manufacturer, product, serial = _read_device_strings(dev)
    attached_by = attached_by_busid.get(busid)

    # Simplified bind_state vocabulary now that the registry/allowlist
    # mechanism is gone: a device is either "attached" (a USB/IP
    # client has IMPORTed it right now) or "shared" (available for
    # IMPORT). Bia-Factory's Go side maps these to its UI states.
    bind_state = "attached" if attached_by else "shared"

    return {
        "bus_id": busid,
        "vid": vid,
        "pid": pid,
        "vid_pid": f"{vid:04x}:{pid:04x}",
        "manufacturer": manufacturer,
        "product": product,
        "serial": serial,
        "attached_by": attached_by,
        "bind_state": bind_state,
    }


class ControlPlane:
    """HTTP control plane. Wire with `await plane.start()`."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        usbctx: usb1.USBContext,
        usbip_server: USBIPServer,
        *,
        host: str = "127.0.0.1",
        port: int = 3241,
    ):
        self.loop = loop
        self.usbctx = usbctx
        self.usbip_server = usbip_server
        self.host = host
        self.port = port
        self._server: asyncio.base_events.Server | None = None
        self._started_at = time.time()

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        logger.info("control plane listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            logger.info("stopping HTTP control plane on %s:%d", self.host, self.port)
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
                logger.info("control plane stopped cleanly")
            except asyncio.TimeoutError:
                logger.warning("control plane didn't drain in 2s; forcing exit")
            except BaseException:
                pass

    # ---- per-connection handler -----------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = await self._read_request(reader)
            if req is None:
                return
            method, path, _query, _headers, _body = req
            await self._route(method, path, writer)
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
    ) -> tuple[str, str, dict[str, str], dict[str, str], bytes] | None:
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
        query: dict[str, str] = {}
        if query_str:
            for pair in query_str.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                else:
                    k, v = pair, ""
                query[k] = v

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            try:
                k, v = line.decode("latin-1").split(":", 1)
                headers[k.strip().lower()] = v.strip()
            except ValueError:
                continue

        # The endpoints we keep don't accept request bodies, but we
        # still read Content-Length if present so the next request on
        # a keep-alive connection isn't corrupted.
        body = b""
        cl = headers.get("content-length")
        if cl:
            try:
                n = int(cl)
                if 0 < n <= 1 << 20:
                    body = await reader.readexactly(n)
            except (ValueError, asyncio.IncompleteReadError):
                return None

        return method.upper(), path, query, headers, body

    async def _route(
        self,
        method: str,
        path: str,
        writer: asyncio.StreamWriter,
    ) -> None:
        if method == "GET" and path == "/health":
            await self._write_json(writer, 200, self._health_payload())
        elif method == "GET" and path == "/devices":
            await self._write_json(writer, 200, self._devices_payload())
        else:
            await self._write_json(writer, 404, {"error": "not found", "path": path})

    # ---- handlers -------------------------------------------------------

    def _health_payload(self) -> dict[str, Any]:
        # Lazy import to avoid a circular dependency at module load:
        # pyusbip.__init__ already imports ControlPlane.
        from . import __version__ as pkg_version

        return {
            "ok": True,
            "version": pkg_version,
            "api_version": API_VERSION,
            "uptime_s": int(time.time() - self._started_at),
            "devices_total": len(self.usbctx.getDeviceList()),
            "attached_total": len(self.usbip_server.attached_by_busid),
        }

    def _devices_payload(self) -> dict[str, Any]:
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
                    _describe_device(dev, attached_by_busid=attached),
                )
            except Exception:
                logger.exception("describe_device crashed")
        return {"devices": devices}

    # ---- response writer ------------------------------------------------

    async def _write_json(
        self, writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]
    ) -> None:
        body = json.dumps(payload).encode()
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            500: "Internal Server Error",
        }.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(head.encode() + body)
        await writer.drain()
