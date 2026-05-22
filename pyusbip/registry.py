"""Bind allowlist with JSON persistence.

The registry is the device-agnostic answer to "should this USB device
be exported to USB/IP clients?" Entries are `Match` records — any of
{vid, pid, serial} can be set; unset fields act as wildcards. A device
"matches" an entry when every set field equals the device's value.

This is the macOS-side analogue of Windows usbipd-win's persistent
"bind" state. Operators add entries via `pyusbip --bind` (CLI) or via
the HTTP control plane's `POST /bind` (GUI); entries survive pyusbip
restarts because they're written to a JSON file (default
`/etc/pyusbip/shared.json`).

Wildcard semantics deliberately match the common workflows:

  * Bind a model (every ST-Link V3 MINIE):
        Match(vid=0x0483, pid=0x3754)
  * Bind one specific probe (this physical ST-Link only):
        Match(vid=0x0483, pid=0x3754, serial="004C0032...")
  * Bind every USB device from a vendor (every STMicro device):
        Match(vid=0x0483)

`is_allowed(device)` short-circuits on the first matching entry, so an
operator who narrows their allowlist later doesn't need to remove
broader entries first.
"""

from __future__ import annotations

import builtins
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("pyusbip.registry")


@dataclass(frozen=True)
class Match:
    """One allowlist entry. Frozen so it's hashable and safe to keep in
    a set; fields are Optional[int|str] so unset == wildcard."""

    vid: int | None = None
    pid: int | None = None
    serial: str | None = None

    def matches(self, vid: int, pid: int, serial: str) -> bool:
        if self.vid is not None and self.vid != vid:
            return False
        if self.pid is not None and self.pid != pid:
            return False
        if self.serial is not None and self.serial != serial:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.vid is not None:
            out["vid"] = f"0x{self.vid:04x}"
        if self.pid is not None:
            out["pid"] = f"0x{self.pid:04x}"
        if self.serial is not None:
            out["serial"] = self.serial
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Match:
        def _parse_int(v):
            if v is None:
                return None
            if isinstance(v, int):
                return v
            return int(v, 0)  # auto-detect 0x prefix

        return cls(
            vid=_parse_int(data.get("vid")),
            pid=_parse_int(data.get("pid")),
            serial=data.get("serial"),
        )


@dataclass
class Registry:
    """In-memory allowlist with optional JSON persistence.

    Thread-safe: a single mutex guards mutations so the libusb event
    thread (hotplug callback) and the asyncio loop (HTTP control
    plane) can both query/modify without races.
    """

    path: str | None = None  # None == in-memory only
    entries: builtins.list[Match] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Listeners invoked after every mutation. Used by the server to
    # publish BIND_CHANGED events without registry needing to import
    # the EventBus directly (keeps the dependency one-way).
    _listeners: builtins.list[Callable[[], None]] = field(default_factory=list, repr=False)

    def add_listener(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                logger.exception("registry listener raised")

    def load(self) -> None:
        """Read entries from `self.path`. Missing file == empty
        registry. Malformed JSON is logged and ignored — pyusbip
        should still start so the operator can fix the file via the
        control plane."""
        if not self.path:
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            logger.info("registry: no shared file at %s, starting empty", self.path)
            return
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("registry: failed to load %s: %s; starting empty", self.path, e)
            return

        with self._lock:
            self.entries = [Match.from_dict(d) for d in data.get("entries", [])]
        logger.info("registry: loaded %d entries from %s", len(self.entries), self.path)

    def save(self) -> None:
        """Atomically write the current allowlist to `self.path`.
        No-op when path is None. Errors are logged but not raised —
        bind state is recoverable; killing the server because we
        can't write a config file is worse than losing the entry."""
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError as e:
            logger.warning("registry: cannot create dir for %s: %s", self.path, e)
            return

        tmp = self.path + ".tmp"
        try:
            with self._lock:
                payload = {"entries": [m.to_dict() for m in self.entries]}
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("registry: failed to save %s: %s", self.path, e)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def bind(self, match: Match) -> bool:
        """Add an entry. Returns True if added, False if it was
        already present (idempotent — safe to call repeatedly with
        the same Match)."""
        with self._lock:
            if match in self.entries:
                return False
            self.entries.append(match)
        self.save()
        self._notify()
        logger.info("registry: bind %s", match)
        return True

    def unbind(self, match: Match) -> bool:
        """Remove an entry. Returns True if it was present and
        removed; False if no such entry."""
        with self._lock:
            try:
                self.entries.remove(match)
            except ValueError:
                return False
        self.save()
        self._notify()
        logger.info("registry: unbind %s", match)
        return True

    def is_allowed(self, vid: int, pid: int, serial: str) -> bool:
        """Returns True if any entry matches the given device. An
        empty registry returns False — callers wanting "allow all
        when empty" semantics should check `bool(self)` first."""
        with self._lock:
            for m in self.entries:
                if m.matches(vid, pid, serial):
                    return True
        return False

    def list(self) -> builtins.list[Match]:
        with self._lock:
            return list(self.entries)

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self.entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self.entries)
