"""USB/IP server: asyncio TCP server + per-connection protocol handler.

This module is the libusb-touching half of pyusbip — everything that
holds device handles, claims interfaces, and shuffles bytes between
USB and the USB/IP wire format. The HTTP control plane (`control.py`)
and the bind registry (`registry.py`) deliberately don't import this
module, so a future replacement (e.g. a pure-Rust core via PyO3) can
swap `USBIPServer` out without touching the API surface seen by GUIs.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import usb1

from . import events as ev
from .protocol import (
    USB_EPIPE,
    USB_ENOENT,
    USB_RECIP_DEVICE,
    USB_RECIP_INTERFACE,
    USB_REQ_SET_ADDRESS,
    USB_REQ_SET_CONFIGURATION,
    USB_REQ_SET_INTERFACE,
    USBIP_BUS_ID_SIZE,
    USBIP_CMD_SUBMIT,
    USBIP_CMD_UNLINK,
    USBIP_DIR_IN,
    USBIP_DIR_OUT,
    USBIP_OP_DEVINFO,
    USBIP_OP_DEVLIST,
    USBIP_OP_IMPORT,
    USBIP_OP_UNSPEC,
    USBIP_REPLY,
    USBIP_REQUEST,
    USBIP_RESET_DEV,
    USBIP_RET_SUBMIT,
    USBIP_RET_UNLINK,
    USBIP_SPEED_FULL,
    USBIP_SPEED_HIGH,
    USBIP_SPEED_LOW,
    USBIP_SPEED_UNKNOWN,
    USBIP_ST_NA,
    USBIP_ST_OK,
    USBIP_VERSION,
    USBIPProtocolErrorException,
    USBIPUnimplementedException,
)
from .registry import Registry

logger = logging.getLogger("pyusbip.server")


@dataclass
class USBIPDevice:
    """One opened libusb handle, keyed by the synthesised devid we
    return in IMPORT (busnum<<16 | devnum)."""
    devid: int
    hnd: Any  # libusb1.USBDeviceHandle
    busid: str = ""  # e.g. "2-19", populated at IMPORT time


@dataclass
class USBIPPending:
    """One in-flight URB; we keep these so UNLINK can cancel the
    underlying libusb transfer."""
    seqnum: int
    device: USBIPDevice
    xfer: Any  # libusb1 transfer handle


def _busid_for(dev) -> str:
    return "{}-{}".format(dev.getBusNumber(), dev.getDeviceAddress())


def _read_serial_safely(dev) -> str:
    """Read iSerial via a brief libusb open/close. Returns '' on any
    failure (device without iSerial descriptor, permission denied,
    already in use by another libusb client, etc.). Safe to call from
    DEVLIST handlers because libusb open is shareable on macOS."""
    try:
        hnd = dev.open()
        try:
            return hnd.getSerialNumber() or ""
        finally:
            hnd.close()
    except Exception:
        return ""


class USBIPConnection:
    """One USB/IP client connection.

    Holds the reader/writer, the set of currently-imported devices on
    this connection, and the dispatcher loop. Lifecycle:

      __init__ -> connection() -> handle_packet() (loop) -> cleanup()

    The cleanup path (formerly inline) does the release/reset/close
    sequence on every imported device — macOS IOKit can leave the
    device in a stuck "still claimed" state if we just close() the
    handle without releasing interfaces, which breaks subsequent
    libusb_open from other host-side tools (probe-rs, STM32 CLI).
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        usbctx: usb1.USBContext,
        *,
        vid_filter: Optional[set] = None,
        registry: Optional[Registry] = None,
        require_bind: bool = False,
        event_bus: Optional[ev.EventBus] = None,
        on_attach: Optional[Callable[[str, str], None]] = None,
        on_detach: Optional[Callable[[str, str], None]] = None,
    ):
        self.reader = reader
        self.writer = writer
        self.usbctx = usbctx
        self.vid_filter = vid_filter
        self.registry = registry
        self.require_bind = require_bind
        self.event_bus = event_bus
        # on_attach/on_detach let USBIPServer maintain its attached-by
        # map without USBIPConnection needing a back-reference. Called
        # with (busid, peer_str) right after a successful IMPORT and
        # for each device freed in cleanup.
        self._on_attach = on_attach
        self._on_detach = on_detach
        self.devices: Dict[int, Optional[USBIPDevice]] = {}
        self.urbs: Dict[int, USBIPPending] = {}
        self._peer = writer.get_extra_info("peername")

    # ---- logging helpers ------------------------------------------------

    def say(self, msg: str) -> None:
        logger.info("%s: %s", self._peer, msg)

    def trace(self, msg: str) -> None:
        # Cheap fast path so we don't format hot-path strings when
        # debug logging is off.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s: %s", self._peer, msg)

    # ---- filtering ------------------------------------------------------

    def _device_allowed(self, dev) -> bool:
        """Apply --vid filter + bind registry to decide whether a
        device should appear in DEVLIST / be IMPORTable on this
        connection."""
        if self.vid_filter is not None and dev.getVendorID() not in self.vid_filter:
            return False
        if self.require_bind:
            if self.registry is None or not self.registry:
                return False
            serial = _read_serial_safely(dev)
            if not self.registry.is_allowed(
                dev.getVendorID(), dev.getProductID(), serial
            ):
                return False
        return True

    # ---- protocol packers (preserved from the monolith) -----------------

    def pack_device_desc(self, dev, interfaces: bool = True) -> bytes:
        path = "pyusbip/{}/{}".format(dev.getBusNumber(), dev.getDeviceAddress())
        busid = _busid_for(dev)
        busnum = dev.getBusNumber()
        devnum = dev.getDeviceAddress()
        speed = {
            usb1.SPEED_UNKNOWN: USBIP_SPEED_UNKNOWN,
            usb1.SPEED_LOW: USBIP_SPEED_LOW,
            usb1.SPEED_FULL: USBIP_SPEED_FULL,
            usb1.SPEED_HIGH: USBIP_SPEED_HIGH,
            # USB 3.x speeds are reported as HIGH on the wire — the
            # USB/IP protocol predates SuperSpeed.
            usb1.SPEED_SUPER: USBIP_SPEED_HIGH,
            usb1.SPEED_SUPER_PLUS: USBIP_SPEED_HIGH,
        }[dev.getDeviceSpeed()]

        idVendor = dev.getVendorID()
        idProduct = dev.getProductID()
        bcdDevice = dev.getbcdDevice()

        bDeviceClass = dev.getDeviceClass()
        bDeviceSubClass = dev.getDeviceSubClass()
        bDeviceProtocol = dev.getDeviceProtocol()
        configs = list(dev.iterConfigurations())
        try:
            hnd = dev.open()
            bConfigurationValue = hnd.getConfiguration()
            hnd.close()
        except Exception:
            # Device may not be openable (already claimed by us via a
            # different connection, or permission denied). Fall back
            # to the first configuration's value — DEVLIST consumers
            # treat this as informational anyway.
            bConfigurationValue = configs[0].getConfigurationValue()
        bNumConfigurations = dev.getNumConfigurations()

        config = configs[0]
        for _config in configs:
            if _config.getConfigurationValue() == bConfigurationValue:
                config = _config
                break
        bNumInterfaces = config.getNumInterfaces()

        data = struct.pack(
            ">256s32sIIIHHHBBBBBB",
            path.encode(),
            busid.encode(),
            busnum,
            devnum,
            speed,
            idVendor,
            idProduct,
            bcdDevice,
            bDeviceClass,
            bDeviceSubClass,
            bDeviceProtocol,
            bConfigurationValue,
            bNumConfigurations,
            bNumInterfaces,
        )

        if interfaces:
            for ifc in config.iterInterfaces():
                set_ = list(ifc)[0]
                data += struct.pack(
                    ">BBBB",
                    set_.getClass(),
                    set_.getSubClass(),
                    set_.getProtocol(),
                    0,
                )

        return data

    # ---- op handlers ----------------------------------------------------

    def handle_op_devlist(self) -> None:
        devlist = [d for d in self.usbctx.getDeviceList() if self._device_allowed(d)]

        resp = struct.pack(
            ">HHII",
            USBIP_VERSION,
            USBIP_OP_DEVLIST | USBIP_REPLY,
            USBIP_ST_OK,
            len(devlist),
        )
        for dev in devlist:
            resp += self.pack_device_desc(dev)
        self.writer.write(resp)

    def handle_op_import(self, busid: str) -> None:
        for dev in self.usbctx.getDeviceList():
            if busid != _busid_for(dev):
                continue
            if not self._device_allowed(dev):
                self.say("device {} rejected by filter/bind policy".format(busid))
                resp = struct.pack(
                    ">HHI",
                    USBIP_VERSION,
                    USBIP_OP_IMPORT | USBIP_REPLY,
                    USBIP_ST_NA,
                )
                self.writer.write(resp)
                return

            hnd = dev.open()
            self.say("opened device {}".format(busid))
            devid = dev.getBusNumber() << 16 | dev.getDeviceAddress()
            self.devices[devid] = USBIPDevice(devid=devid, hnd=hnd, busid=busid)
            resp = struct.pack(
                ">HHI",
                USBIP_VERSION,
                USBIP_OP_IMPORT | USBIP_REPLY,
                USBIP_ST_OK,
            )
            resp += self.pack_device_desc(dev, interfaces=False)
            self.writer.write(resp)
            if self._on_attach:
                self._on_attach(busid, str(self._peer))
            if self.event_bus:
                self.event_bus.publish(
                    ev.DEVICE_ATTACHED,
                    {"bus_id": busid, "peer": str(self._peer)},
                )
            return

        self.say("device not found: {}".format(busid))
        resp = struct.pack(
            ">HHI", USBIP_VERSION, USBIP_OP_IMPORT | USBIP_REPLY, USBIP_ST_NA
        )
        self.writer.write(resp)

    # ---- URB submit / unlink (same logic as the monolith) ---------------

    async def handle_urb_submit(self, seqnum, dev, direction, ep):
        op_submit = ">Iiiii8s"
        data = await self.reader.readexactly(struct.calcsize(op_submit))
        (transfer_flags, buflen, start_frame, number_of_packets, interval, setup) = struct.unpack(
            op_submit, data
        )

        if number_of_packets != 0:
            raise USBIPUnimplementedException(
                "ISO number_of_packets {}".format(number_of_packets)
            )

        if direction == USBIP_DIR_OUT:
            buf = await self.reader.readexactly(buflen)

        (bRequestType, bRequest, wValue, wIndex, wLength) = struct.unpack(
            "<BBHHH", setup
        )

        self.trace(
            "seq {:x}: ep {}, direction {}, {} bytes".format(seqnum, ep, direction, buflen)
        )

        if ep == 0:
            if wLength != buflen:
                raise USBIPProtocolErrorException(
                    "wLength {} neq buflen {}".format(wLength, buflen)
                )

            self.trace(
                "EP0 requesttype {}, request {}".format(bRequestType, bRequest)
            )

            fakeit = False

            if bRequestType == USB_RECIP_DEVICE and bRequest == USB_REQ_SET_ADDRESS:
                raise USBIPUnimplementedException("USB_REQ_SET_ADDRESS")
            elif (
                bRequestType == USB_RECIP_DEVICE
                and bRequest == USB_REQ_SET_CONFIGURATION
            ):
                self.say("set configuration: {}".format(wValue))
                dev.hnd.setConfiguration(wValue)

                config = None
                for _config in dev.hnd.getDevice().iterConfigurations():
                    if _config.getConfigurationValue() == wValue:
                        config = _config
                        break
                for i in range(config.getNumInterfaces()):
                    self.trace("  claim interface: {}".format(i))
                    try:
                        if dev.hnd.kernelDriverActive(i):
                            self.trace("    detach kernel driver")
                            dev.hnd.detachKernelDriver(i)
                    except usb1.USBError:
                        self.trace("    kernel driver check failed")
                        pass
                    dev.hnd.claimInterface(i)

                fakeit = True
            elif (
                bRequestType == USB_RECIP_INTERFACE
                and bRequest == USB_REQ_SET_INTERFACE
            ):
                self.trace(
                    "set interface alt setting: {} -> {}".format(wIndex, wValue)
                )
                dev.hnd.claimInterface(wIndex)
                dev.hnd.setInterfaceAltSetting(wIndex, wValue)
                fakeit = True

            try:
                if direction == USBIP_DIR_IN:
                    data = dev.hnd.controlRead(
                        bRequestType, bRequest, wValue, wIndex, wLength
                    )
                    resp = struct.pack(
                        ">IIIIIiiiii8s",
                        USBIP_RET_SUBMIT,
                        seqnum,
                        0,
                        0,
                        0,
                        0,
                        len(data),
                        0,
                        0,
                        0,
                        b"",
                    )
                    resp += data
                    self.trace(
                        "wrote response with {}/{} bytes".format(len(data), wLength)
                    )
                    self.writer.write(resp)
                else:
                    if fakeit:
                        wlen = 0
                    else:
                        wlen = dev.hnd.controlWrite(
                            bRequestType, bRequest, wValue, wIndex, buf
                        )
                    resp = struct.pack(
                        ">IIIIIiiiii8s",
                        USBIP_RET_SUBMIT,
                        seqnum,
                        0,
                        0,
                        0,
                        0,
                        wlen,
                        0,
                        0,
                        0,
                        b"",
                    )
                    self.trace("wrote {}/{} bytes".format(wlen, wLength))
                    self.writer.write(resp)
            except usb1.USBErrorPipe:
                resp = struct.pack(
                    ">IIIIIiiiii8s",
                    USBIP_RET_SUBMIT,
                    seqnum,
                    0,
                    0,
                    0,
                    -USB_EPIPE,
                    0,
                    0,
                    0,
                    0,
                    b"",
                )
                self.trace("EPIPE")
                self.writer.write(resp)
        else:
            xfer = dev.hnd.getTransfer()

            if direction == USBIP_DIR_IN:
                def callback(xfer_):
                    self.trace(
                        "callback IN seqnum {:x} status {} len {} buflen {}".format(
                            seqnum,
                            xfer.getStatus(),
                            xfer.getActualLength(),
                            len(xfer.getBuffer()),
                        )
                    )
                    resp = struct.pack(
                        ">IIIIIiiiii8s",
                        USBIP_RET_SUBMIT,
                        seqnum,
                        0,
                        0,
                        0,
                        -xfer.getStatus(),
                        xfer.getActualLength(),
                        0,
                        0,
                        0,
                        b"",
                    )
                    resp += xfer.getBuffer()[: xfer.getActualLength()]
                    self.writer.write(resp)
                    del self.urbs[seqnum]

                xfer.setBulk(ep | 0x80, buflen, callback)
                xfer.submit()
                self.urbs[seqnum] = USBIPPending(seqnum=seqnum, device=dev, xfer=xfer)
            else:
                def callback(xfer_):
                    self.trace(
                        "callback OUT seqnum {:x} status {} ".format(
                            seqnum, xfer.getStatus()
                        )
                    )
                    resp = struct.pack(
                        ">IIIIIiiiii8s",
                        USBIP_RET_SUBMIT,
                        seqnum,
                        0,
                        0,
                        0,
                        -xfer.getStatus(),
                        xfer.getActualLength(),
                        0,
                        0,
                        0,
                        b"",
                    )
                    self.writer.write(resp)
                    del self.urbs[seqnum]

                xfer.setBulk(ep, buf, callback)
                xfer.submit()
                self.urbs[seqnum] = USBIPPending(seqnum=seqnum, device=dev, xfer=xfer)

    async def handle_urb_unlink(self, seqnum, dev, direction, ep):
        op_submit = ">Iiiii8s"
        data = await self.reader.readexactly(struct.calcsize(op_submit))
        (sseqnum, buflen, start_frame, number_of_packets, interval, setup) = struct.unpack(
            op_submit, data
        )

        self.trace("seq {:x}: UNLINK".format(sseqnum))

        if sseqnum not in self.urbs:
            rv = -USB_ENOENT
        else:
            rv = 0
            self.urbs[sseqnum].xfer.cancel()

        resp = struct.pack(
            ">IIIIIiiiii8s",
            USBIP_RET_UNLINK,
            seqnum,
            0,
            0,
            0,
            rv,
            0,
            0,
            0,
            0,
            b"",
        )
        # NOTE: matches the monolith — original code packed the unlink
        # response but didn't write it. Preserved verbatim to avoid
        # surprising existing clients; flag for future cleanup if a
        # real client ever cares about UNLINK acks.
        _ = resp

    # ---- packet dispatch loop -------------------------------------------

    async def handle_packet(self) -> bool:
        try:
            data = await self.reader.readexactly(2)
        except asyncio.exceptions.IncompleteReadError:
            return False

        (version,) = struct.unpack(">H", data)
        if version == 0x0000:
            op_common = ">HIIII"
            data = await self.reader.readexactly(struct.calcsize(op_common))
            (opcode, seqnum, devid, direction, ep) = struct.unpack(op_common, data)

            if devid not in self.devices or self.devices[devid] is None:
                raise USBIPProtocolErrorException("devid unattached {:x}".format(devid))
            dev = self.devices[devid]

            if opcode == USBIP_CMD_SUBMIT:
                await self.handle_urb_submit(seqnum, dev, direction, ep)
            elif opcode == USBIP_CMD_UNLINK:
                await self.handle_urb_unlink(seqnum, dev, direction, ep)
            elif opcode == USBIP_RESET_DEV:
                raise USBIPUnimplementedException("URB_RESET_DEV")
            else:
                raise USBIPProtocolErrorException("bad USBIP URB {:x}".format(opcode))
        elif (version & 0xFF00) == 0x0100:
            op_common = ">HI"
            data = await self.reader.readexactly(struct.calcsize(op_common))
            (opcode, status) = struct.unpack(op_common, data)

            if opcode == USBIP_OP_UNSPEC | USBIP_REQUEST:
                self.writer.write(
                    struct.pack(
                        ">HHI", version, USBIP_OP_UNSPEC | USBIP_REPLY, USBIP_ST_OK
                    )
                )
            elif opcode == USBIP_OP_DEVINFO | USBIP_REQUEST:
                await self.reader.readexactly(USBIP_BUS_ID_SIZE)
                raise USBIPUnimplementedException("DEVINFO")
            elif opcode == USBIP_OP_DEVLIST | USBIP_REQUEST:
                self.say("DEVLIST")
                self.handle_op_devlist()
            elif opcode == USBIP_OP_IMPORT | USBIP_REQUEST:
                data = (await self.reader.readexactly(USBIP_BUS_ID_SIZE)).decode().rstrip("\0")
                self.say("IMPORT {}".format(data))
                self.handle_op_import(data)
            else:
                raise USBIPProtocolErrorException("bad USBIP op {:x}".format(opcode))
        else:
            raise USBIPProtocolErrorException(
                "unsupported USBIP version {:02x}".format(version)
            )

        return True

    async def connection(self) -> None:
        self.say("connect")

        while True:
            try:
                success = await self.handle_packet()
                await self.writer.drain()
                if not success:
                    break
            except Exception:
                traceback.print_exc()
                self.say("force disconnect due to exception")
                break

        self.say("disconnect")
        self._cleanup_devices()
        try:
            await self.writer.drain()
        except Exception:
            pass
        self.writer.close()

    def _cleanup_devices(self) -> None:
        """Release / reset / close every imported device. macOS IOKit
        leaves devices in an unopenable state otherwise; see the
        package README and the disconnect commit history for the gory
        details."""
        for devid in list(self.devices):
            dev = self.devices[devid]
            if dev is None or dev.hnd is None:
                self.devices[devid] = None
                continue
            hnd = dev.hnd
            busid = dev.busid

            # 1. release interfaces of the active configuration
            try:
                dev_obj = hnd.getDevice()
                try:
                    bConfigVal = hnd.getConfiguration()
                except Exception:
                    bConfigVal = None
                for _config in dev_obj.iterConfigurations():
                    if (
                        bConfigVal is None
                        or _config.getConfigurationValue() == bConfigVal
                    ):
                        for i in range(_config.getNumInterfaces()):
                            try:
                                hnd.releaseInterface(i)
                            except Exception:
                                pass
                        break
            except Exception as e:
                self.say("releaseInterface cleanup failed: {}".format(e))

            # 2. USB port reset so the next host-side libusb client
            #    (probe-rs, STM32_Programmer_CLI) can open cleanly
            try:
                hnd.resetDevice()
            except Exception as e:
                self.say("resetDevice failed: {}".format(e))

            # 3. close handle
            try:
                hnd.close()
            except Exception as e:
                self.say("close failed: {}".format(e))
            self.devices[devid] = None

            if busid and self._on_detach:
                self._on_detach(busid, str(self._peer))
            if self.event_bus and busid:
                self.event_bus.publish(
                    ev.DEVICE_DETACHED,
                    {"bus_id": busid, "peer": str(self._peer)},
                )


