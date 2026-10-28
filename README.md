# OpenADB File Manager

A dual-pane desktop file manager (Python + PySide6/Qt 6) for devices reachable through
**Android Debug Bridge**, including devices that run an ADB daemon but are **not Android** —
for example the Anbernic RG35XX SP running **Knulli**.

The application never needs Android framework services (`getprop`, `pm`, `am`, …). A device
with a working `adb shell` and the ADB sync protocol (`push`/`pull`) is fully usable. The
ADB-reported model (Knulli reports `model:Nexus_4`) is shown for information only and is never
used to identify the hardware.

## Features

- **Device discovery** via `adb devices -l`: USB and TCP/IP devices, several at once,
  automatic refresh, `offline` / `unauthorized` / `no permissions` states, `adb connect HOST:PORT`,
  custom device names and saved per-device profiles.
- **Capability probe** (non-destructive): OS, architecture, available tools (`stat`, `find`,
  `sha256sum`, …), binary-safe shell detection, Knulli/Batocera detection. Devices are classified
  as *Android-compatible*, *Generic Linux ADB*, *Minimal ADB* or *Unknown* — classification
  never blocks file management.
- **Dual-pane browser**: local (left) and device (right) with breadcrumbs/editable path bar,
  back/forward/up, filter, hidden-file toggle, sortable columns (name, size, type, date,
  permissions, owner), free-space display.
- **Remote file management**: create (nested) folders, rename, move (cut/paste or drag onto a
  folder), copy, delete files, empty folders and — after explicit confirmation — folders
  recursively; properties dialog with permissions, ownership, size, dates and `du` size.
- **Safe deletion**: confirmation listing every path and what will be removed, a mandatory
  "recursive" checkbox for non-empty folders, clear *permanent* labelling (no fake recycle bin),
  protected locations (`/`, every top-level folder such as `/userdata`, `/bin`, `/etc`, `/usr`,
  plus `/userdata/roms`, `/userdata/bios`, `/userdata/saves`, `/userdata/system` and user-defined
  ones), symlinks never followed, per-item results and an honest partial-failure report.
- **Robust filenames**: listings never parse `ls -l`; names are transferred NUL-separated and
  matched against machine-readable `stat -c` output. Spaces, quotes, brackets, Unicode, tabs and
  even newlines work. On shells that are not binary-safe a hex-encoded transport is used; if that
  is impossible the affected names are flagged and refused rather than risking the wrong file.
- **Transfers** with `adb -s SERIAL push/pull`: files and whole directories (expanded per file),
  drag and drop both ways, persistent queue (pending/preparing/transferring/verifying/completed/
  failed/cancelled/interrupted), pause/resume at job boundaries, cancel, retry, reorder,
  configurable concurrency, overwrite policies (ask/skip/replace/rename/backup), free-space check,
  partial-file detection after cancellation, moves that delete the source only after a verified copy.
- **Real progress only**: current-file and batch progress, bytes, speed, elapsed, ETA. Progress is
  parsed from ADB's own output (run on a pseudo-terminal so ADB prints it) or measured from the
  destination file size; otherwise it is shown as *indeterminate*. Nothing is invented.
- **Verification**: size comparison, SHA-256/MD5 on device, or download-and-hash; results are shown
  per file. High-integrity mode for ROM uploads.
- **Activity & Logs**: structured events (timestamp with timezone, severity, device, operation id,
  type, paths, duration, result, error), live viewer with severity/text/device/operation filters,
  copy, export TXT/JSONL, local diagnostic bundle (ZIP), rotation and retention, credential redaction.
- **Console**: *Remote Shell* (runs on the device, working directory remembered) and *ADB Commands*
  (allow-listed `adb` subcommands only). Asynchronous, with stdout/stderr, exit status, duration,
  timeout, cancel, history, and a warning before destructive commands. No local shell is exposed.
- **Knulli profile**: shortcuts to `/userdata`, `roms`, `bios`, `saves`, `system`, `system/logs`
  (only enabled when they exist), ROM and BIOS upload wizard with editable system → folder
  mappings, free-space and conflict checks, verification and summary. EmulationStation game lists
  are only reloaded when you explicitly ask for it.
