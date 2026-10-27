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

`BaseMetadataHandler._atomic_write` delegates to `secure_atomic_write`.
The API also prepares mixed format/filesystem operations in one transaction.

1. Resolve the target and verify it is an accessible regular file.
2. Serialize in-process writers and create an exclusively allocated private temporary
   directory on the same filesystem as the target.
3. Copy the file, apply changes to the copy, preserve Unix ownership where required and
   flush its contents. Reject non-regular temporary files.
4. Check that the original file's identity and recorded stat attributes have not changed.
5. Replace using `os.replace` on Unix or `ReplaceFileW` on Windows. The Windows operation
   preserves the original security descriptor and alternate data streams.
6. Clean up only the operation's private workspace. Windows replacement uses a private
   backup for rollback; if rollback itself fails, retain recovery data and report its path.

Writer errors before replacement leave the original untouched. Concurrent-change checks
reduce accidental overwrites; they do not guarantee protection against a malicious process
with equivalent OS privileges or a hostile parent directory. Atomic visibility does not
provide an unconditional guarantee of power-loss durability.

## Format safeguards

- Updated parser dependencies address the tracked dependency advisories.
- JPEG exposes only supported EXIF fields as writable. Other image format metadata remains
  read-only; writable filesystem fields are separate.
- Encrypted PDFs are read-only to avoid accidentally removing encryption.
- Office edits rewrite core properties only, preserving the other package parts and macros.
  Digitally signed Office packages are read-only.
- Audio and video writes/deletions use the shared file transaction.
- The API derives capabilities from server-side handlers, validates keys and types, and
  keeps a bounded session cache of authorized deleted field templates for Undo restoration.

## Validation and limits

Regression tests cover rollback, colliding temporary names, metadata preservation, mixed
operations, authentication, NTFS streams/security descriptors, and restore after deletion.
Real Unix ownership and symlink behavior must also be tested on a supported Unix system.
Malformed-file resource exhaustion cannot be ruled out by unit tests; keep parser dependencies
current and retain backups of important files.

References: [FastAPI CORS](https://fastapi.tiangolo.com/tutorial/cors/),
[Python tempfile](https://docs.python.org/3/library/tempfile.html),
[Microsoft ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew),
[Electron security](https://www.electronjs.org/docs/latest/tutorial/security).