class USBIPServer:
    """High-level wrapper around the asyncio TCP server, libusb pollfd
    integration, and (optionally) libusb hotplug callbacks.

    Construct once, call `start()` to bind, then `await stop_event.wait()`
    or use `serve_forever()` from `main()`. All wiring for vid-filter /
    registry / event-bus is via constructor — the per-connection
    `USBIPConnection` reads from these attributes on every request, so
    runtime changes (operator binds a new device) take effect on the
    next DEVLIST/IMPORT without restarting the server.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        usbctx: usb1.USBContext,
        *,
        host: str = "127.0.0.1",
        port: int = 3240,
        vid_filter: Optional[set] = None,
        registry: Optional[Registry] = None,
        require_bind: bool = False,
        event_bus: Optional[ev.EventBus] = None,
    ):
        self.loop = loop
        self.usbctx = usbctx
        self.host = host
        self.port = port
        self.vid_filter = vid_filter
        self.registry = registry
        self.require_bind = require_bind
        self.event_bus = event_bus
        self._server: Optional[asyncio.base_events.Server] = None
        self._hotplug_handle = None
        # busid -> peer-string. Mutated only from the asyncio loop
        # thread (USBIPConnection._on_attach/_on_detach are called
        # synchronously from handle_op_import / cleanup), so no lock.
        self.attached_by_busid: Dict[str, str] = {}

    def _track_attach(self, busid: str, peer: str) -> None:
        self.attached_by_busid[busid] = peer

    def _track_detach(self, busid: str, _peer: str) -> None:
        self.attached_by_busid.pop(busid, None)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = USBIPConnection(
            reader,
            writer,
            self.usbctx,
            vid_filter=self.vid_filter,
            registry=self.registry,
            require_bind=self.require_bind,
            event_bus=self.event_bus,
            on_attach=self._track_attach,
            on_detach=self._track_detach,
        )
        await conn.connection()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

        # libusb pollfd integration: wire libusb's fds into our loop so
        # the asynchronous bulk-transfer callbacks dispatch here without
        # a separate libusb event thread.
        def usb_callback():
            self.usbctx.handleEventsTimeout()

        def usb_added(fd, _events):
            self.loop.add_reader(fd, usb_callback)

        def usb_removed(fd, _events):
            self.loop.remove_reader(fd)

        for fd, events in self.usbctx.getPollFDList():
            usb_added(fd, events)
        self.usbctx.setPollFDNotifiers(usb_added, usb_removed)

        # libusb hotplug — fires from libusb's event thread on plug /
        # unplug. We re-publish via EventBus.publish() which itself is
        # thread-safe.
        if self.event_bus is not None:
            try:
                self._hotplug_handle = self.usbctx.hotplugRegisterCallback(
                    self._on_hotplug
                )
            except Exception as e:
                # Hotplug isn't supported on every libusb backend
                # (notably some macOS minor versions historically).
                # Not fatal — clients can still poll GET /devices.
                logger.warning(
                    "hotplug registration failed: %s; falling back to poll-only",
                    e,
                )

    def _on_hotplug(self, ctx, dev, event):
        """libusb hotplug callback. Runs on libusb's event thread, NOT
        in the asyncio loop — keep this fast and only call thread-safe
        APIs (EventBus.publish uses call_soon_threadsafe)."""
        if self.event_bus is None:
            return
        kind = (
            ev.DEVICE_ADDED
            if event == usb1.HOTPLUG_EVENT_DEVICE_ARRIVED
            else ev.DEVICE_REMOVED
        )
        try:
            payload = {
                "bus_id": _busid_for(dev),
                "vid": dev.getVendorID(),
                "pid": dev.getProductID(),
            }
        except Exception:
            # Some devices race the callback — getDescriptor may fail
            # if the device disappeared mid-callback.
            payload = {"bus_id": ""}
        self.event_bus.publish(kind, payload)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._hotplug_handle is not None:
            try:
                self.usbctx.hotplugDeregisterCallback(self._hotplug_handle)
            except Exception:
                pass