- Light/dark/system themes, translation-ready UI, settings dialog.

## Requirements

- Python 3.11+ and PySide6 (Qt 6)
- The `adb` client. Arch Linux: `sudo pacman -S android-tools`
- No root privileges are needed.

## Install and run (development)

```bash
git clone https://github.com/SpikeTheDragon40k/adb_helper_knulli.git
cd adb_helper_knulli
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m openadb
```

Other options:

- **Per-user install** (venv + menu entry, no root): `./scripts/install-user.sh`
- **Arch package**: `cd packaging/arch && makepkg -si` (uses the system `pyside6` and `android-tools`)
- **AppImage**: `./packaging/appimage/build-appimage.sh` → `dist/OpenADB_File_Manager-0.1.0-x86_64.AppImage`

## Building the AppImage

```bash
./packaging/appimage/build-appimage.sh
chmod +x dist/OpenADB_File_Manager-*-x86_64.AppImage
./dist/OpenADB_File_Manager-*-x86_64.AppImage
```

The script downloads a relocatable CPython AppImage (niess/python-appimage) and appimagetool,
installs the app plus `PySide6-Essentials` into it and packs the result. It needs `curl`,
`python3` and network access. `adb` is **not** bundled: the system `adb` is used (or the path set
in Settings). If FUSE is not available, run the AppImage with `--appimage-extract-and-run`.

## Connecting the RG35XX SP (Knulli)

1. On the handheld, make sure ADB is enabled (Knulli enables `adbd` over USB on the OTG/USB-C
   port; check *System settings* if needed).
2. Connect it with a **data-capable** USB-C cable to the PC.
3. Run `adb devices -l`. You should see something like
   `0123456789  device usb:3-2 product:occam model:Nexus_4 device:mako`.
   If it says `no permissions`, see [Troubleshooting](docs/TROUBLESHOOTING.md).
4. Start OpenADB. The device appears in the *Device* selector as *Knulli handheld (0123456789)
   — USB · Generic Linux ADB*; the right pane opens `/userdata`.
5. Over Wi-Fi: if the device exposes ADB over TCP, use *Device ▸ Connect…* and enter `IP:5555`.

## Uploading ROMs and BIOS files

- **Wizard**: select ROM files in the left pane (optional), click **Upload ROMs…**. The system is
  suggested from the file extension (e.g. `.gba` → *Game Boy Advance* → `/userdata/roms/gba`);
  the dialog checks that the folder exists, free space and name conflicts. Choose what to do with
  existing files (default: skip) and keep *High-integrity mode* on to hash-verify every file.
  A summary appears when the upload finishes, with an optional button to ask EmulationStation to
  reload its game lists.
- **BIOS**: **Upload BIOS…** works the same way with `/userdata/bios` as destination.
- **Manual**: navigate the right pane to any folder and drag files/folders from the left pane or
  from Dolphin, or select and press **Ctrl+U**.
- System → folder mappings, the ROM root and BIOS folder are editable in *Settings ▸ ROM systems*
  and *Settings ▸ Device profiles*.

## Inspecting transfer logs

- **Activity & Logs** tab (Ctrl+2): filter by severity, text, device and operation
  (`upload`, `download`, `delete`, `rename`, `probe`, `shell`, …). Each transfer and deletion has an
  operation id shared by all its events.
- **Transfers** tab ▸ *Details* shows per-file status, verification method and result, errors.
- Export the current view as TXT/JSONL or save a diagnostic ZIP; nothing is uploaded anywhere.
- Log files: `~/.local/share/openadb/logs/activity*.jsonl` (rotated; retention configurable).
  Settings: `~/.config/openadb/settings.json`. Transfer queue: `~/.local/share/openadb/transfer_queue.json`.

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| Enter / double-click | open folder |
| Backspace, Alt+Up | parent folder |
| Alt+Left / Alt+Right | back / forward |
| F5 | refresh |
| Ctrl+L | edit path |
| Ctrl+F | filter |
| F2 | rename |
| Delete | delete (device: permanent with confirmation; local: move to trash) |
| Shift+Delete | delete permanently (local) |
| Ctrl+Shift+N | new folder |
| Ctrl+C / Ctrl+X / Ctrl+V | copy / cut / paste (within or across panes) |
| Ctrl+Shift+C | copy path |
| Ctrl+U / Ctrl+D | upload / download selection |
| Alt+Enter | properties |
| Ctrl+1 / 2 / 3 | Transfers / Logs / Console |

