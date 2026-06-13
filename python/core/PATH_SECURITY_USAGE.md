# Path Security Module: Quick Reference

## Module: `core.path_security`

Provides safe, CodeQL-compliant path validation and atomic write operations for MetaLens.

### Imports

```python
from core.path_security import (
    validate_file_path,
    validate_directory_path,
    secure_atomic_write,
    normalize_path,
    PathSecurityError,
)
```

---

## Function Reference

### `validate_file_path(path_str: str, must_exist: bool = True) -> Path`

Validate and normalize an absolute file path.

**Parameters**:
- `path_str` (str): Path string (absolute or relative)
- `must_exist` (bool): If True, file must exist and be readable (default: True)

**Returns**:
- `Path`: Resolved, validated Path object

**Raises**:
- `PathSecurityError`: If path is invalid, unreadable, or doesn't exist (when required)

**Examples**:
```python
# Validate existing file
fpath = validate_file_path("/home/user/document.pdf")
print(fpath.stat().st_size)  # File definitely exists and is readable

# Validate path that may not exist
fpath = validate_file_path("~/new_file.txt", must_exist=False)
# fpath is absolute even though file doesn't exist yet
```

**Accepts**:
- Absolute paths: `/home/user/file.txt`, `C:\Users\file.txt`
- Relative paths: `./file.txt`, `../file.txt` (resolved to absolute)
- Tilde expansion: `~/file.txt` (expands to home directory)
- Paths with `..`: `./dir/../file.txt` (normalized)
- Symlinks: `~/symlink_to_file.txt` (resolved to target)

---

### `validate_directory_path(path_str: str, must_exist: bool = True) -> Path`

Validate and normalize an absolute directory path.

Same as `validate_file_path()` but for directories.

**Examples**:
```python
# Validate existing directory
dirpath = validate_directory_path("/home/user/Documents")
for item in dirpath.iterdir():
    print(item.name)

# Validate directory that may not exist yet
dirpath = validate_directory_path("~/new_project", must_exist=False)
```

---

### `secure_atomic_write(target_path: Path, write_fn: callable, temp_suffix: str = ".ml_tmp") -> None`

Perform atomic write with symlink-attack protection.

**Parameters**:
- `target_path` (Path): Path object of the file to write (must exist and be writable)
- `write_fn` (callable): Function that takes (Path) and performs write operations
- `temp_suffix` (str): Suffix for temporary file (default: ".ml_tmp")

**Raises**:
- `PathSecurityError`: If target_path is not valid or not writable
- Any exception from `write_fn` or `os.replace()`

**Examples**:
```python
# Simple text write
def writer(tmp_path):
    with tmp_path.open("w") as f:
        f.write("new content")

target = validate_file_path("/home/user/file.txt")
secure_atomic_write(target, writer)

# Binary write (e.g., modifying EXIF)
def binary_writer(tmp_path):
    with tmp_path.open("rb") as f:
        data = f.read()
    # ... modify data ...
    with tmp_path.open("wb") as f:
        f.write(modified_data)

target = validate_file_path("/home/user/photo.jpg")
secure_atomic_write(target, binary_writer)

# Using with handler
def write_metadata(tmp_path):
    handler = HandlerRegistry.get(tmp_path)
    handler._modify_file(tmp_path)

target = validate_file_path(fpath)
secure_atomic_write(target, write_metadata)
```

**What It Does**:
1. Validates target path (exists, writable)
2. Creates temporary file in same directory
3. Copies original to temporary
4. Calls `write_fn(temporary)` to perform modifications
5. On Unix: verifies temporary file is not a symlink (before and after write)
6. Atomically replaces original with temporary
7. On error: cleans up temporary file

---

### `normalize_path(path_str: str) -> str`

Normalize a path string without validation.

Useful for logging/display. Does NOT validate existence or permissions.

**Examples**:
```python
# For error messages
try:
    fpath = validate_file_path(user_input)
except PathSecurityError:
    print(f"Invalid path: {normalize_path(user_input)}")
```

---

### `PathSecurityError`

Exception raised when path validation fails.

```python
try:
    fpath = validate_file_path(path)
except PathSecurityError as e:
    # e.args[0] contains error message
    logger.error(f"Path validation failed: {e}")
    raise HTTPException(status_code=400, detail=str(e))
```

---

## API Integration Pattern

### Read Operation

```python
from fastapi import APIRouter, HTTPException, Query
from core.path_security import validate_file_path, PathSecurityError

@router.get("/read")
def read_metadata(path: str = Query(...)):
    try:
        fpath = validate_file_path(path, must_exist=True)
    except PathSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    # Use fpath safely
    handler = HandlerRegistry.get(fpath)
    return handler.read(fpath).to_dict()
```

### Write Operation

```python
from core.path_security import validate_file_path, secure_atomic_write, PathSecurityError

@router.post("/write")
def write_metadata(req: WriteRequest):
    try:
        fpath = validate_file_path(req.path, must_exist=True)
    except PathSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    handler = HandlerRegistry.get(fpath)
    
    def write_fn(tmp_path):
        # Modify metadata on tmp_path
        handler.write(tmp_path, req.fields)
    
    try:
        secure_atomic_write(fpath, write_fn)
    except PathSecurityError as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    return {"ok": True}
```

