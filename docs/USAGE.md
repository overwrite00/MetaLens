# 📖 Usage Guide — MetaLens

Learn how to browse files, inspect and edit metadata, compute hashes, and compare two files.

> [!TIP]
> 💡 **First time?** Install the app first — see the [Installation Guide](./INSTALLATION.md).

---

## 📚 Table of Contents

1. [🌐 Open the App](#-open-the-app)
2. [📁 Browse Folders](#-browse-folders)
3. [📄 Select a File](#-select-a-file)
4. [👁️ View Metadata](#-view-metadata)
5. [✏️ Edit Metadata](#-edit-metadata)
6. [🗑️ Delete Metadata Fields](#-delete-metadata-fields)
7. [🔑 Compute File Hashes](#-compute-file-hashes)
8. [⚖️ Compare Two Files (Diff)](#-compare-two-files-diff)
9. [↩️ Undo](#-undo)

---

## 🌐 Open the App

Launch MetaLens from the Start Menu (Windows) or your application launcher (Linux). The window opens at 1280×800 with three panels: a folder tree on the left, a file list in the center, and an empty detail panel on the right.

---

## 📁 Browse Folders

The **left panel** (`FolderPanel`) lists your available drives and home directory. Click any entry to expand it and navigate into subfolders — MetaLens reads directly from the local filesystem, nothing is uploaded or synced anywhere.

---

## 📄 Select a File

The **center panel** (`FilePanel`) shows every file in the currently selected folder, with an icon colored by extension. Click a file to load its metadata into the detail panel on the right.

> [!NOTE]
> 📖 Every file type is supported in some form — recognized formats get a dedicated handler (EXIF/XMP/IPTC, ID3, PDF info, Office properties, …); any other file is examined by the **hachoir** fallback, which recognizes many formats from their content (also files without extension). Every file also shows the filesystem layer (size, timestamps, Windows attributes or POSIX permissions/owner, extended attributes).

---

## 👁️ View Metadata

The **detail panel** (`DetailPanel`) opens on the **Metadata** tab by default, showing a read-only table (`MetadataTable`) of every field MetaLens extracted, grouped by source (`exif`, `gps`, `xmp`, `iptc`, `id3`, `vorbis`, `mp4`, `pdf`, `office`, `hachoir`, `filesystem`, …).

- Multi-valued fields (artists, keywords, …) are shown as lists, never joined into one string.
- A line above the table tells what sharing the file would reveal (location, identity, device, dates...), and each sensitive field carries a badge with its category.
- Binary values (cover art, pictures, MakerNote, thumbnails) are shown as size, MIME type and SHA-256, not as their bytes.
- Notices above the table explain why the format metadata is read-only (e.g. signed PDF, AAC, legacy Office), report read errors and show warnings (e.g. an EXIF MakerNote that is moved on rewrite, duplicated metadata segments).

---

## ✏️ Edit Metadata

Switch to the **Edit** tab to open `MetadataEditor`:

- Editable fields show an input matching their type: text, multi-line text, number, checkbox (booleans), dropdown (enumerated values), exact `n/d` rationals, a list editor for multi-valued fields, fixed-size inputs for pairs such as track `[number, total]`, and date-time with a separate UTC offset (EXIF dates are local times without offset)
- **Add field** offers the fields the format can store but the file does not have yet (e.g. an ID3 title on an untagged WAV, an XMP keyword list on a JPEG)
- **Save** sends all edits, additions and deletions as **one** save. The sidecar validates every value first (invalid values are reported next to their field and nothing is written), applies the changes to a private copy next to the file, reads the copy back to check that exactly the requested changes were stored, and only then replaces the original (`ReplaceFileW` on Windows, keeping the security descriptor, alternate data streams and attributes such as *hidden*; `os.replace` elsewhere). A failure at any step leaves the original untouched. The file is replaced by a new one, so hard links to it are detached
- When the format metadata is read-only (e.g. RAW, TIFF/PNG/WebP/GIF, AAC, MKV/AVI, legacy `.doc`/`.xls`/`.ppt`, encrypted or digitally signed PDFs, signed Office packages), the Edit tab shows a **read-only** notice with the reason; filesystem fields such as the modification time (`fs:mtime`) stay editable

---

## 🗑️ Delete Metadata Fields

Individual fields marked as **deletable** can be removed from the Edit tab, including binary ones such as cover art (which cannot be edited). Deletions are part of the same verified atomic save as edits. Computed values (image size, stream info, file size, timestamps) cannot be deleted.

---

## 🔑 Compute File Hashes

From the **Metadata** area of the detail panel, compute cryptographic hashes **on demand** (not automatically, to avoid slowing down browsing):

| Algorithm | Notes |
|---|---|
| MD5 | Legacy, widely used for checksums |
| SHA-1 | Legacy, used by Git and many tools |
| SHA-256 | Modern standard, recommended |
| SHA-512 | High-security workloads |
| BLAKE2b | Fast modern algorithm, built into Python |

Each computed hash has a one-click **copy to clipboard** button.

---

## ⚖️ Compare Two Files (Diff)

Switch to the **Diff** tab (`DiffView`) to compare two files side by side:

- Fields only present in file A, only in file B, changed between the two, and identical fields are each shown in their own section
- Both sides include the filesystem layer; binary values are compared by their SHA-256, so two different pictures of the same size are reported as changed
- **CSV** exports the comparison
- The diff view also includes its own hash comparison section, so you can confirm at a glance whether two files are byte-identical

---

## ↩️ Undo

MetaLens keeps an undo stack of your last **50** saves for the current session. Trigger it with **Edit → Undo** or **Ctrl+Z** (Cmd+Z on macOS); while the cursor is in a text field, the shortcut undoes your typing instead.

- Undo is **exact**: the sidecar restores the original bytes of every field the save touched, binaries and multi-valued fields included, in one verified atomic save.
- If the file was changed since the save (by another program or a later edit of the same fields), the Undo is refused and the entry is removed from the stack; the same happens when the sidecar no longer has the entry (more than 50 entries or 64 MiB of saved data). Other errors keep the entry so you can retry.
- The file's modification time is **not** put back: after an Undo it is the time of the Undo, unless the undone save itself changed the modification time. A later save moving the modification time never blocks an earlier Undo; only a change made by another program does.
- Nothing is persisted to disk: closing the app clears the stack and the sidecar's journal.

---

## ℹ️ Checking Your Version

Open **Help → About** to see the running app version (the packaged Electron app version, including any `-beta.N` suffix) — useful when reporting a bug. The dialog also links to the project repository on GitHub.

---

## 🔔 Updates

MetaLens never checks for updates on its own unless you enable it.

- **Check now** — **Help → Check for Updates…**, or **About → Check for updates**. This contacts the public GitHub Releases API (`api.github.com`) and compares the result with your version.
- **Check at startup** — tick **Check for updates automatically at startup** in the About dialog (off by default). A failed startup check is silent; if a new version is found, an **Update vX.Y.Z** button appears in the toolbar.
- **Which releases count** — a stable build is only offered newer **stable** releases. A beta build (`X.Y.Z-beta.N`) is offered newer betas as well as the stable release of its version or later, and the dialog says whether the new version is a beta or a stable.
- **What you see** — the release notes (as plain text) and, depending on the platform, buttons:
  - **Windows (installed build)** — **Download & install** downloads the installer to a temporary folder, verifies its SHA-256 checksum against the one published by GitHub, then starts it and closes MetaLens. A download that does not match is discarded. Use **Cancel** to stop a download.
  - **Linux** — only **Release page** is offered; download the package for your distribution from there (see the [Installation Guide](./INSTALLATION.md)).
- **Ignore this version** — hides the toolbar notice for that version only (a manual check still shows it, marked *ignored*, with **Stop ignoring**). A newer version is announced again.

The preferences (auto-check on/off, ignored version) are stored in `settings.json` inside the app's user-data folder.

---

*Last updated: 2026-11-03*
*← [Installation](./INSTALLATION.md) | [API Reference →](./API.md)*