## Tests

```bash
QT_QPA_PLATFORM=offscreen python -m pytest
```

The suite uses a **mock `adb`** (`tests/mock_adb/adb`) that simulates devices as directories,
including the RG35XX SP fixture (serial `0123456789`, ADB model `Nexus_4`, `uname` → Linux aarch64,
no `getprop`, data under `/userdata`), PTY-style shells without `shell_v2`, minimal devices without
`stat`, slow/failing/corrupting transfers and disconnects. No physical device is required.

Opt-in, non-destructive checks against a real device:

```bash
OPENADB_REAL_DEVICE=0123456789 python -m pytest tests/integration -v -s
# also push/pull a temporary file under /tmp on the device:
OPENADB_REAL_DEVICE=0123456789 OPENADB_REAL_DEVICE_WRITE=1 python -m pytest tests/integration -v -s
```

## Architecture

```
openadb/
  core/                 Qt-independent backend
    process.py          subprocess runner: argv only, timeouts, cancellation, PTY streaming
    adb.py              ADB client wrapper, remote scripts with exit-status recovery, push/pull
    devices.py          `adb devices -l` parsing and diffing
    capabilities.py     non-destructive capability probe and classification
    remote_fs.py        NUL-safe listing, stat, mkdir/rename/copy/delete, df, hashes
    operations.py       logged user-level remote operations (delete, rename, mkdir, paste)
    local_fs.py         local walk, hashing, copy/move/delete helpers
    transfer.py         transfer jobs, engine and persistent queue
    progress.py         ADB progress parser, rate estimator, snapshots
    verify.py           size / hash / download-and-hash verification
    logbook.py          structured logging, rotation, retention, export, redaction
    console.py          console command validation and destructive-command detection
    config.py           settings and device profiles (JSON)
    knulli.py           Knulli shortcuts and ROM system mappings
    paths.py            app directories, quoting, path normalisation and protection rules
    diagnostics.py      local diagnostic ZIP
  gui/                  PySide6 user interface (main window, panes, panels, dialogs)
```

## Known limitations

- **Only one Knulli handheld can be connected at a time.** Many Knulli devices (in particular the
  Allwinner-based Anbernic handhelds such as the RG35XX family) report the same factory ADB serial,
  `0123456789`. ADB selects a device by its serial (`adb -s SERIAL …`), so when two devices with the
  same serial are connected simultaneously, commands and transfers cannot be routed to the right
  one. Connect one handheld at a time (USB or network). Devices with distinct serials can still be
  connected together.
- Byte-level resume is not implemented: a retried transfer starts its files from the beginning.
  Cancelled transfers may leave a partial destination file; it is reported and can be removed from
  the Transfers tab.
- Progress depends on what ADB prints. On Windows (no PTY) progress is measured from the
  destination size; very fast transfers may show no intermediate progress.
- Copying directly between two devices is not supported (download, then upload).
- Dragging device files to other applications (e.g. Dolphin) is not supported; drop them on the
  left pane instead.
- On device shells that are neither binary-safe nor have `od`, names containing newlines are listed
  but refused for operations.
- No translations are shipped yet (the UI is translation-ready).
- Windows support is architected for (no POSIX-only calls in the core besides the optional PTY) but
  has not been tested.

## AI assistance disclosure

This project was developed with the assistance of Claude Opus 5.5 (Anthropic), which helped write
the code, the test suite and the documentation, and supported testing during development.
All changes were reviewed, tested on real hardware and published by the maintainer.

## License

MIT
