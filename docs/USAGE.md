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
> 📖 Every file type is supported in some form — recognized formats get a dedicated handler (EXIF, ID3, PDF info, Office properties, …); anything else falls back to filesystem metadata (name, size, timestamps, permissions).

---

## 👁️ View Metadata

The **detail panel** (`DetailPanel`) opens on the **View** tab by default, showing a read-only, grouped table (`MetadataTable`) of every field MetaLens extracted — grouped and color-coded by source (EXIF, IPTC, XMP, ID3, Vorbis comments, PDF info, filesystem, …).

---

## ✏️ Edit Metadata

Switch to the **Edit** tab to open `MetadataEditor`:

- Editable fields show an input matching their type (text, number, date, or a dropdown for enumerated values)
- Change a value and save — MetaLens writes it back **atomically**: it copies the file to a `.ml_tmp` temp file, applies the change there, then atomically replaces the original (`os.replace`) so a crash or write error never corrupts your file
- If the file's handler doesn't support writing (e.g. RAW images, legacy Office `.doc`/`.xls`/`.ppt`, MKV/AVI video), the Edit tab shows a **read-only** notice instead of an editable form

---

## 🗑️ Delete Metadata Fields

Individual fields marked as **deletable** can be removed from the Edit tab. Deletion uses the same atomic write-back as editing — the original file is only touched once the operation succeeds.

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
- The diff view also includes its own hash comparison section, so you can confirm at a glance whether two files are byte-identical

---

## ↩️ Undo

MetaLens keeps an in-memory undo stack of your last **50** write/delete operations for the current session (`useUndoStack`). It is **not** persisted to disk — closing the app clears it. Trigger undo from the **Edit** menu or its keyboard shortcut.

---

## ℹ️ Checking Your Version

Open **Help → About** to see the running app version, read live from the Python sidecar's `/health` endpoint — useful when reporting a bug.

---

*Last updated: 2026-07-14*
*← [Installation](./INSTALLATION.md) | [API Reference →](./API.md)*
