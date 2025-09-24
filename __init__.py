"""
PyUSBIP - Python USB/IP Server Implementation

A Python implementation of USB/IP server that allows sharing USB devices over the network.
"""

__version__ = "0.1.0"
__author__ = "Jenish-Rudani"
__email__ = "jrudani1999@gmail.com"

# Import main function from the module
try:
    from .pyusbip import main
except ImportError:
    # Fallback for when running as a script
    from pyusbip import main

__all__ = ["main"]
