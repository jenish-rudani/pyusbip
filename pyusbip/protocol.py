"""USB/IP wire-protocol constants and shared exception types.

Kept narrow on purpose — this module is pure data plus exception classes
so that `server.py`, `control.py`, and any future tooling can import it
without dragging in libusb or asyncio.

References:
  - usbip protocol: https://docs.kernel.org/usb/usbip_protocol.html
  - userland tools: https://github.com/torvalds/linux/tree/master/tools/usb/usbip
"""

# Two distinct namespaces share the same 16-bit field on the wire:
#   * for op_common (DEVLIST/IMPORT etc): the field is the protocol
#     version, currently 0x0111
#   * for URB headers (CMD_SUBMIT / CMD_UNLINK): the field is the
#     command, where the high byte is zero — that's how the dispatcher
#     in USBIPConnection.handle_packet distinguishes the two.
USBIP_VERSION = 0x0111

USBIP_REQUEST = 0x8000
USBIP_REPLY = 0x0000

USBIP_OP_UNSPEC = 0x00
USBIP_OP_DEVINFO = 0x02
USBIP_OP_IMPORT = 0x03
USBIP_OP_EXPORT = 0x06
USBIP_OP_UNEXPORT = 0x07
USBIP_OP_DEVLIST = 0x05

USBIP_CMD_SUBMIT = 0x0001
USBIP_CMD_UNLINK = 0x0002
USBIP_RET_SUBMIT = 0x0003
USBIP_RET_UNLINK = 0x0004
USBIP_RESET_DEV = 0xFFFF

USBIP_DIR_OUT = 0
USBIP_DIR_IN = 1

USBIP_ST_OK = 0x00
USBIP_ST_NA = 0x01

USBIP_BUS_ID_SIZE = 32
USBIP_DEV_PATH_MAX = 256

USBIP_SPEED_UNKNOWN = 0
USBIP_SPEED_LOW = 1
USBIP_SPEED_FULL = 2
USBIP_SPEED_HIGH = 3
USBIP_SPEED_VARIABLE = 4

# Standard USB request constants we synthesize responses for. The Linux
# usbip-host kernel module hides these from us on the wire (SET_ADDRESS,
# SET_CONFIGURATION, SET_INTERFACE go through the host stack), but
# pyusbip is a userspace server and has to fake the responses itself.
USB_RECIP_DEVICE = 0x00
USB_RECIP_INTERFACE = 0x01
USB_REQ_SET_ADDRESS = 0x05
USB_REQ_SET_CONFIGURATION = 0x09
USB_REQ_SET_INTERFACE = 0x0B

# errno values used in USBIP_RET_SUBMIT.status — kept here rather than
# importing from `errno` so the wire constants are co-located with the
# rest of the protocol.
USB_ENOENT = 2
USB_EPIPE = 32


class USBIPUnimplementedException(Exception):
    """Raised when a USB/IP request asks for a feature pyusbip hasn't
    implemented (e.g. isochronous transfers, USBIP_RESET_DEV). Caught
    by USBIPConnection.connection() which logs a traceback and closes
    the connection so the client can retry."""


class USBIPProtocolErrorException(Exception):
    """Raised on a structural protocol violation — wrong version,
    unknown opcode, devid referring to a never-IMPORTed device, etc.
    These almost always indicate a misbehaving client and cause the
    server to drop the connection."""
