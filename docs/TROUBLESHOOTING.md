# Troubleshooting

## The device does not appear

1. Run `adb devices -l` in a terminal. OpenADB shows exactly what this command reports.
2. Use a data-capable USB-C cable and, on the RG35XX SP, the USB port intended for OTG/data.
3. Make sure ADB is enabled on the device.
4. Restart the ADB server: `adb kill-server && adb start-server` (or the *ADB Commands* console).
5. Check *Settings ▸ ADB & devices*: executable path (default `/usr/bin/adb` from PATH), server
   host/port (`127.0.0.1:5037`). The **Test** button runs `adb version`.

## `no permissions (user in plugdev group; are your udev rules wrong?)`

Your user may not access the USB device. On Arch Linux:

```bash
sudo pacman -S android-udev     # community udev rules for ADB devices
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then unplug/replug. If the device's USB vendor ID is not covered, add a rule such as
`/etc/udev/rules.d/51-android-local.rules`:

```
SUBSYSTEM=="usb", ATTR{idVendor}=="18d1", MODE="0660", TAG+="uaccess"
```

(find the vendor id with `lsusb`). Running OpenADB as root is not required and not recommended.

## `unauthorized`

Android devices show an RSA key prompt that must be accepted. Knulli normally does not require
this; if it appears, check the device's ADB settings.

## `offline`

Reconnect the cable or run *Device ▸ Rescan*. Transfers that were running fail with
"device disconnected" and can be retried from the Transfers tab; a possibly partial destination
file is reported.

## `getprop: not found`

Expected on Knulli and other non-Android systems. OpenADB logs it as a warning and switches to
*Generic Linux ADB* mode; nothing else is affected.

## No progress percentage is shown

ADB only prints progress when its output is a terminal. OpenADB runs it on a pseudo-terminal on
Linux; when ADB still prints nothing, OpenADB measures the destination file size once per second.
If neither works the bar is labelled *indeterminate*. The batch progress is always based on
completed bytes.

## Verification says "size (hash unavailable)"

The device has neither `sha256sum` nor `md5sum`. Choose *Compare hashes (download & hash…)* in
Settings ▸ Transfers to verify by downloading the file again (slower).

## A delete was refused

Protected locations (`/`, all top-level folders such as `/userdata`, `/bin`, `/etc`, `/usr`, and
`/userdata/roms`, `/userdata/bios`, `/userdata/saves`, `/userdata/system`) can never be deleted,
renamed or moved from the GUI. Delete their contents instead. Additional protected paths can be
configured in Settings ▸ Safety.

## The AppImage does not start

- `fuse: failed to exec fusermount`: run with `--appimage-extract-and-run`, or install `fuse2`.
- Qt platform plugin errors on Wayland: try `QT_QPA_PLATFORM=xcb ./OpenADB*.AppImage`.
- Start from a terminal with `--debug` to see log output.

## Where are logs and settings?

- Logs: `~/.local/share/openadb/logs/` (JSON Lines, rotated)
- Settings: `~/.config/openadb/settings.json`
- Transfer queue: `~/.local/share/openadb/transfer_queue.json`

*Activity & Logs ▸ Diagnostic bundle…* packs these (with redaction) into a local ZIP you can
attach to a bug report yourself. Nothing is uploaded automatically.
