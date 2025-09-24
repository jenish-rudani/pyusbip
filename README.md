# PyUSBIP

A Python implementation of USB/IP server that allows sharing USB devices over the network.

## Description

PyUSBIP is a USB/IP server implementation written in Python that enables you to share USB devices over a network connection. It uses the USB/IP protocol to make local USB devices available to remote systems.

## Features

- USB/IP server implementation
- Asynchronous USB device handling
- Support for control, bulk transfers
- Device enumeration and import/export
- Compatible with standard USB/IP clients

## Installation

### From PyPI (when published)

```bash
pip install pyusbip
```

### From Source

```bash
git clone <repository-url>
cd pyusbip
pip install .
```

### Development Installation

```bash
pip install -e .
```

## Dependencies

- Python 3.6+
- python-libusb1

## Usage

### As a Script

```bash
pyusbip
```

### As a Module

```python
import pyusbip
# Your code here
```

### Configuration

By default, the server listens on `127.0.0.1:3240`. You can modify the `USBIP_HOST` and `USBIP_PORT` variables in the code to change this.

## Requirements

- Root/administrator privileges may be required for USB device access
- USB devices should not be in use by other applications

## License

This project is open source. Please check the license file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
