# Path handling and atomic updates

Use `core.path_security` for path normalization and all file-content updates.
See [the security model](../SECURITY.md) for the HTTP authentication boundary and limitations.

## Validation

`validate_file_path(path_str, must_exist=True)` expands the user home, resolves the path
and symbolic links, then checks regular-file existence and read access.
`validate_directory_path` performs equivalent directory checks.
Both return an absolute `Path` or raise `PathSecurityError`.

Relative paths are resolved against the current working directory. Normalization is not
an allowlist: this desktop application intentionally permits access to files allowed by
the user's OS permissions. Authenticate API callers before accepting paths.

```python
from core.path_security import validate_file_path, validate_directory_path

file_path = validate_file_path("~/Documents/report.pdf")
directory = validate_directory_path("~/Documents")
```

## Writing

Handlers should call `self._atomic_write(path, callback)`; this delegates to the shared
`secure_atomic_write(target_path, callback, temp_suffix=".ml_tmp")`.

The callback receives a temporary copy with the original extension, inside a private
directory beside the target. It must modify only that copy and propagate failures.
Never construct predictable temporary names or write directly to the original.

```python
from core.path_security import secure_atomic_write

def update_copy(temporary_path):
    # Use a format-aware library to update only the temporary file.
    update_metadata(temporary_path)

secure_atomic_write(file_path, update_copy)
```

Updates are serialized within the process. Before replacement, the implementation checks
that the temporary file is regular and that the target's identity, size and timestamps
still match the initial snapshot. It flushes the temporary file and preserves ownership
on Unix, or rejects the change if ownership cannot be retained.

Replacement uses `os.replace` on Unix and `ReplaceFileW` on Windows. Windows replacement
preserves alternate data streams and the security descriptor; a private backup supports
rollback on partial failure. If rollback fails, the operation retains its recovery files
and reports their location rather than deleting them.

## API integration

The API validates field permissions against server-side handler metadata. Format and
filesystem changes are applied to one temporary copy before replacement. Duplicate,
unknown, unsupported or invalid fields are rejected. Deleted dynamic field templates
are cached with a bound for authorized session-local Undo operations.

- Invalid paths: HTTP 400.
- Invalid fields or values: HTTP 422.
- Concurrent-change validation failure: HTTP 409.
- Permission errors from the writer: HTTP 403.
- Other writer/replacement errors: HTTP 500; report and inspect any retained recovery path.

## Tests and limitations

Run `pytest python/tests/` from the repository root. The integrity regression suite
checks collision safety, rollback, concurrent modification, mixed writes, metadata
preservation, NTFS streams and DACLs. Unix-specific checks require Unix and symlink tests
may be skipped where the account lacks symlink privileges.

Checks reduce accidental data loss but do not guarantee safety against an attacker who
controls the parent directory or has equivalent filesystem privileges. Atomic replacement
does not guarantee all power-loss durability scenarios. Keep important file backups.