### List Directory

```python
@router.get("/list")
def list_directory(path: str = Query(...)):
    try:
        dirpath = validate_directory_path(path, must_exist=True)
    except PathSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    items = []
    for entry in dirpath.iterdir():
        items.append({
            "name": entry.name,
            "path": str(entry),
            "is_dir": entry.is_dir(),
        })
    return {"path": str(dirpath), "items": items}
```

---

## Error Handling

### Validation Errors

```python
try:
    fpath = validate_file_path(path_from_user)
except PathSecurityError as e:
    # Log for debugging
    logger.error(f"Invalid path '{path_from_user}': {e}")
    
    # Return user-friendly error
    return {
        "ok": False,
        "error": "Invalid file path",
        "details": str(e)
    }
```

### Write Errors

```python
try:
    secure_atomic_write(fpath, writer)
except PathSecurityError as e:
    # Symlink attack detected or permission issue
    logger.error(f"Atomic write failed: {e}")
    raise HTTPException(status_code=403, detail="Cannot write file")
except Exception as e:
    # Other errors (permissions, disk full, etc.)
    logger.error(f"Write failed: {e}")
    raise HTTPException(status_code=500, detail="Write operation failed")
```

---

## Security Guarantees

### What This Module Guarantees

✓ Path is absolute (no relative escapes like `../../etc/passwd`)
✓ Path is normalized (all symlinks resolved before use)
✓ File/directory exists and is accessible (permission checked)
✓ Write operations are atomic (all-or-nothing, not partial writes)
✓ Symlink attacks prevented during write (no TOCTOU window)
✓ Temporary files cleaned up on error

### What This Module DOES NOT Guarantee

- File contents are not malicious
- User has legitimate access (only OS permissions checked, not app-specific ACLs)
- Other processes won't interfere (only our own atomic operations protected)
- Disk space won't run out during write

---

## Performance Notes

- `.resolve()` is fast (no I/O in most cases, just path normalization)
- `os.access()` requires one stat() call per path
- `secure_atomic_write()` copies entire file to temp (uses `shutil.copy2()`)
- For large files (>100MB), consider streaming copy with chunking

---

## Testing

Run tests to verify path security:

```bash
# All path security tests
pytest python/tests/test_path_security.py -v

# Specific test
pytest python/tests/test_path_security.py::TestSecureAtomicWrite -v

# With coverage
pytest python/tests/test_path_security.py --cov=core.path_security
```

---

## Troubleshooting

### "Invalid path" Error

**Cause**: Path is not absolute or doesn't resolve
**Solution**: Ensure path is absolute or relative to cwd, use normalize_path() for debugging

### "File not found" Error

**Cause**: File doesn't exist when must_exist=True
**Solution**: Check file exists, or use must_exist=False if you're creating the file

### "No read permission" Error

**Cause**: File exists but user lacks read permission
**Solution**: Check file permissions, run with proper user account

### "No write permission" Error (on atomic_write)

**Cause**: File exists but user lacks write permission
**Solution**: Check file and directory permissions, may need to run with elevated privilege

### Symlink Error (on Unix)

**Cause**: Temporary file became a symlink during write
**Solution**: This indicates a potential symlink attack. Check system logs, move to safer directory, or run with restricted umask

---

## Examples

### Example 1: Read Metadata Handler

```python
from core.path_security import validate_file_path

def read_image_metadata(path_str: str):
    try:
        fpath = validate_file_path(path_str, must_exist=True)
    except PathSecurityError as e:
        return {"ok": False, "error": str(e)}
    
    if fpath.suffix.lower() not in {'.jpg', '.png'}:
        return {"ok": False, "error": "Not an image"}
    
    from PIL import Image
    img = Image.open(fpath)
    return {
        "ok": True,
        "size": img.size,
        "format": img.format,
    }
```

### Example 2: Batch Validation

```python
def validate_file_list(path_list: list[str]) -> tuple[list[Path], list[str]]:
    """Validate multiple paths, return valid paths and errors."""
    valid = []
    errors = []
    
    for path_str in path_list:
        try:
            fpath = validate_file_path(path_str, must_exist=True)
            valid.append(fpath)
        except PathSecurityError as e:
            errors.append(f"{path_str}: {e}")
    
    return valid, errors
```

### Example 3: Safe Metadata Modification

```python
from core.path_security import validate_file_path, secure_atomic_write

def update_metadata(file_path: str, new_tags: dict) -> bool:
    try:
        fpath = validate_file_path(file_path, must_exist=True)
    except PathSecurityError:
        return False
    
    def writer(tmp_path):
        # Load original
        from PIL import Image
        img = Image.open(tmp_path)
        
        # Update metadata
        exif = img.info.get('exif', b'')
        # ... modify exif ...
        
        # Save
        img.save(tmp_path, exif=exif)
    
    try:
        secure_atomic_write(fpath, writer)
        return True
    except Exception:
        return False
```

---

## See Also

- `python/SECURITY.md` — Detailed security analysis
- `python/api/routes.py` — API integration examples
- `python/tests/test_path_security.py` — Full test suite
