#!/usr/bin/env python3

from setuptools import setup, find_packages
import os

# Read the README file


def read_readme():
    with open(os.path.join(os.path.dirname(__file__), 'README.md'), 'r', encoding='utf-8') as f:
        return f.read()


setup(
    name="pyusbip",
    version="0.1.0",
    author="Jenish-Rudani",
    author_email="jrudani1999@gmail.com",
    description="A Python implementation of USB/IP server for sharing USB devices over network",
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/jenish-rudani/pyusbip",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: System :: Hardware :: Universal Serial Bus (USB)",
        "Topic :: System :: Networking",
    ],
    python_requires=">=3.6",
    install_requires=[
        "libusb1>=3.0.0",
    ],
    extras_require={
        "dev": [
            "pytest>=6.0",
            "pytest-asyncio",
            "black",
            "flake8",
        ],
    },
    entry_points={
        "console_scripts": [
            "pyusbip=pyusbip:main",
        ],
    },
    keywords="usb usbip networking hardware",
    project_urls={
        "Bug Reports": "https://github.com/jenish-rudani/pyusbip/issues",
        "Source": "https://github.com/jenish-rudani/pyusbip",
    },
)
