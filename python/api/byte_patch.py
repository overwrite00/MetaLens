"""Byte-level pre-image of a save, for an Undo that gives the original bytes back.

A save rewrites the tag area of a file; everything before and after it (usually the
audio/pixel data, in front of or behind the tags) stays identical, even when the
tags grow or shrink and the rest of the file shifts. ``make_patch`` compares the
file as it was with the file as saved and keeps only the *middle* that differs:

* ``prefix`` / ``suffix``: lengths of the identical head and tail;
* ``middle``: the original bytes between them (what the Undo puts back);
* ``post_size`` / ``post_digest``: size and SHA-256 of the saved middle, so the
  patch only applies to the file exactly as the save left it (any other change
  of that region makes ``apply_patch`` refuse and the value-level restore of the
  handler take over).

Memory cost is the size of the changed region, not of the file. Files above
``MAX_FILE_SIZE`` and changed regions above ``MAX_MIDDLE`` get no patch.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["BytePatch", "make_patch", "apply_patch", "MAX_FILE_SIZE", "MAX_MIDDLE"]

MAX_FILE_SIZE = 1024 ** 3        # 1 GiB: the scan reads the file once
MAX_MIDDLE = 16 * 1024 ** 2      # changed region kept in memory
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class BytePatch:
    prefix: int
    suffix: int
    middle: bytes          # original bytes between prefix and suffix
    post_size: int         # size of the file as saved
    post_digest: str       # sha256 of the saved bytes between prefix and suffix

    def __len__(self) -> int:    # journal budget accounting
        return len(self.middle) + 128


def _common_prefix(a, b, limit: int) -> int:
    done = 0
    while done < limit:
        n = min(_CHUNK, limit - done)
        ca, cb = a.read(n), b.read(n)
        if ca != cb:
            for i in range(min(len(ca), len(cb))):
                if ca[i] != cb[i]:
                    return done + i
            return done + min(len(ca), len(cb))
        if not ca:
            break
        done += len(ca)
    return done


def _common_suffix(a, size_a: int, b, size_b: int, limit: int) -> int:
    done = 0
    while done < limit:
        n = min(_CHUNK, limit - done)
        a.seek(size_a - done - n)
        b.seek(size_b - done - n)
        ca, cb = a.read(n), b.read(n)
        if ca != cb:
            for i in range(1, n + 1):
                if ca[-i] != cb[-i]:
                    return done + i - 1
        done += n
    return done


def make_patch(before: Path, after: Path) -> BytePatch | None:
    """Patch that turns ``after`` back into ``before``; ``None`` when not worth keeping
    or when the files cannot be read."""
    try:
        size_b, size_a = os.path.getsize(before), os.path.getsize(after)
        if max(size_b, size_a) > MAX_FILE_SIZE:
            return None
        with open(before, "rb") as fb, open(after, "rb") as fa:
            limit = min(size_b, size_a)
            prefix = _common_prefix(fb, fa, limit)
            suffix = _common_suffix(fb, size_b, fa, size_a, limit - prefix)
            if size_b - prefix - suffix > MAX_MIDDLE:
                return None
            fb.seek(prefix)
            middle = fb.read(size_b - prefix - suffix)
            fa.seek(prefix)
            digest = _digest(fa, size_a - prefix - suffix)
    except (OSError, ValueError, IndexError):
        # A file that changed or shrank while it was read (short reads, negative seeks):
        # the Undo then keeps the values, as it does without a patch.
        return None
    return BytePatch(prefix, suffix, middle, size_a, digest)


def _digest(stream, length: int) -> str:
    h = hashlib.sha256()
    while length > 0:
        chunk = stream.read(min(_CHUNK, length))
        if not chunk:
            break
        h.update(chunk)
        length -= len(chunk)
    return h.hexdigest()


def apply_patch(path: Path, patch: BytePatch) -> bool:
    """Put the original bytes back into ``path``. ``False`` (file untouched) when
    ``path`` is not exactly the file the patch was made from.

    An ``OSError`` is NOT swallowed: it may come after the first byte was written,
    so ``path`` can be half patched and the caller must discard or restore it."""
    size = os.path.getsize(path)
    if size != patch.post_size or size < patch.prefix + patch.suffix:
        return False
    old_len = size - patch.prefix - patch.suffix
    with open(path, "r+b") as stream:
        stream.seek(patch.prefix)
        if _digest(stream, old_len) != patch.post_digest:
            return False
        new_len = len(patch.middle)
        if new_len != old_len:
            _move_tail(stream, size, patch.suffix, old_len, new_len, patch.prefix)
        stream.seek(patch.prefix)
        stream.write(patch.middle)
        stream.flush()
        os.fsync(stream.fileno())
    return True


def _move_tail(stream, size: int, suffix: int, old_len: int, new_len: int, prefix: int) -> None:
    """Shift the last ``suffix`` bytes from ``prefix+old_len`` to ``prefix+new_len``."""
    src, dst = prefix + old_len, prefix + new_len
    if dst > src:                                   # grow: copy from the end
        remaining = suffix
        while remaining > 0:
            n = min(_CHUNK, remaining)
            stream.seek(src + remaining - n)
            chunk = stream.read(n)
            stream.seek(dst + remaining - n)
            stream.write(chunk)
            remaining -= n
    else:                                           # shrink: copy from the start
        done = 0
        while done < suffix:
            n = min(_CHUNK, suffix - done)
            stream.seek(src + done)
            chunk = stream.read(n)
            stream.seek(dst + done)
            stream.write(chunk)
            done += n
        stream.truncate(dst + suffix)
