"""Responses never carry the raw text of an exception (paths, library internals)."""
import errno

import pytest

from core.errors import describe_error


@pytest.mark.parametrize("exc, expected", [
    (PermissionError(errno.EACCES, "denied: C:/secret/place/file.mp3", "C:/secret/place/file.mp3"), "permission denied"),
    (FileNotFoundError(errno.ENOENT, "no such file", "/home/user/x"), "no such file or directory"),
    (OSError("odd failure in /home/user/x"), "input/output error"),
    (MemoryError("big"), "not enough memory"),
    (ValueError("bad /home/user/x"), "unexpected value in the file"),
    (RuntimeError("boom /home/user/x"), "unexpected error"),
])
def test_describe_error_uses_fixed_vocabulary(exc, expected):
    text = describe_error(exc)
    assert text == expected
    assert not any(leak in text for leak in ("secret", "home", "C:"))


def test_post_commit_warnings_do_not_leak_the_exception_text(tmp_path, monkeypatch):
    """A failure while restoring metadata after the commit is reported with a fixed
    description; the detail (here a path) goes to the log only."""
    import core.path_security as security
    from core.path_security import secure_atomic_write
    if not hasattr(security, "_restore_windows_metadata"):
        pytest.skip("Windows-only code path")
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows-only code path")
    target = tmp_path / "f.bin"
    target.write_bytes(b"x")

    def boom(*args, **kwargs):
        raise PermissionError(errno.EACCES, "denied", "C:/very/secret/leak.txt")
    monkeypatch.setattr(security, "_restore_windows_metadata", boom)
    warnings = secure_atomic_write(target, lambda p: p.write_bytes(b"y"))
    assert warnings and all("leak.txt" not in w and "secret" not in w for w in warnings)
    assert "permission denied" in " ".join(warnings)
