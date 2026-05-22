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
from dataclasses import dataclass
from typing import Any, Callable

import usb1

from . import events as ev
from .protocol import (
    USB_ENOENT,
    USB_EPIPE,
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
    return f"{dev.getBusNumber()}-{dev.getDeviceAddress()}"


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
        vid_filter: set | None = None,
        registry: Registry | None = None,
        require_bind: bool = False,
        event_bus: ev.EventBus | None = None,
        on_attach: Callable[[str, str], None] | None = None,
        on_detach: Callable[[str, str], None] | None = None,
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
        self.devices: dict[int, USBIPDevice | None] = {}
        self.urbs: dict[int, USBIPPending] = {}
        self._peer = writer.get_extra_info("peername")
        # True once we've read at least one byte from the peer.
        # Distinguishes a real USB/IP session from a silent TCP
        # liveness probe (Bia-Factory's pyusbipRunning() dials and
        # closes without sending data). Set inside handle_packet so
        # the "connect" log appears before any per-packet log
        # (IMPORT, DEVLIST, …), not after.
        self._first_packet_logged = False

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
            if not self.registry.is_allowed(dev.getVendorID(), dev.getProductID(), serial):
                return False
        return True

    # ---- protocol packers (preserved from the monolith) -----------------

    def pack_device_desc(self, dev, interfaces: bool = True) -> bytes:
        path = f"pyusbip/{dev.getBusNumber()}/{dev.getDeviceAddress()}"
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
                self.say(f"IMPORT {busid} rejected (not in --vid filter / require-bind allowlist)")
                resp = struct.pack(
                    ">HHI",
                    USBIP_VERSION,
                    USBIP_OP_IMPORT | USBIP_REPLY,
                    USBIP_ST_NA,
                )
                self.writer.write(resp)
                return

            # Read a short product label for the log; best-effort.
            try:
                product = dev.getProduct() or ""
            except Exception:
                product = ""
            vid_pid = f"{dev.getVendorID():04x}:{dev.getProductID():04x}"

            hnd = dev.open()
            self.say(
                "IMPORT {} → opened ({}{})".format(
                    busid,
                    vid_pid,
                    ", " + product if product else "",
                )
            )
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

        self.say(f"IMPORT {busid} → device not found")
        resp = struct.pack(">HHI", USBIP_VERSION, USBIP_OP_IMPORT | USBIP_REPLY, USBIP_ST_NA)
        self.writer.write(resp)

    # ---- URB submit / unlink (same logic as the monolith) ---------------

    async def handle_urb_submit(self, seqnum, dev, direction, ep):
        op_submit = ">Iiiii8s"
        data = await self.reader.readexactly(struct.calcsize(op_submit))
        (transfer_flags, buflen, start_frame, number_of_packets, interval, setup) = struct.unpack(
            op_submit, data
        )

        if number_of_packets != 0:
            raise USBIPUnimplementedException(f"ISO number_of_packets {number_of_packets}")

        if direction == USBIP_DIR_OUT:
            buf = await self.reader.readexactly(buflen)

        (bRequestType, bRequest, wValue, wIndex, wLength) = struct.unpack("<BBHHH", setup)

        self.trace(f"seq {seqnum:x}: ep {ep}, direction {direction}, {buflen} bytes")

        if ep == 0:
            if wLength != buflen:
                raise USBIPProtocolErrorException(f"wLength {wLength} neq buflen {buflen}")

            self.trace(f"EP0 requesttype {bRequestType}, request {bRequest}")

            fakeit = False

            if bRequestType == USB_RECIP_DEVICE and bRequest == USB_REQ_SET_ADDRESS:
                raise USBIPUnimplementedException("USB_REQ_SET_ADDRESS")
            elif bRequestType == USB_RECIP_DEVICE and bRequest == USB_REQ_SET_CONFIGURATION:
                dev.hnd.setConfiguration(wValue)

                config = None
                for _config in dev.hnd.getDevice().iterConfigurations():
                    if _config.getConfigurationValue() == wValue:
                        config = _config
                        break
                n_ifaces = config.getNumInterfaces()
                for i in range(n_ifaces):
                    self.trace(f"  claim interface: {i}")
                    try:
                        if dev.hnd.kernelDriverActive(i):
                            self.trace("    detach kernel driver")
                            dev.hnd.detachKernelDriver(i)
                    except usb1.USBError:
                        self.trace("    kernel driver check failed")
                        pass
                    dev.hnd.claimInterface(i)
                self.say(f"{dev.busid} configured: cfg={wValue}, claimed {n_ifaces} interface(s)")

                fakeit = True
            elif bRequestType == USB_RECIP_INTERFACE and bRequest == USB_REQ_SET_INTERFACE:
                self.trace(f"set interface alt setting: {wIndex} -> {wValue}")
                dev.hnd.claimInterface(wIndex)
                dev.hnd.setInterfaceAltSetting(wIndex, wValue)
                fakeit = True

            try:
                if direction == USBIP_DIR_IN:
                    data = dev.hnd.controlRead(bRequestType, bRequest, wValue, wIndex, wLength)
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
                    self.trace(f"wrote response with {len(data)}/{wLength} bytes")
                    self.writer.write(resp)
                else:
                    if fakeit:
                        wlen = 0
                    else:
                        wlen = dev.hnd.controlWrite(bRequestType, bRequest, wValue, wIndex, buf)
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
                    self.trace(f"wrote {wlen}/{wLength} bytes")
                    self.writer.write(resp)
            except (usb1.USBErrorNoDevice, usb1.USBErrorNotFound) as e:
                # Device went away (typical after a USB replug).
                # See _submit_bulk_or_stall for the NO_DEVICE vs
                # NOT_FOUND distinction. Tell the client this URB
                # failed with -EPIPE so its kernel has a clean URB
                # termination, then signal the outer connection loop
                # to close.
                self.say(f"device gone: {e}")
                self._respond_control_error(seqnum)
                raise USBIPProtocolErrorException(f"device disconnected ({e})") from e
            except usb1.USBErrorPipe:
                # Standard endpoint stall — common during device init
                # and benign. Quiet log.
                self._respond_control_error(seqnum)
            except usb1.USBError as e:
                # Any other libusb error on the control transfer:
                # USBErrorIO (LIBUSB_ERROR_IO), USBErrorTimeout,
                # USBErrorOverflow, USBErrorBusy. Map all of them to
                # -EPIPE on the wire — the client kernel will see a
                # terminated URB and decide whether to retry / reset
                # the endpoint, same behaviour as the Linux usbip-host
                # kernel module. Log at INFO so the operator sees
                # which class actually fired (USBErrorIO is what fires
                # on macOS for a device that's been replugged but
                # libusb hasn't refreshed yet).
                self.say(f"control transfer error: {e}")
                self._respond_control_error(seqnum)
        else:
            xfer = dev.hnd.getTransfer()

            if direction == USBIP_DIR_IN:

                def callback(xfer_):
                    self.trace(
                        f"callback IN seqnum {seqnum:x} status {xfer.getStatus()} len {xfer.getActualLength()} buflen {len(xfer.getBuffer())}"
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
                self._submit_bulk_or_stall(xfer, seqnum, dev)
            else:

                def callback(xfer_):
                    self.trace(f"callback OUT seqnum {seqnum:x} status {xfer.getStatus()} ")
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
                self._submit_bulk_or_stall(xfer, seqnum, dev)

    def _respond_control_error(self, seqnum: int) -> None:
        """Send a USBIP_RET_SUBMIT with status=-EPIPE for a failed EP0
        control transfer. Used for both genuine stalls and the broader
        "libusb said no" cases (USBErrorIO, USBErrorTimeout, etc.) so
        the client kernel sees a terminated URB instead of a hung wait.
        Centralised so the various except branches above don't each
        carry their own copy of the struct.pack."""
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
        self.writer.write(resp)

    def _submit_bulk_or_stall(self, xfer, seqnum, dev) -> None:
        """Submit a bulk transfer, translating libusb errors into
        proper USBIP_RET_SUBMIT responses instead of letting them
        kill the whole connection.

        Why this matters: probe-rs/openocd issue thousands of bulk
        URBs per second during SWD operations. A single stalled
        endpoint (LIBUSB_ERROR_PIPE from xfer.submit()) used to take
        down the entire pyusbip ↔ VM connection — operator saw
        "force disconnect" mid-flash. The Linux usbip-host kernel
        module handles this gracefully by reporting the stall to the
        client kernel, which then issues a CLEAR_FEATURE/RESET; we
        do the same by responding with status=-EPIPE here.
        """
        try:
            xfer.submit()
            self.urbs[seqnum] = USBIPPending(seqnum=seqnum, device=dev, xfer=xfer)
            return
        except (usb1.USBErrorNoDevice, usb1.USBErrorNotFound) as e:
            # Device is physically gone. LIBUSB_ERROR_NO_DEVICE (-4) is
            # the canonical "device disconnected" code on Linux;
            # LIBUSB_ERROR_NOT_FOUND (-5) is what macOS / some libusb
            # versions return for the same condition when the
            # handle's backing endpoint can no longer be resolved.
            # Either way: acknowledge the URB with -EPIPE so the
            # client kernel terminates it cleanly, then raise to
            # break out of the connection loop — every subsequent
            # URB would fail the same way.
            self.say(f"bulk submit: device gone: {e}")
            self._respond_control_error(seqnum)
            raise USBIPProtocolErrorException(f"device disconnected ({e})") from e
        except usb1.USBErrorPipe:
            self.trace(f"bulk submit EPIPE seqnum {seqnum:x}")
        except usb1.USBError as e:
            # Other libusb errors (IO, BUSY, TIMEOUT, …): map to a
            # generic EPIPE response so the client at least sees a
            # terminated URB rather than a hung wait. The specific
            # errno isn't reported by libusb here in a usable form.
            self.say(f"bulk submit failed seqnum {seqnum:x}: {e}")

        self._respond_control_error(seqnum)

    async def handle_urb_unlink(self, seqnum, dev, direction, ep):
        op_submit = ">Iiiii8s"
        data = await self.reader.readexactly(struct.calcsize(op_submit))
        (sseqnum, buflen, start_frame, number_of_packets, interval, setup) = struct.unpack(
            op_submit, data
        )

        self.trace(f"seq {sseqnum:x}: UNLINK")

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

        # We've seen the first byte(s) — this is a real USB/IP
        # session, not a silent TCP liveness probe. Log "connect"
        # once, BEFORE we dispatch (so it appears above IMPORT /
        # DEVLIST in the log instead of after).
        if not self._first_packet_logged:
            self.say("connect")
            self._first_packet_logged = True

        (version,) = struct.unpack(">H", data)
        if version == 0x0000:
            op_common = ">HIIII"
            data = await self.reader.readexactly(struct.calcsize(op_common))
            (opcode, seqnum, devid, direction, ep) = struct.unpack(op_common, data)

            if devid not in self.devices or self.devices[devid] is None:
                raise USBIPProtocolErrorException(f"devid unattached {devid:x}")
            dev = self.devices[devid]

            if opcode == USBIP_CMD_SUBMIT:
                await self.handle_urb_submit(seqnum, dev, direction, ep)
            elif opcode == USBIP_CMD_UNLINK:
                await self.handle_urb_unlink(seqnum, dev, direction, ep)
            elif opcode == USBIP_RESET_DEV:
                raise USBIPUnimplementedException("URB_RESET_DEV")
            else:
                raise USBIPProtocolErrorException(f"bad USBIP URB {opcode:x}")
        elif (version & 0xFF00) == 0x0100:
            op_common = ">HI"
            data = await self.reader.readexactly(struct.calcsize(op_common))
            (opcode, status) = struct.unpack(op_common, data)

            if opcode == USBIP_OP_UNSPEC | USBIP_REQUEST:
                self.writer.write(
                    struct.pack(">HHI", version, USBIP_OP_UNSPEC | USBIP_REPLY, USBIP_ST_OK)
                )
            elif opcode == USBIP_OP_DEVINFO | USBIP_REQUEST:
                await self.reader.readexactly(USBIP_BUS_ID_SIZE)
                raise USBIPUnimplementedException("DEVINFO")
            elif opcode == USBIP_OP_DEVLIST | USBIP_REQUEST:
                self.say("DEVLIST")
                self.handle_op_devlist()
            elif opcode == USBIP_OP_IMPORT | USBIP_REQUEST:
                # handle_op_import logs the richer "IMPORT 2-18 →
                # opened (0483:3754, STLINK-V3)" line; no need to
                # double-log the bare request here.
                data = (await self.reader.readexactly(USBIP_BUS_ID_SIZE)).decode().rstrip("\0")
                self.handle_op_import(data)
            else:
                raise USBIPProtocolErrorException(f"bad USBIP op {opcode:x}")
        else:
            raise USBIPProtocolErrorException(f"unsupported USBIP version {version:02x}")

        return True

    async def connection(self) -> None:
        # The "connect" log is emitted from handle_packet on the first
        # byte read — so a silent TCP probe never sees a "connect"
        # line, and a real session sees "connect" *before* any IMPORT
        # / DEVLIST per-packet log. The mirror "disconnect" log here
        # only fires for sessions that were announced.
        while True:
            try:
                success = await self.handle_packet()
                await self.writer.drain()
                if not success:
                    break
            except (asyncio.CancelledError, KeyboardInterrupt):
                # Server is shutting down. Let the asyncio task end
                # cleanly after we finish device cleanup below — we
                # specifically don't re-raise CancelledError because
                # the surrounding _handle() shouldn't propagate it.
                if self._first_packet_logged:
                    self.say("connection cancelled")
                break
            except USBIPProtocolErrorException as e:
                # Intentional protocol-level exit — we raise this
                # ourselves when libusb tells us the device is gone.
                # No traceback: the cause was logged at the raise
                # site with the actual libusb error.
                if self._first_packet_logged:
                    self.say(f"closing session: {e}")
                break
            except Exception:
                if self._first_packet_logged:
                    traceback.print_exc()
                    self.say("force disconnect due to exception")
                else:
                    # Probe-style connection that errored before
                    # sending a packet (rare). Log compactly at debug.
                    self.trace("probe errored before any packet")
                break

        if self._first_packet_logged:
            self.say("disconnect")
        else:
            self.trace("probe (no bytes sent)")

        try:
            self._cleanup_devices()
        except BaseException as e:
            # Belt-and-braces. _cleanup_devices already catches
            # BaseException internally, but if a future refactor
            # adds a path that escapes, the asyncio task still
            # ends cleanly rather than crashing the event loop.
            self.say(f"cleanup raised: {e}")
        try:
            await self.writer.drain()
        except BaseException:
            pass
        try:
            self.writer.close()
        except BaseException:
            pass

    def _cleanup_devices(self) -> None:
        """Release / reset / close every imported device. macOS IOKit
        leaves devices in an unopenable state otherwise; see the
        package README and the disconnect commit history for the gory
        details.

        Every step swallows BaseException (not just Exception): the
        sequence is invoked from the asyncio task's normal-and-shutdown
        paths alike, and `hnd.close()` internally calls
        context.handleEvents(). On Ctrl-C, the resulting KeyboardInterrupt
        used to bubble out as 'Unhandled exception in client_connected_cb'.
        Catching BaseException keeps the shutdown clean — we'd rather
        leak a libusb handle for the few ms before process exit than
        have noisy tracebacks in the operator's terminal.
        """
        # Count live handles so the shutdown log gives the operator
        # something to read while we wait. Devices that were already
        # None (e.g. cleaned up earlier) are skipped silently.
        live = [
            (devid, dev)
            for devid, dev in self.devices.items()
            if dev is not None and dev.hnd is not None
        ]
        if live:
            self.say(
                "cleanup: {} device(s) ({})".format(
                    len(live),
                    ", ".join(dev.busid or "?" for _, dev in live),
                )
            )
        for devid in list(self.devices):
            dev = self.devices[devid]
            if dev is None or dev.hnd is None:
                self.devices[devid] = None
                continue
            hnd = dev.hnd
            busid = dev.busid

            busid_label = busid or "?"
            # Cleanup is 3 steps (release → reset → close) but we
            # collapse them into one log line on success and break
            # out per-step warnings only when something fails. Step
            # detail is at trace/debug level for diagnostic deep-dives.
            reset_skipped = False

            # 1. release interfaces of the active configuration
            self.trace(f"  {busid_label}: releasing interfaces")
            try:
                dev_obj = hnd.getDevice()
                try:
                    bConfigVal = hnd.getConfiguration()
                except BaseException:
                    bConfigVal = None
                for _config in dev_obj.iterConfigurations():
                    if bConfigVal is None or _config.getConfigurationValue() == bConfigVal:
                        for i in range(_config.getNumInterfaces()):
                            try:
                                hnd.releaseInterface(i)
                            except BaseException:
                                pass
                        break
            except BaseException as e:
                self.say(f"  {busid_label}: release failed: {e}")

            # 2. USB port reset so the next host-side libusb client
            #    (probe-rs, STM32_Programmer_CLI) can open cleanly.
            self.trace(f"  {busid_label}: resetting device")
            try:
                hnd.resetDevice()
            except (usb1.USBErrorNoDevice, usb1.USBErrorNotFound):
                # Expected when the device was unplugged before
                # disconnect — surface in the summary line, not as
                # a separate scary error.
                reset_skipped = True
            except BaseException as e:
                self.say(f"  {busid_label}: reset failed: {e}")

            # 3. close handle
            self.trace(f"  {busid_label}: closing handle")
            try:
                hnd.close()
            except BaseException as e:
                self.say(f"  {busid_label}: close failed: {e}")

            self.say(
                "  {}: {}".format(
                    busid_label,
                    "released, closed (device was gone — no reset)"
                    if reset_skipped
                    else "released, reset, closed",
                )
            )
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
        vid_filter: set | None = None,
        registry: Registry | None = None,
        require_bind: bool = False,
        event_bus: ev.EventBus | None = None,
    ):
        self.loop = loop
        self.usbctx = usbctx
        self.host = host
        self.port = port
        self.vid_filter = vid_filter
        self.registry = registry
        self.require_bind = require_bind
        self.event_bus = event_bus
        self._server: asyncio.base_events.Server | None = None
        self._hotplug_handle = None
        # busid -> peer-string. Mutated only from the asyncio loop
        # thread (USBIPConnection._on_attach/_on_detach are called
        # synchronously from handle_op_import / cleanup), so no lock.
        self.attached_by_busid: dict[str, str] = {}
        # Active client-handler tasks. We track them so stop() can
        # cancel any still-pending tasks after the bounded
        # wait_closed timeout — otherwise Python's asyncio prints
        # "Task was destroyed but it is pending!" on a forced exit.
        self._client_tasks: set[asyncio.Task] = set()

    def _track_attach(self, busid: str, peer: str) -> None:
        self.attached_by_busid[busid] = peer

    def _track_detach(self, busid: str, _peer: str) -> None:
        self.attached_by_busid.pop(busid, None)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Register our own task so stop() can cancel it on forced
        # shutdown. asyncio.current_task() returns the task running
        # this coroutine. The discard-on-done callback keeps the set
        # bounded across long sessions.
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
            task.add_done_callback(self._client_tasks.discard)

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
                self._hotplug_handle = self.usbctx.hotplugRegisterCallback(self._on_hotplug)
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
        kind = ev.DEVICE_ADDED if event == usb1.HOTPLUG_EVENT_DEVICE_ARRIVED else ev.DEVICE_REMOVED
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
        active_busids = list(self.attached_by_busid.keys())
        if self._server is not None:
            logger.info(
                "stopping USB/IP server on %s:%d (%d active client(s)%s)",
                self.host,
                self.port,
                len(active_busids),
                ": " + ", ".join(active_busids) if active_busids else "",
            )
            self._server.close()
            # Bounded wait. In Python 3.12.1+, wait_closed() waits for
            # active client handlers to finish — and our handlers don't
            # finish until the client closes its end. On normal Ctrl-C
            # shutdown the operator wants a fast exit, not a graceful
            # drain, so cap at 2s.
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
                logger.info("USB/IP server stopped cleanly")
            except asyncio.TimeoutError:
                # Cancel any still-running client handlers explicitly.
                # Without this, Python prints "Task was destroyed but
                # it is pending!" at process exit because the handler
                # tasks were holding a libusb device and the asyncio
                # task object was reaped without being awaited.
                pending = [t for t in self._client_tasks if not t.done()]
                if pending:
                    logger.warning(
                        "USB/IP server didn't drain in 2s — cancelling "
                        "%d client task(s) (devices: %s)",
                        len(pending),
                        ", ".join(active_busids) or "—",
                    )
                    for t in pending:
                        t.cancel()
                    # Brief wait for cancellation to propagate through
                    # the handlers' _cleanup_devices(). Capped so a
                    # stuck libusb close can't hang the shutdown.
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*pending, return_exceptions=True),
                            timeout=1.0,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "client task(s) didn't cancel in 1s; OS will "
                            "reap sockets at process exit"
                        )
            except BaseException:
                pass
        if self._hotplug_handle is not None:
            try:
                self.usbctx.hotplugDeregisterCallback(self._hotplug_handle)
            except Exception:
                pass
