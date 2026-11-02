# Sidecar security and file integrity

The Python sidecar is a local HTTP service with access to files owned by the current user.
Binding to loopback alone does not authenticate clients. Metadata and file contents must be
treated as untrusted input, including when the user opens them through the desktop UI.

## Access boundary

- Electron generates a random 256-bit session token and passes it through
  `METALENS_SIDECAR_TOKEN`. Python removes it from its environment after reading it.
- Every operation, including `/health`, requires an Authorization bearer token.
- Foreign browser origins are rejected. Allowed origins are the packaged file origin
  (`null`) and Vite at localhost or 127.0.0.1 on port 5173; these still require the token.
- Electron validates the main frame and its exact application URL before answering IPC.
- The renderer uses context isolation, the sandbox, a Content Security Policy and a narrow
  preload bridge. Browser navigation, popups and unrelated permissions are denied.

This is not a filesystem sandbox: an authenticated user may intentionally select any file
allowed by operating-system permissions. Normalization is not an authorization mechanism.
Do not dismiss path-related findings solely because the service listens on loopback.

## File updates

Every API mutation (`/write`, `/delete`, `/undo`) runs in ONE `secure_atomic_write`
transaction; the handler hooks (`snapshot`, `apply_write`/`apply_delete`, `restore`) work on
the temporary copy and never open a nested transaction. `BaseMetadataHandler.write`/`delete`
remain public wrappers that open their own transaction (`_atomic_write`).

1. Resolve the target and verify it is an accessible regular file; refuse it with
   `TargetNotWritableError` (HTTP 403) if it is not writable (permissions or the Windows
   READONLY attribute) or no temporary directory can be created beside it.
2. Serialize in-process writers and create an exclusively allocated private temporary
   directory (`.metalens-*`) on the same filesystem as the target.
3. Copy the file (`<stem>.ml_tmp<suffix>`). On Windows, clear HIDDEN/SYSTEM/READONLY on the
   copy while the callback runs (libraries recreate files by name, which Windows refuses for
   hidden files).
4. Validate and apply all changes to the copy, then read it back: written keys must hold the
   canonical value, deleted keys must be gone, no other field may change (else HTTP 422
   `lossy_write`, original untouched). Preserve Unix ownership where required, flush, reject
   non-regular temporary files.
5. Check that the original file's identity and recorded stat attributes have not changed
   (else HTTP 409).
6. Replace using `os.replace` on Unix or `ReplaceFileW` on Windows. The Windows operation
   preserves the original security descriptor and alternate data streams; the original file
   attributes are put back on the copy before the replace and re-applied afterwards if needed.
   The target is a new file: hard links to the old one are detached.
7. Clean up only the operation's private workspace. Windows replacement uses a private
   backup for rollback; if rollback itself fails, retain recovery data and report its path.

Writer errors before replacement leave the original untouched. Concurrent-change checks
reduce accidental overwrites; they do not guarantee protection against a malicious process
with equivalent OS privileges or a hostile parent directory. Atomic visibility does not
provide an unconditional guarantee of power-loss durability.

## Undo journal

`api/undo_journal.py` keeps, in sidecar memory only, the raw pre-images and post-images of
the keys each save changed (max 50 entries / 64 MiB, FIFO eviction), addressed by a random
opaque token. `/undo` accepts only the token: the path comes from the journal, never from
the client. Undo is refused (409) when a touched key no longer holds its post-image or the
file was replaced (device/inode check); unknown, used or evicted tokens get 410. The journal
may contain metadata values (including binaries) of recently edited files and is lost when
the sidecar stops.

## Format safeguards

- JPEG metadata is rewritten by `core.jpeg_segments`: only the touched APP1 Exif / APP1 XMP /
  APP13 Photoshop segment changes; every other segment, the scan data and trailers stay
  byte-identical. Duplicate Exif segments are removed when EXIF is rewritten, so deleted
  fields (e.g. GPS) do not survive in an older copy. The EXIF codec keeps every entry with its
  original type and bytes; the MakerNote is kept as opaque bytes at its original absolute offset
  (Canon-style pointers stay valid; vendor data stored outside the MakerNote itself is not tracked, hence
  a warning). Saving values equal to the current ones changes nothing: the file is not even replaced.
- Other image formats (TIFF, RAW, PNG, WebP, GIF, BMP) are read-only.
- Encrypted PDFs are read-only to avoid accidentally removing encryption; digitally signed
  PDFs (`/SigFlags`, a signed `/FT /Sig` field, `/Perms`) are read-only to avoid invalidating
  the signature. pypdf rewrites the whole PDF on save (content is preserved, the file is not
  byte-identical).
- Office edits rewrite `docProps/core.xml` only, preserving the other package parts and
  macros. Digitally signed Office packages are read-only.
- XMP packets with DOCTYPE or entity declarations are refused.
- Audio: AAC and formats outside an explicit list of mutagen classes are read-only.
- The API derives capabilities from server-side handlers and ignores client flags; values are
  validated and canonicalized before the file is touched; library value errors become 422.

## Validation and limits

Regression tests cover rollback, colliding temporary names, metadata preservation, mixed
operations, authentication, NTFS streams/security descriptors, hidden and read-only files on
Windows, exact Undo, and a generic contract run on every synthetic sample in
`tests/factories`. CI runs the Python suite on both `ubuntu-latest` and `windows-latest`.
Real Unix ownership and symlink behavior must also be tested on a supported Unix system.
Malformed-file resource exhaustion cannot be ruled out by unit tests; keep parser dependencies
current and retain backups of important files.

References: [FastAPI CORS](https://fastapi.tiangolo.com/tutorial/cors/),
[Python tempfile](https://docs.python.org/3/library/tempfile.html),
[Microsoft ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew),
[Electron security](https://www.electronjs.org/docs/latest/tutorial/security).
