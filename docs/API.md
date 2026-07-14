# 📡 API Reference — MetaLens

REST API exposed by the Python (FastAPI) sidecar that the Electron main process talks to over `127.0.0.1`.

> [!IMPORTANT]
> 🔒 The sidecar binds to `127.0.0.1` **only**, on a port chosen dynamically at launch (never `0.0.0.0`, never a fixed port). It is not reachable from other machines and is not intended to be called directly by end users — this reference is for developers extending MetaLens.

---

## 📚 Table of Contents

1. [GET /health](#get-health)
2. [GET /list](#get-list)
3. [GET /read](#get-read)
4. [POST /write](#post-write)
5. [POST /delete](#post-delete)
6. [GET /diff](#get-diff)
7. [GET /hash](#get-hash)
8. [📦 Data Models](#-data-models)

---

## GET `/health`

Health check + version, used by the About dialog and by Electron to know the sidecar is ready.

**Response**
```json
{ "status": "ok", "version": "0.2.1", "app": "MetaLens" }
```

---

## GET `/list`

Lists the contents of a directory with detected handler per file.

| Param | Type | Description |
|---|---|---|
| `path` | string (query) | Absolute directory path |

**Response**
```json
{
  "path": "/home/user/Pictures",
  "items": [
    { "name": "photo.jpg", "path": "/home/user/Pictures/photo.jpg", "is_dir": false, "size": 2048301, "mtime": 1752480000, "ext": ".jpg", "handler": "image" }
  ]
}
```

`handler` is `"—"` when no dedicated handler matches (filesystem-only metadata still applies).

---

## GET `/read`

Reads full metadata for a single file. Resolves the matching handler, then merges filesystem metadata as a fallback layer for any key not already populated by the handler.

| Param | Type | Description |
|---|---|---|
| `path` | string (query) | Absolute file path |

**Response** — a [`MetadataRecord`](#-data-models)

---

## POST `/write`

Atomically writes one or more metadata fields back to the file.

**Body**
```json
{
  "path": "/home/user/Pictures/photo.jpg",
  "fields": [
    { "key": "Artist", "value": "Jane Doe" }
  ]
}
```

**Response**
```json
{ "ok": true, "errors": [] }
```

Returns **422** if the file's handler reports `supports_write: false` (e.g. RAW images, legacy Office formats, MKV/AVI).

---

## POST `/delete`

Removes the specified metadata keys from the file.

**Body**
```json
{ "path": "/home/user/Pictures/photo.jpg", "keys": ["GPSLatitude", "GPSLongitude"] }
```

**Response**
```json
{ "ok": true, "errors": [] }
```

Returns **422** if the handler reports `supports_delete: false`.

---

## GET `/diff`

Compares metadata between two files.

| Param | Type | Description |
|---|---|---|
| `a` | string (query) | Absolute path to the first file |
| `b` | string (query) | Absolute path to the second file |

**Response** — a [`DiffResult`](#-data-models)

---

## GET `/hash`

Computes cryptographic hashes for a file on demand, streaming it in 1 MB chunks (no full read into memory).

| Param | Type | Description |
|---|---|---|
| `path` | string (query) | Absolute file path |
| `algorithms` | string (query, optional) | Comma-separated list. Default: `md5,sha1,sha256,sha512,blake2b` |

**Response**
```json
{
  "path": "/home/user/Pictures/photo.jpg",
  "size": 2048301,
  "hashes": { "md5": "...", "sha1": "...", "sha256": "...", "sha512": "...", "blake2b": "..." }
}
```

Returns **400** if an unsupported algorithm name is requested.

---

## ⚠️ Error Handling

All path-based endpoints validate input through `validate_file_path` / `validate_directory_path` before touching the filesystem (existence checks, traversal protection) and return **400** on violation.

---

## 📦 Data Models

Defined in `python/core/models.py`.

### `MetadataField`
| Field | Type | Notes |
|---|---|---|
| `key` | string | Raw field identifier |
| `label` | string | Human-readable label |
| `value` | any | Current value |
| `raw_value` | any | Unformatted underlying value |
| `editable` | bool | Whether `/write` accepts this field |
| `deletable` | bool | Whether `/delete` accepts this key |
| `field_type` | enum | `str \| int \| float \| datetime \| bytes \| enum` |
| `source` | enum | `exif \| iptc \| xmp \| id3 \| vorbis \| pdf \| filesystem \| ...` |
| `choices` | array | Present only for `enum` fields |

### `MetadataRecord`
| Field | Type |
|---|---|
| `file_path` | string |
| `file_size` | int |
| `mtime` | float |
| `handler_name` | string |
| `fields` | `MetadataField[]` |
| `read_errors` | string[] |
| `supports_write` | bool |
| `supports_delete` | bool |

### `DiffResult`
| Field | Type |
|---|---|
| `file_a` / `file_b` | string |
| `only_in_a` / `only_in_b` | `MetadataField[]` |
| `changed` | `[MetadataField, MetadataField][]` |
| `identical` | `MetadataField[]` |

---

*Last updated: 2026-07-14*
*← [Usage](./USAGE.md) | [Back to README →](../README.md)*
