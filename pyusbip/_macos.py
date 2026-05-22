"""macOS-only: force a USB device re-enumeration via IOKit.

`libusb_reset_device()` on macOS only triggers a real re-enumeration
(`USBDeviceReEnumerate`) when it detects that the device descriptors
changed between its cached copy and the live device. For a stable
device (STLINK-V3, etc.) the descriptors never change across a USB/IP
detach, so libusb's darwin backend falls back to a plain
`IOUSBDevice::ResetDevice()` — a port-level USB reset that does *not*
cause IOKit to re-publish `IOUSBHostInterface` nubs.

Concretely: after a USB/IP detach, the device sits in IOReg with
`kUSBCurrentConfiguration=1` and *zero* interface children. Class
drivers (`AppleUSBCDCACM`, `IOUSBMassStorageDriver`, `…HostHIDDevice`)
never re-match, so `/dev/tty.usbmodem*` and the device's storage volume
don't come back — physical replug is the only recovery.

This module bypasses libusb and calls IOKit's `USBDeviceReEnumerate(0)`
directly, which IS the full re-enumeration we want. The cleanup path
in `server.py` invokes it AFTER `libusb_close()` so IOKit can take
an exclusive open without contending with libusb.

Bindings are pure-stdlib `ctypes` — adding `pyobjc` as a runtime dep
for what amounts to five function calls is not worth the ~30MB. The
COM-style vtable on `IOUSBDeviceInterface` is dispatched manually;
indices below match Apple's `IOUSBLib.h` and have been ABI-stable
since macOS 10.0 (new methods only get appended at the end of
derivative interfaces 182/187/197/245/300/500).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger("pyusbip.macos")

_ENABLED = sys.platform == "darwin"


def reenumerate_device(vid: int, pid: int, serial: str | None = None) -> bool:
    """Force a USB re-enumeration of devices matching (vid, pid[, serial]).

    Returns True if at least one device was successfully re-enumerated,
    False on no match / IOKit error / non-macOS platform. Logging
    captures the per-step failure.

    Passing `serial` (the USB serial-number string) is strongly
    recommended when more than one device might share the VID/PID;
    without it, every matching device gets kicked.
    """
    if not _ENABLED:
        return False
    return _reenumerate(vid, pid, serial)


if _ENABLED:
    # ------------------------------------------------------------------
    # Framework loading
    # ------------------------------------------------------------------
    _cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    _iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))

    # ------------------------------------------------------------------
    # Type aliases (opaque pointers / 32-bit IDs all the way down)
    # ------------------------------------------------------------------
    CFTypeRef = ctypes.c_void_p
    CFAllocatorRef = ctypes.c_void_p
    CFStringRef = ctypes.c_void_p
    CFNumberRef = ctypes.c_void_p
    CFUUIDRef = ctypes.c_void_p
    CFMutableDictionaryRef = ctypes.c_void_p

    io_object_t = ctypes.c_uint32
    io_service_t = ctypes.c_uint32
    io_iterator_t = ctypes.c_uint32
    kern_return_t = ctypes.c_int32
    IOReturn = kern_return_t
    mach_port_t = ctypes.c_uint32
    HRESULT = ctypes.c_int32

    class CFUUIDBytes(ctypes.Structure):
        """16-byte UUID, passed BY VALUE to COM-style `QueryInterface`."""

        _fields_ = [(f"b{i}", ctypes.c_uint8) for i in range(16)]

    # ------------------------------------------------------------------
    # CoreFoundation prototypes
    # ------------------------------------------------------------------
    _cf.CFNumberCreate.restype = CFNumberRef
    _cf.CFNumberCreate.argtypes = [CFAllocatorRef, ctypes.c_int32, ctypes.c_void_p]

    _cf.CFStringCreateWithCString.restype = CFStringRef
    _cf.CFStringCreateWithCString.argtypes = [CFAllocatorRef, ctypes.c_char_p, ctypes.c_uint32]

    _cf.CFDictionarySetValue.restype = None
    _cf.CFDictionarySetValue.argtypes = [CFMutableDictionaryRef, CFTypeRef, CFTypeRef]

    _cf.CFRelease.restype = None
    _cf.CFRelease.argtypes = [CFTypeRef]

    _cf.CFUUIDCreateFromUUIDBytes.restype = CFUUIDRef
    _cf.CFUUIDCreateFromUUIDBytes.argtypes = [CFAllocatorRef, CFUUIDBytes]

    _cf.CFStringGetCString.restype = ctypes.c_bool
    _cf.CFStringGetCString.argtypes = [CFStringRef, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]

    # ------------------------------------------------------------------
    # IOKit prototypes
    # ------------------------------------------------------------------
    _iokit.IOServiceMatching.restype = CFMutableDictionaryRef
    _iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]

    _iokit.IOServiceGetMatchingServices.restype = kern_return_t
    _iokit.IOServiceGetMatchingServices.argtypes = [
        mach_port_t,
        CFMutableDictionaryRef,
        ctypes.POINTER(io_iterator_t),
    ]

    _iokit.IOIteratorNext.restype = io_object_t
    _iokit.IOIteratorNext.argtypes = [io_iterator_t]

    _iokit.IOObjectRelease.restype = kern_return_t
    _iokit.IOObjectRelease.argtypes = [io_object_t]

    _iokit.IORegistryEntryCreateCFProperty.restype = CFTypeRef
    _iokit.IORegistryEntryCreateCFProperty.argtypes = [
        io_service_t,
        CFStringRef,
        CFAllocatorRef,
        ctypes.c_uint32,
    ]

    _iokit.IOCreatePlugInInterfaceForService.restype = kern_return_t
    _iokit.IOCreatePlugInInterfaceForService.argtypes = [
        io_service_t,
        CFUUIDRef,
        CFUUIDRef,
        ctypes.POINTER(ctypes.c_void_p),  # IOCFPlugInInterface**
        ctypes.POINTER(ctypes.c_int32),  # SInt32* score
    ]

    # ------------------------------------------------------------------
    # Constants
    # ------------------------------------------------------------------
    _kCFNumberSInt32Type = 3
    _kCFStringEncodingUTF8 = 0x08000100
    _kIOMainPortDefault = 0  # historically kIOMasterPortDefault; value unchanged

    # UUIDs from <IOKit/IOCFPlugIn.h>, <IOKit/usb/IOUSBLib.h>.
    # Stored as hex strings to keep them line-stable through ruff's
    # auto-formatter (a 16-tuple of bytes gets one-per-line expanded).
    def _uuid(hexstr: str) -> CFUUIDBytes:
        u = CFUUIDBytes()
        for i, v in enumerate(bytes.fromhex(hexstr)):
            setattr(u, f"b{i}", v)
        return u

    _kIOCFPlugInInterfaceID = _uuid("C244E858109C11D491D40050E4C6426F")
    _kIOUSBDeviceUserClientTypeID = _uuid("9DC7B7809EC011D4A54F000A27052861")
    _kIOUSBDeviceInterfaceID500 = _uuid("A33CF0474B5B48E2B57D0207FCEAE13B")

    # Vtable indices for IOUSBDeviceInterface500. The first four slots
    # come from IUNKNOWN_C_GUTS (see CFPlugInCOM.h):
    #
    #   [0] void *_reserved     ← NOT a function — trying to call it
    #                              gives "must be callable or integer
    #                              function address" because it's NULL
    #   [1] QueryInterface
    #   [2] AddRef
    #   [3] Release
    #
    # Then IOUSBDeviceInterface500 methods start at index 4. Counted
    # against IOUSBLib.h in the macOS 15.4 SDK:
    #   [4..28]   IOUSBDeviceInterface (base)
    #   [29..36]  IOUSBDeviceInterface182 additions
    #   [37]      USBDeviceReEnumerate (added in IOUSBDeviceInterface187)
    #   [38..]    197/245/300/400/500 additions
    _IDX_QueryInterface = 1
    _IDX_Release = 3
    _IDX_USBDeviceOpen = 8
    _IDX_USBDeviceClose = 9
    _IDX_USBDeviceReEnumerate = 37

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _cfstr(s: str) -> CFStringRef:
        return _cf.CFStringCreateWithCString(None, s.encode("utf-8"), _kCFStringEncodingUTF8)

    def _cfnum_i32(v: int) -> CFNumberRef:
        n = ctypes.c_int32(v)
        return _cf.CFNumberCreate(None, _kCFNumberSInt32Type, ctypes.byref(n))

    _PTR_SIZE = ctypes.sizeof(ctypes.c_void_p)

    def _vmethod(obj: ctypes.c_void_p, idx: int, restype, argtypes):
        """Return a callable for vtable method `idx` of a COM-style
        double-pointer interface `obj`. The implicit `self` arg is
        bound automatically — call the returned function with the
        explicit method args only.

        Uses `c_void_p.from_address` rather than `ctypes.cast(int, ...)`
        because the latter returns a `c_void_p` instance on some
        Python versions and a raw int on others; `CFUNCTYPE(...)` only
        accepts an int, so the wrong return type explodes with
        "argument must be callable or integer function address".
        """
        obj_addr = obj.value if isinstance(obj, ctypes.c_void_p) else int(obj)
        if not obj_addr:
            raise RuntimeError("interface pointer is NULL")
        vtable_addr = ctypes.c_void_p.from_address(obj_addr).value
        if not vtable_addr:
            raise RuntimeError("vtable pointer is NULL")
        method_addr = ctypes.c_void_p.from_address(vtable_addr + idx * _PTR_SIZE).value
        if not method_addr:
            raise RuntimeError(f"vtable[{idx}] is NULL")
        fn_type = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        fn = fn_type(method_addr)
        return lambda *args: fn(obj, *args)

    def _read_string_prop(svc: int, key: str) -> str | None:
        key_ref = _cfstr(key)
        try:
            prop = _iokit.IORegistryEntryCreateCFProperty(svc, key_ref, None, 0)
            if not prop:
                return None
            try:
                buf = ctypes.create_string_buffer(512)
                if _cf.CFStringGetCString(prop, buf, len(buf), _kCFStringEncodingUTF8):
                    return buf.value.decode("utf-8", errors="replace")
                return None
            finally:
                _cf.CFRelease(prop)
        finally:
            _cf.CFRelease(key_ref)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def _reenumerate(vid: int, pid: int, serial: str | None) -> bool:
        # Try modern class first; fall back to legacy on older macOS.
        for class_name in (b"IOUSBHostDevice", b"IOUSBDevice"):
            if _reenumerate_one_class(class_name, vid, pid, serial):
                return True
        return False

    def _reenumerate_one_class(class_name: bytes, vid: int, pid: int, serial: str | None) -> bool:
        matching = _iokit.IOServiceMatching(class_name)
        if not matching:
            return False

        vid_ref = _cfnum_i32(vid)
        pid_ref = _cfnum_i32(pid)
        vid_key = _cfstr("idVendor")
        pid_key = _cfstr("idProduct")
        try:
            _cf.CFDictionarySetValue(matching, vid_key, vid_ref)
            _cf.CFDictionarySetValue(matching, pid_key, pid_ref)
        finally:
            _cf.CFRelease(vid_key)
            _cf.CFRelease(pid_key)
            _cf.CFRelease(vid_ref)
            _cf.CFRelease(pid_ref)

        # IOServiceGetMatchingServices consumes `matching` (ownership
        # transferred), so we must not release it ourselves on success.
        iter_ = io_iterator_t(0)
        kr = _iokit.IOServiceGetMatchingServices(_kIOMainPortDefault, matching, ctypes.byref(iter_))
        if kr != 0 or not iter_.value:
            return False

        success = False
        try:
            while True:
                svc = _iokit.IOIteratorNext(iter_)
                if not svc:
                    break
                try:
                    if serial:
                        s = _read_string_prop(svc, "kUSBSerialNumberString")
                        if s != serial:
                            continue
                    if _reenumerate_service(svc):
                        success = True
                finally:
                    _iokit.IOObjectRelease(svc)
        finally:
            _iokit.IOObjectRelease(iter_)

        return success

    def _reenumerate_service(svc: int) -> bool:
        plugin_uuid = _cf.CFUUIDCreateFromUUIDBytes(None, _kIOUSBDeviceUserClientTypeID)
        iface_uuid = _cf.CFUUIDCreateFromUUIDBytes(None, _kIOCFPlugInInterfaceID)
        plugin = ctypes.c_void_p()
        score = ctypes.c_int32()
        try:
            kr = _iokit.IOCreatePlugInInterfaceForService(
                svc, plugin_uuid, iface_uuid, ctypes.byref(plugin), ctypes.byref(score)
            )
        finally:
            _cf.CFRelease(plugin_uuid)
            _cf.CFRelease(iface_uuid)

        if kr != 0 or not plugin.value:
            logger.debug("IOCreatePlugInInterfaceForService failed: 0x%x", kr & 0xFFFFFFFF)
            return False

        # plugin->QueryInterface(plugin, kIOUSBDeviceInterfaceID500, &dev)
        qi = _vmethod(
            plugin,
            _IDX_QueryInterface,
            HRESULT,
            [CFUUIDBytes, ctypes.POINTER(ctypes.c_void_p)],
        )
        dev = ctypes.c_void_p()
        try:
            hr = qi(_kIOUSBDeviceInterfaceID500, ctypes.byref(dev))
        finally:
            _vmethod(plugin, _IDX_Release, ctypes.c_uint32, [])()

        if hr != 0 or not dev.value:
            logger.debug("QueryInterface(kIOUSBDeviceInterfaceID500) failed: 0x%x", hr & 0xFFFFFFFF)
            return False

        try:
            kr = _vmethod(dev, _IDX_USBDeviceOpen, IOReturn, [])()
            if kr != 0:
                logger.debug(
                    "USBDeviceOpen failed: 0x%x (process needs USB access — try running as root)",
                    kr & 0xFFFFFFFF,
                )
                return False

            kr = _vmethod(dev, _IDX_USBDeviceReEnumerate, IOReturn, [ctypes.c_uint32])(0)
            if kr != 0:
                logger.debug("USBDeviceReEnumerate failed: 0x%x", kr & 0xFFFFFFFF)
                # Best-effort close on failure (on success the device
                # is being torn down so closing would race).
                try:
                    _vmethod(dev, _IDX_USBDeviceClose, IOReturn, [])()
                except Exception:
                    pass
                return False
            return True
        finally:
            _vmethod(dev, _IDX_Release, ctypes.c_uint32, [])()


# ----------------------------------------------------------------------
# CLI / smoke-test entrypoint:  python -m pyusbip._macos 0x0483 0x3754
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(prog="pyusbip._macos")
    p.add_argument("vid", help="USB vendor ID (hex, e.g. 0x0483)")
    p.add_argument("pid", help="USB product ID (hex, e.g. 0x3754)")
    p.add_argument("--serial", help="optional serial-number filter", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    vid = int(args.vid, 0)
    pid = int(args.pid, 0)
    ok = reenumerate_device(vid, pid, args.serial)
    print(f"reenumerate_device(0x{vid:04x}, 0x{pid:04x}) -> {ok}")
    sys.exit(0 if ok else 1)
