# Changelog

## 1.0.0 — 2026-10-28

First public release.

### Features
- Dual-pane file manager (local ↔ device) for ADB devices, including non-Android Linux devices such
  as Knulli handhelds; no Android framework services required.
- Device discovery over USB and TCP/IP, connect/disconnect, offline/unauthorized/no-permission states,
  device names and per-device profiles; non-destructive capability probe and classification.
- Remote file management: browse, create (nested) folders, rename, move, copy, delete (including
  confirmed recursive deletion), properties; protected system and Knulli locations.
- Filename-safe listings (spaces, quotes, Unicode, newlines) without parsing `ls -l`.
- Transfer queue with upload/download of files and folders, drag and drop, pause/resume, cancel,
  retry, reorder, overwrite policies, free-space checks and partial-file detection.
- Real progress reporting (parsed from adb or measured), with honest indeterminate states.
- Verification by size, on-device SHA-256/MD5, or download-and-hash.
- Knulli profile: folder shortcuts and a ROM/BIOS upload wizard with editable system mappings.
- Activity & Logs panel with filters, TXT/JSONL export and local diagnostic bundle.
- Integrated console: remote shell and allow-listed adb commands.
- Light/dark themes, settings dialog, AppImage build script and Arch PKGBUILD.

### Known limitations
- Only one device with a given ADB serial can be connected at a time (many Knulli handhelds share
  the serial `0123456789`).
- No byte-level resume of interrupted transfers.
