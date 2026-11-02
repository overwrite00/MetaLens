"""In-memory journal of the exact Undo (plan F3B, decision D1).

Each successful ``/write`` or ``/delete`` that actually changed something gets
one entry, addressed by an opaque random token handed to the renderer:

* ``path``: the resolved target (the client never sends a path to ``/undo``);
* ``handler_name``: the format handler that produced the snapshots;
* ``identity``: ``(st_dev, st_ino)`` of the file after the operation, kept in
  step by the API after each of its own transactions on the same file, so a
  file replaced by another program is detected;
* ``pre``: ``{layer: {key: raw pre-image | ABSENT}}`` from ``handler.snapshot``
  before the mutation (what ``handler.restore`` puts back);
* ``post``: ``{layer: {key: raw post-image | ABSENT}}`` right after it (what the
  key must still hold for the Undo to be allowed);
* ``patch``: optional ``byte_patch.BytePatch`` that gives the original bytes of the
  changed region back (an Undo that is identical to the file before the save, not
  only equal in values); the handler restore is the fallback when it no longer applies.

The journal lives only in memory: a restarted sidecar answers 410 to old
tokens. Limits: ``max_entries`` entries and ``max_bytes`` of snapshot data
(pre + post images), oldest evicted first (FIFO). An entry larger than the
whole byte budget is not recorded: ``add_entry`` raises ``UndoTooLargeError``
(with the sizes, so the API can tell the user the change has no Undo),
``add`` returns ``None``. The size is measured before anything is copied.
Thread-safe; callers receive deep copies, never the stored snapshots.
"""
from __future__ import annotations

import copy
import pickle
import secrets
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

__all__ = ["UndoEntry", "UndoJournal", "UndoTooLargeError", "MAX_ENTRIES", "MAX_BYTES", "snapshot_size"]

MAX_ENTRIES = 50
MAX_BYTES = 64 * 1024 * 1024

Layers = dict[str, dict[str, Any]]   # layer -> {key: raw | ABSENT}


class UndoTooLargeError(ValueError):
    """The pre + post images of one operation exceed the whole journal budget."""

    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"undo entry of {size} bytes exceeds the {limit} bytes journal budget")


@dataclass
class UndoEntry:
    token: str
    path: Path
    handler_name: str
    identity: tuple
    pre: Layers
    post: Layers
    size: int
    patch: Any = None

    @property
    def keys(self) -> list[str]:
        return [key for layer in self.pre.values() for key in layer]


def snapshot_size(value: Any) -> int:
    """Approximate memory cost of a snapshot (bytes)."""
    try:
        return len(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
    except Exception:
        return _deep_size(value, set())


def _deep_size(value: Any, seen: set[int]) -> int:
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value, 64)
    if isinstance(value, (bytes, bytearray, memoryview, str)):
        return size
    if isinstance(value, dict):
        return size + sum(_deep_size(k, seen) + _deep_size(v, seen) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return size + sum(_deep_size(v, seen) for v in value)
    attrs = getattr(value, "__dict__", None)
    return size + (_deep_size(attrs, seen) if isinstance(attrs, dict) else 0)


class UndoJournal:
    def __init__(self, max_entries: int = MAX_ENTRIES, max_bytes: int = MAX_BYTES) -> None:
        if max_entries < 1 or max_bytes < 1:
            raise ValueError("journal limits must be positive")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries: OrderedDict[str, UndoEntry] = OrderedDict()
        self._bytes = 0
        self._lock = Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return self._bytes

    def add(self, path: Path, handler_name: str, identity: tuple, pre: Layers, post: Layers,
            patch: Any = None) -> str | None:
        """Record one operation; returns its token, or ``None`` if it exceeds the budget."""
        try:
            return self.add_entry(path, handler_name, identity, pre, post, patch)
        except UndoTooLargeError:
            return None

    def add_entry(self, path: Path, handler_name: str, identity: tuple, pre: Layers, post: Layers,
                  patch: Any = None) -> str:
        """Record one operation and return its token; ``UndoTooLargeError`` if it
        exceeds the whole byte budget (nothing is recorded or evicted then)."""
        size = snapshot_size(pre) + snapshot_size(post)
        if size > self.max_bytes:
            raise UndoTooLargeError(size, self.max_bytes)
        pre = copy.deepcopy(pre)
        post = copy.deepcopy(post)
        token = secrets.token_urlsafe(24)
        with self._lock:
            # The byte patch is a bonus: it never costs other entries their place. Without room
            # for it (or for an entry's values alone beyond the budget) the Undo keeps the values.
            if patch is not None:
                if self._bytes + size + len(patch) > self.max_bytes:
                    patch = None
                else:
                    size += len(patch)
            entry = UndoEntry(token, Path(path), handler_name, tuple(identity), pre, post, size, patch)
            self._entries[token] = entry
            self._bytes += size
            while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
                _, old = self._entries.popitem(last=False)
                self._bytes -= old.size
        return token

    def get(self, token: str) -> UndoEntry | None:
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return None
            pre, post = entry.pre, entry.post
            result = UndoEntry(entry.token, entry.path, entry.handler_name, entry.identity,
                               pre, post, entry.size, entry.patch)
        result.pre = copy.deepcopy(pre)
        result.post = copy.deepcopy(post)
        return result

    def pop(self, token: str) -> UndoEntry | None:
        with self._lock:
            entry = self._entries.pop(token, None)
            if entry is not None:
                self._bytes -= entry.size
            return entry

    def update_identity(self, path: Path, old: tuple, new: tuple) -> None:
        """After one of our own transactions on ``path``: entries that matched the
        file before it now follow the new file (stale ones stay stale -> 409)."""
        path = Path(path)
        old, new = tuple(old), tuple(new)
        with self._lock:
            for entry in self._entries.values():
                if entry.path == path and entry.identity == old:
                    entry.identity = new

    def update_mtime(self, path: Path, key: str, old: Any, new: Any) -> None:
        """After one of our own transactions that rewrote ``path``: entries whose
        post-value of the modification time was the old one now expect the new one
        (any rewrite moves the mtime; only a change by someone else stays a conflict)."""
        if old == new:
            return
        path = Path(path)
        with self._lock:
            for entry in self._entries.values():
                if entry.path != path:
                    continue
                for layer in entry.post.values():
                    if key in layer and layer[key] == old:
                        layer[key] = new

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0
