# 📡 API Reference — MetaLens

REST API exposed by the Python (FastAPI) sidecar. The React renderer calls it directly over HTTP on `127.0.0.1`, with the port and session token it obtains from the Electron main process through IPC.

> [!IMPORTANT]
> 🔒 The sidecar binds to `127.0.0.1` **only**, on a port chosen dynamically at launch (never `0.0.0.0`, never a fixed port). It is not reachable from other machines and is not intended to be called directly by end users — this reference is for developers extending MetaLens.

---

## Authentication and origins

Every API request, including `/health`, requires `Authorization: Bearer <session-token>`.
Electron creates the token at startup and passes it to Python through `METALENS_SIDECAR_TOKEN`;
the sidecar refuses to start without it. The renderer receives `{port, token}` through the validated
IPC channel `get-sidecar-connection`. Do not put tokens in URLs, source files, logs or Vite environment variables.

Missing or invalid credentials return **401** (`unauthorized`); an imported application without a configured
token returns **503** (`auth_not_configured`). Foreign browser origins return **403** (`origin_not_allowed`).
Allowed origins are the packaged application's `null` origin and the Vite origins `http://localhost:5173`
and `http://127.0.0.1:5173`, with valid credentials still required. These responses use the same
[error body](#-errors) as the API. Use Vite together with Electron for development; the standalone
browser fallback is disabled.

---

## 📚 Table of Contents

1. [GET /health](#get-health)
2. [GET /aliases](#get-aliases)
2. [GET /list](#get-list)
3. [GET /read](#get-read)
4. [POST /write](#post-write)
5. [POST /delete](#post-delete)
6. [POST /sanitize](#post-sanitize)
7. [POST /undo](#post-undo)
7. [GET /diff](#get-diff)
8. [GET /hash](#get-hash)
9. [⚠️ Errors](#-errors)
10. [📦 Data Models](#-data-models)
11. [🔤 Value model](#-value-model)
12. [🗝️ Key namespaces](#-key-namespaces)

---

## GET `/health`

Health check + version, used by Electron to know the sidecar is ready (it checks `status` and `app`). The About dialog shows the packaged app version from Electron, not this value.

**Response**
```json
{ "status": "ok", "version": "0.4.0", "app": "MetaLens" }
```

---

## GET `/aliases`

Other names under which common fields are known (`"date taken"` for `exif:DateTimeOriginal`…), used by the search box.

**Response**
```json
{ "aliases": { "exif:DateTimeOriginal": ["date taken", "capture date", "shot date", "create date"] } }
```

---

## GET `/list`

Lists the contents of a directory with the detected format handler per file (directories first, then files, by name).

| Param | Type | Description |
|---|---|---|
| `path` | string (query) | Absolute directory path |

**Response**
```json
{
  "path": "/home/user/Pictures",
  "items": [
    { "name": "photo.jpg", "path": "/home/user/Pictures/photo.jpg", "is_dir": false, "size": 2048301, "mtime": 1752480000.0, "ext": ".jpg", "handler": "image" }
  ]
}
```

`handler` is one of `image`, `audio`, `video`, `pdf`, `office`, `ole` or `hachoir` (the fallback for any other file,
including files without extension). Directories have `"handler": "—"` and `"size": null`.
Entries that cannot be stat'ed are skipped. Errors: **400** invalid path, **403** directory not readable.

---

## GET `/read`

Reads the full metadata of one file: the format handler's record plus the filesystem layer (`fs:*`, `xattr:*`),
merged with unique keys.

| Param | Type | Description |
|---|---|---|
| `path` | string (query) | Absolute file path |

**Response** — a [`MetadataRecord`](#metadatarecord).

The server is the only authority on what can be modified: `editable` / `deletable` on each field,
`format_supports_write` / `format_supports_delete` and `read_only_reason` on the record, and `addable_fields`
(fields that are absent but can be added with `/write`). `read_errors` make the **format** metadata read-only;
`warnings` are informational and never block a write.

---

## POST `/write`

Writes one or more fields — and optionally deletes others — in **one** atomic save.

**Body**
```json
{
  "path": "/home/user/Pictures/photo.jpg",
  "fields": [
    { "key": "exif:Artist", "value": "Jane Doe" },
    { "key": "xmp:dc:subject", "value": ["holiday", "sea"] }
  ],
  "delete": ["gps:Latitude", "gps:LatitudeRef"]
}
```

`fields` and `delete` are both optional, but at least one must be non-empty. Only `key` and `value` of each
field are read: permission and type flags sent by the client are ignored.

**Response**
```json
{ "ok": true, "errors": [], "undo_token": "kq3V…", "undo_unavailable": null, "warnings": [] }
```

`warnings` lists non-blocking notices about a save that did succeed (for example the file owner or the Windows
security descriptor SACL could not be restored without a privilege). A save whose values all equal the current ones
is a no-op: nothing is rewritten or replaced (the file keeps its identity, hard links and times).

`undo_token` identifies the operation for [`POST /undo`](#post-undo); it is `null` when the save changed nothing
(e.g. writing the current values) or no exact Undo could be recorded. Both `/write` and `/delete` also return
`undo_unavailable`: `null` when there is a token or the save really changed nothing, otherwise
`{"code", "message"}` explaining why a change that **was** saved cannot be undone (`undo_too_large`: the pre/post
images exceed the 64 MiB journal budget, `undo_snapshot_failed`: the raw images could not be captured). The
save is not refused in that case; clients should tell the user. A save whose values equal the current ones does
not rewrite the file.

**Large integers.** Integers beyond 2^53 (an MP4 `plID`, an ASF QWORD) are exact in the JSON the server emits and are
accepted back either as numbers or as decimal strings (`"1152921504606846979"`). JavaScript clients must read them
without converting to `Number` (the MetaLens renderer does this and sends a decimal string).

How a save works:

1. Every key and value is validated against the server's own schema **before** the file is touched; all problems
   are reported together, one entry per key (**422**). A key must be a field of the record, an `addable_fields`
   entry or a valid dynamic key of the format (e.g. `tag:TXXX:<desc>`, `xattr:user.<name>`).
2. Values are converted to their canonical form (see [Value model](#-value-model)).
3. The changes are applied to a private copy of the file; the copy is read back and compared with the request:
   every written key must hold the canonical value, deleted keys must be gone and no other field may change.
   Otherwise the save fails with **422** `lossy_write` and the original is untouched.
4. The copy replaces the original (see [Atomic writes](./USAGE.md#-edit-metadata)).

Filesystem fields (`fs:mtime`, `xattr:user.*`) are applied after the format metadata in the same save, so a written
`fs:mtime` is the final modification time. They stay writable on files whose format metadata is read-only.

---

## POST `/delete`

Removes metadata fields. Equivalent to `/write` with only `delete`.

**Body**
```json
{ "path": "/home/user/Pictures/photo.jpg", "keys": ["gps:Latitude", "gps:Longitude"] }
```

**Response**
```json
{ "ok": true, "errors": [], "undo_token": "Zt8x…", "undo_unavailable": null, "warnings": [] }
```

Only fields present in the file with `deletable: true` can be removed; other keys return **422**
(`unknown_field` or `not_deletable`). Computed fields (stream info, image size, `fs:size`, the `fs:` timestamps) are never deletable.

---

## POST `/sanitize`

Prepares a file for sharing: removes every deletable field whose [sensitivity](#metadatafield) category is chosen,
in one atomic save with one Undo token.

**Body**
```json
{ "path": "/home/user/Pictures/photo.jpg", "categories": ["location", "device"], "preview": true, "keep": ["exif:Make"] }
```

`categories`: one or more of `location`, `identity`, `device`, `thumbnail`, `history`, `text`, `datetime`, `software` (unknown values: **422**).
`preview` (default `true`): only report, change nothing. `keep`: keys that stay in the file.

**Response**
```json
{ "ok": true, "preview": false,
  "will_remove": [{ "key": "gps:Latitude", "label": "Latitude", "category": "location" }],
  "removed": [{ "key": "gps:Latitude", "label": "Latitude", "category": "location" }],
  "remaining": [{ "key": "exif:Artist", "label": "Artist", "category": "identity", "reason": "..." }],
  "undo_token": "Zt8x…", "undo_unavailable": null, "warnings": [] }
```

`remaining` lists the sensitive fields of the chosen categories still in the file (not removable for this kind of file, or
still present after the save). `removed` is only present when `preview` is `false`.

---

## Operations on several files

`POST /sanitize-many` `{paths, categories, preview, keep}` · `POST /shift-dates` `{paths, seconds, keys?, preview}` ·
`POST /copy-tags` `{source, targets, keys?, preview}` · `POST /rename` `{path, template, preview}` ·
`GET /export?path=<folder>&format=json|csv&recursive=false` · `POST /geotag` `{paths, latitude, longitude, altitude?, gpx?, utc_offset_minutes, max_gap_seconds, preview}`
(fixed coordinates, or the position looked up along a GPX track by the capture time of each picture).

The first three answer `{ok, preview, results: [{path, ok, ..., undo_token}], undo_tokens}`: every file is its own
verified save with its own Undo token, a failing file does not stop the others (they are not atomic across files).
A failed file is `{path, ok: false, status, errors: [{code, key, message}]}` (no `detail`; the UI joins the messages).
At most 500 files per request (2000 for the export). `preview` defaults to `true` and changes nothing.
The rename template accepts `{name}`, `{ext}` and `{key}` / `{key|strftime}` for date-time keys, never replaces an existing
file (409) and cannot be undone. CSV cells that start with a formula character are prefixed with `'`.

---

## POST `/undo`

Reverts one earlier `/write` or `/delete` exactly, using the raw pre-images kept by the sidecar.

**Body**
```json
{ "token": "kq3V…" }
```

Only `token` is accepted (the file path comes from the journal, never from the client).

Besides the field pre-images the journal keeps the original bytes of the region of the file the save changed
(the part between the identical head and tail). When the file is still exactly as the save left it, the Undo puts
those bytes back (files up to 1 GiB), so the file is **byte-identical** to the one before the save (also for what mutagen cannot
remove, e.g. the FLAC padding or the MP4 `meta` atom added with a first tag). If another change touched that
region, or the changed region exceeds 16 MiB, the Undo restores the field values instead (verified the same way).

**Response**
```json
{ "ok": true, "errors": [], "keys": ["exif:Artist", "gps:Latitude"], "undo_token": null }
```

- Every key touched by the operation must still hold the value the operation left; if any was changed since
  (or the file was replaced, moved or is now read by another handler), the Undo is refused with **409**
  `concurrent_modification` and the entry is kept.
- The restore runs in one verified atomic transaction: binaries (cover art, pictures, MakerNote), multi-valued
  fields and text stored in legacy encodings come back byte for byte.
- An unknown, already used or evicted token returns **410** `undo_expired`.
- The journal lives in sidecar memory only: at most **50** entries and **64 MiB** of snapshot data, oldest evicted
  first. It is lost when the sidecar stops (closing the app); an operation larger than the budget simply has no Undo.
- The file's modification time is **not** restored implicitly: after an Undo it is the time of the Undo, unless
  the undone operation itself wrote `fs:mtime`.

---

## GET `/diff`

Compares the metadata of two files. Each side is the same merged record `/read` returns, filesystem layer included.

| Param | Type | Description |
|---|---|---|
| `a` | string (query) | Absolute path to the first file |
| `b` | string (query) | Absolute path to the second file |

**Response** — a [`DiffResult`](#diffresult). Fields are matched by key; two values are equal only when they have
the same JSON type and value (binary descriptors are compared by size, SHA-256 and MIME, so two different images of
the same size are reported as changed). The filesystem times `fs:atime` (changed by reading) and `fs:ctime` (creation or
metadata-change time, different for any copy) are not compared, so a file diffed with itself reports no changes.

---

## GET `/hash`

Computes cryptographic hashes of a file on demand, streaming it in 1 MiB chunks (no full read into memory).

| Param | Type | Description |
|---|---|---|
| `path` | string (query) | Absolute file path |
| `algorithms` | string (query, optional) | Comma-separated list of `md5`, `sha1`, `sha256`, `sha512`, `blake2b`. Default: all five |

**Response**
```json
{
  "path": "/home/user/Pictures/photo.jpg",
  "size": 2048301,
  "hashes": { "md5": "...", "sha1": "...", "sha256": "...", "sha512": "...", "blake2b": "..." }
}
```

Returns **400** (`invalid_parameter`, key `algorithms`) if no algorithm is requested (e.g. `algorithms=`) or an
unsupported name is given, **403** if the file cannot be read.

---

## ⚠️ Errors

Every error response, including authentication failures, has the same body; `detail` is always a string summary:

```json
{
  "ok": false,
  "errors": [
    { "code": "missing_offset", "key": "fs:mtime", "message": "fs:mtime: ..." },
    { "code": "read_only", "key": "img:width", "message": "img:width: field is read-only" }
  ],
  "detail": "fs:mtime: ...; img:width: field is read-only"
}
```

`key` is `null` for errors not tied to one field.

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_path`, `invalid_parameter` | Path not absolute / not an existing readable file or directory; bad query parameter (`/hash`) |
| 401 | `unauthorized` | Missing or wrong bearer token |
| 403 | `origin_not_allowed` | Foreign browser origin |
| 403 | `not_writable` | The target file cannot be written (read-only attribute, permissions, no right to create the temporary copy next to it) |
| 403 | `access_denied` | Directory or file not readable (`/list`, `/hash`) |
| 404 / 405 | `not_found` / `method_not_allowed` | Unknown route or method |
| 409 | `concurrent_modification` | The file changed during the save, or (Undo) since the operation |
| 410 | `undo_expired` | Undo token unknown, already used or evicted |
| 422 | field codes below, `invalid_request`, `duplicate_key` | Validation failed; nothing was written |
| 500 | `internal_error` | Unexpected failure (a retained recovery copy, if any, is named in the message) |
| 503 | `auth_not_configured` | Sidecar started without a session token |
| 503 | `file_busy` | The file stays locked by another program after a short retry (response has `Retry-After`); retry later. A vanished file is 404 `file_not_found` |

Field-level codes (422): `invalid_value`, `wrong_type`, `null_value`, `empty_value`, `out_of_range`,
`invalid_choice`, `wrong_length`, `missing_offset`, `unexpected_offset`, `too_large`, `unsupported`,
`read_only`, `not_deletable`, `unknown_field`, `lossy_write`. Request-schema errors (pydantic) use
`invalid_request` with the field location as `key`. A value-related library error is always reported as 422,
never 500.

Path validation (`core.path_security`) resolves `~`, `..` and symbolic links and checks that the target exists and
is readable. It is normalization, **not** an authorization mechanism: an authenticated caller may name any file the
operating-system account can access (see [python/SECURITY.md](../python/SECURITY.md)).

---

## 📦 Data Models

Defined in `python/core/models.py`.

### `MetadataField`
| Field | Type | Notes |
|---|---|---|
| `key` | string | Stable identifier `<namespace>:<native id>` (see [Key namespaces](#-key-namespaces)); unique in a record |
| `label` | string | Human-readable label |
| `value` | any JSON | Canonical value (see [Value model](#-value-model)) |
| `raw_value` | any JSON | Underlying value as read, for display; bytes are shown as `"<binary N bytes>"` |
| `editable` | bool | Whether `/write` accepts this key |
| `deletable` | bool | Whether `/delete` (or `/write` `delete`) accepts this key |
| `field_type` | string | `str`, `text`, `int`, `float`, `bool`, `datetime`, `localdatetime`, `rational`, `duration`, `binary`, `enum` |
| `multi` | bool | `value` is a list of items of `field_type` |
| `length` | int \| null | Fixed list size for tuples (e.g. track `[3, 10]`, GPS coordinate of 3 rationals) |
| `choices` | array | Allowed values for `enum` fields; always present, empty otherwise |
| `binary` | object \| array \| null | For `binary` fields: the `{size, sha256, mime}` descriptor (a list when `multi`) |
| `sensitivity` | string \| null | What the field can reveal when the file is shared: `location`, `identity`, `device`, `thumbnail`, `history`, `text`, `datetime`, `software`; `null` for technical fields. Informative only: it never changes `editable`/`deletable` |
| `source` | string | Group shown in the UI: `exif`, `gps`, `xmp`, `iptc`, `image`, `png`, `gif`, `id3`, `vorbis`, `flac`, `asf`, `ape`, `mp4`, `qt`, `mkv`, `heif`, `jfif`, `jpeg`, `mpf`, `icc`, `audio`, `video`, `pdf`, `office`, `odf`, `epub`, `ole`, `hachoir`, `filesystem`, `xattr` |

### `MetadataRecord`
| Field | Type | Notes |
|---|---|---|
| `file_path` | string | Resolved path |
| `file_size` | int | |
| `mtime` | float | POSIX seconds |
| `handler_name` | string | Format handler (`image`, `audio`, `video`, `pdf`, `office`, `ole`, `hachoir`) |
| `fields` | `MetadataField[]` | Format fields followed by the filesystem layer |
| `read_errors` | string[] | Problems reading the format metadata; when non-empty the format metadata is read-only |
| `warnings` | string[] | Non-blocking notices (filesystem layer, EXIF MakerNote, duplicated EXIF/XMP segments, renamed duplicate keys, ...) |
| `format_supports_write` | bool | The format metadata of this file can be written |
| `format_supports_delete` | bool | The format metadata of this file can be deleted |
| `read_only_reason` | string \| null | Why the format metadata is read-only (e.g. signed PDF, AAC, legacy OLE, unknown format) |
| `supports_write` | bool | Legacy: format **or** filesystem level writable |
| `supports_delete` | bool | Legacy: format **or** filesystem level deletable |
| `addable_fields` | `MetadataField[]` | Schemas (value `null`) of fields that can be added with `/write` |
| `sensitivity_summary` | object | Number of fields per sensitivity category, most serious first (categories without fields are omitted) |

If a handler ever produces two fields with the same key, the later ones are renamed `key#2`, `key#3`…, made
read-only and reported in `warnings`.

### `DiffResult`
| Field | Type |
|---|---|
| `file_a` / `file_b` | string |
| `only_in_a` / `only_in_b` | `MetadataField[]` |
| `changed` | `{ "field_a": MetadataField, "field_b": MetadataField }[]` |
| `identical` | `MetadataField[]` (the field of file A) |

All lists are sorted by key.

---

## 🔤 Value model

`value` is always JSON-native and canonical (`python/core/values.py`). `/write` accepts the forms below and
answers with the canonical form on the next read.

| `field_type` | Canonical value | Accepted on write |
|---|---|---|
| `str` / `text` | string (`text` = multi-line) | string only (numbers are not coerced); an empty string only where the format can store it (otherwise `empty_value`: delete the field instead) |
| `int` | integer | integer, integral number or strict decimal string (`"121"`), within the range of the stored type |
| `float` | finite number | number or numeric string |
| `bool` | `true` / `false` | boolean, `0`/`1`, or (case-insensitive) `"true"/"false"`, `"1"/"0"`, `"yes"/"no"`, `"on"/"off"` — so `"False"` is false; anything else is refused |
| `datetime` | ISO 8601 **with** UTC offset, e.g. `"2026-01-02T03:04:05+02:00"` | same; a value without offset is refused (`missing_offset`) |
| `localdatetime` | ISO 8601 **without** offset (EXIF wall-clock time) | same; a value with offset is refused (`unexpected_offset`) |
| `rational` | exact `"n/d"` string, never reduced (`"1234/100"` stays) | `"n/d"`, `[n, d]`, an integer or a decimal (`"2.5"` → `"25/10"`) |
| `duration` | seconds (non-negative number) | read-only |
| `binary` | `{size, sha256, mime}` descriptor | never editable; may be deletable |
| `enum` | one of `choices` | one of `choices` |

- `multi` fields take a JSON **array** (never a joined string); with `length` the array must have exactly that size.
- `null` is never a valid value (`null_value`).
- Text that is not valid in the format's declared encoding is shown decoded as Latin-1 without loss; the exact
  original bytes are what Undo restores.

---

## 🗝️ Key namespaces

Keys are `<namespace>:<native id>` and never depend on the value, the label or the field order. They are not
persisted anywhere (the Undo journal is in memory), so they may change between versions; changes are listed in
`CHANGELOG.md`.

| Namespace | Handler / formats | Native id | Writable |
|---|---|---|---|
| `img:` | image (all) | `format`, `mode`, `width`, `height`, `dpi` (`[x, y]`, from JFIF/EXIF/Pillow); PNG also `bit_depth`, `color_type`, `interlace` | no (computed) |
| `exif:` | JPEG, PNG `eXIf`, WebP `EXIF` (editable); TIFF, TIFF-based RAW (entries can be changed, added or deleted; nothing existing is moved) (e.g. CR2, NEF, ARW, DNG, ORF, RW2), PNG `eXIf`, WebP `EXIF` | tag name (`exif:Artist`, `exif:DateTimeOriginal`); a tag outside its usual IFD is `exif:<IFD0\|Exif\|Interop\|IFD1>:<Name>`; unnamed tags `exif:tag_<id>` | JPEG only; image-structure tags, pointers, the thumbnail and `UNDEFINED` binaries (e.g. `MakerNote`) are read-only, binaries deletable |
| `gps:` | as `exif:` (GPS IFD) | tag name without the `GPS` prefix (`gps:Latitude`, `gps:LatitudeRef`) | JPEG only |
| `xmp:` | JPEG (APP1 XMP), WebP `XMP `, PNG (`XML:com.adobe.xmp` iTXt), TIFF/DNG (tag 700), MP4/MOV (XMP `uuid`) | `xmp:<prefix>:<local>` with canonical prefixes (`xmp:dc:title`, `xmp:dc:subject`, `xmp:xmp:Rating`, `xmp:photoshop:City`, `xmp:Iptc4xmpCore:Location`…) | JPEG, PNG and WebP, for simple values, language alternatives (`x-default`; the other languages are listed read only as `<key>@<language>`, e.g. `xmp:dc:title@de`) and Bag/Seq of simple items; structs and qualifiers are not shown individually: `xmp:@hidden` lists them (computed, removable as a whole); extended XMP is read-only |
| `iptc:` | JPEG (APP13 `Photoshop 3.0`, resource 0x0404), TIFF/DNG (tag 33723, read-only) | mapped IIM name (`iptc:Keywords`, `iptc:By-line`, `iptc:Caption-Abstract`…) or `iptc:<record>:<dataset>` | JPEG only; repeatable datasets are lists; written as UTF-8 (`1:90 = ESC % G`); a legacy (non-UTF-8) charset makes IPTC read-only; an edit or delete of a dataset is mirrored to its XMP twin (and vice versa) when the twin is already in the file: Keywords↔`dc:subject`, By-line↔`dc:creator`, ObjectName↔`dc:title`, Caption-Abstract↔`dc:description`, CopyrightNotice↔`dc:rights`, Headline, City, Province-State, Country, Credit, Source, SpecialInstructions, TransmissionReference, Sub-location, Country code ↔ the matching `photoshop:`/`Iptc4xmpCore:` property. The mirror is part of the same transaction and Undo, never creates a twin, never overrides a key sent in the same request, and a value the twin cannot store is skipped with a `warnings` note |
| `heif:` | HEIC / HEIF / AVIF (`.heic .heif .hif .avif`; EXIF, XMP and ICC are also served as `exif:`, `gps:`, `xmp:`, `icc:`) | `MajorBrand`, `CompatibleBrands`, `Rotation` | no (read-only; the EXIF / GPS / XMP fields of these files can be deleted in place) |
| `jfif:` | JPEG (APP0 JFIF) | `Version`, `ResolutionUnit`, `XResolution`, `YResolution` | no |
| `jpeg:` | JPEG | `Comment` (COM segments; a list when there are several) | no |
| `maker:` | JPEG/TIFF/RAW EXIF with a Canon, Nikon or Sony camera note | `maker:<vendor>:<Name>` (`maker:canon:OwnerName`, `maker:nikon:SerialNumber`…) | no (computed; removing `exif:MakerNote` removes them) |
| `mpf:` | JPEG with a Multi-Picture index (MPO) | `NumberOfImages`, `image<N>:Type`, `image<N>:Size` (computed) | no |
| `icc:` | JPEG (APP2, split profiles are joined), PNG `iCCP`, TIFF/DNG (tag 34675), WebP `ICCP` | `Version`, `ProfileClass`, `ColorSpace`, `ConnectionSpace`, `CreationDate`, `Platform`, `RenderingIntent`, `Description`, `Copyright` | no |
| `png:` | PNG `tEXt`/`zTXt`/`iTXt` | keyword; an `iTXt` with a language tag is `keyword@language` | no |
| `pngchunk:` | PNG chunks that are not text | `pngchunk:tIME` (last modification, UTC) | no |
| `gif:` | GIF | `comment` | no |
| `tag:` | ID3 (MP3, WAV, AIFF, TTA), Vorbis comments (FLAC, Ogg Vorbis/Opus/Speex/FLAC), ASF (WMA), APEv2 (APE, WavPack, Musepack) | ID3 FrameID (`tag:TPE1`, `tag:TXXX:<desc>`, `tag:COMM:<desc>:<lang>`, URL frames grouped by FrameID); lowercase Vorbis name (`tag:artist`); ASF attribute name; APE item key | yes, except AAC and other read-only formats; pictures, private and structured frames (APIC, GEOB, PRIV, POPM, PCNT, SYLT…) are read-only but deletable |
| `flac:` | FLAC | `picture` (picture blocks) | read-only, deletable |
| `qt:` | MP4 family: M4A/M4B/M4R, MP4/M4V/MOV | `qt:MajorBrand`, `qt:CompatibleBrands`, `qt:CreateDate`/`qt:ModifyDate` (movie header, UTC), `qt:TimeScale`, per track by track ID `qt:track<ID>:HandlerType/CreateDate/Language/MediaTimeScale/Width/Height/Rotation/CodecID/Channels/SampleRate/FrameRate`, QuickTime keys `qt:mdta:<key>` (e.g. `com.apple.quicktime.location.ISO6709`), `qt:udta:<atom>` | dates (`CreateDate`, `ModifyDate`, per track), `udta` text atoms and text `mdta` keys can be changed, added and deleted; the other values are read-only |
| `mp4:` | MP4 family: M4A/M4B/M4R (audio), MP4/M4V/MOV (video) | `ilst` atom (`mp4:©ART`, `mp4:trkn`, `mp4:covr`, freeform `mp4:----:<mean>:<name>`) | yes; `covr` and non-UTF-8 freeform values read-only but deletable |
| `audio:` | audio files, first audio track of MP4 | `duration`, `bitrate`, `sample_rate`, `channels`, `bits_per_sample`, `codec` | no (computed) |
| `video:` | MP4 family | `duration` | no (computed) |
| `pdf:` | PDF (`pdf:@xmp` = the XMP metadata stream, computed, removable as a whole) | `/Info` entry without the slash (`pdf:Title`, `pdf:CreationDate`); computed `pdf:page_count`, `pdf:encrypted`, `pdf:signed`, `pdf:PDFVersion`, `pdf:Linearized` (an `/Info` entry with one of those names is `pdf:<name>#info`) | text and date entries, unless the PDF is encrypted or signed; `/Trapped`, numbers and booleans read-only but deletable |
| `office:` | DOCX/XLSX/PPTX/DOCM/XLSM/PPTM | core property (`office:title`, `office:author`, `office:created`…); `office:app.<Name>` (docProps/app.xml), `office:custom.<name>` (docProps/custom.xml) | core properties editable unless the package is signed; `app.`/`custom.` values are read-only but can be deleted |
| `odf:` | OpenDocument (`.odt .ods .odp .odg` and templates) | `title`, `subject`, `description`, `initial-creator` (author), `creator` (last modified by), `keyword`, dates, `editing-cycles`, `editing-duration`, `stat.<name>`, `custom.<name>` | no (read-only) |
| `epub:` | EPUB | `version`, `title`, `creator`, `language`, `identifier`, `publisher`, `date`, `subject`, `description`, `rights`, `modified`… | no (read-only) |
| `ole:` | DOC/XLS/PPT/DOT/XLT/POT | SummaryInformation and DocumentSummaryInformation property (`ole:author`, `ole:company`…), user-defined `ole:custom:<name>` | read-only, but deletable (the property is emptied in place; the file keeps its size) |
| `mkv:` | Matroska / WebM (`.mkv .webm .mka`) | `mkv:DocType`, `mkv:Info:Title/MuxingApp/WritingApp/DateUTC/Duration`, `mkv:tag:<name>`, `mkv:attachment:<file name>`, `mkv:track<N>:Type/CodecID/Language/Name` | titles, apps and tag texts can be rewritten when they fit; those and the date, tags and attachments can be deleted (in place) |
| `hachoir:` | fallback for other files; AVI/WMV/FLV/TS/MTS/M2TS (video handler) | `hachoir:<group index>:<item key>` (group 0 = root metadata, then one group per stream) | no |
| `fs:` | every file | `size`, `mtime`, `ctime` (creation time on Windows, metadata change on POSIX), `atime`, `win_attrs` (Windows), `permissions`/`owner`/`group` (POSIX) | `fs:mtime` only |
| `xattr:` | every file (Linux, macOS) | attribute name (`xattr:user.comment`) | Linux `user.` attributes with UTF-8 values (non-UTF-8 values binary, read-only, deletable); other namespaces and macOS read-only |

---

*Last updated: 2026-11-04*
*← [Usage](./USAGE.md) | [Back to README →](../README.md)*
