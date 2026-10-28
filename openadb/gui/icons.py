"""Icon lookup: freedesktop theme icons (Breeze on KDE) with Qt standard-icon fallbacks."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle

SP = QStyle.StandardPixmap

_FALLBACK = {
    "go-previous": SP.SP_ArrowBack,
    "go-next": SP.SP_ArrowForward,
    "go-up": SP.SP_ArrowUp,
    "go-home": SP.SP_DirHomeIcon,
    "view-refresh": SP.SP_BrowserReload,
    "folder-new": SP.SP_FileDialogNewFolder,
    "edit-delete": SP.SP_TrashIcon,
    "edit-rename": SP.SP_FileDialogDetailedView,
    "document-properties": SP.SP_FileDialogInfoView,
    "folder": SP.SP_DirIcon,
    "inode-directory": SP.SP_DirIcon,
    "text-x-generic": SP.SP_FileIcon,
    "emblem-symbolic-link": SP.SP_FileLinkIcon,
    "folder-remote": SP.SP_DriveNetIcon,
    "drive-removable-media": SP.SP_DriveHDIcon,
    "network-connect": SP.SP_DriveNetIcon,
    "network-disconnect": SP.SP_DialogCloseButton,
    "configure": SP.SP_FileDialogDetailedView,
    "go-next-view": SP.SP_ArrowRight,
    "go-previous-view": SP.SP_ArrowLeft,
    "process-stop": SP.SP_BrowserStop,
    "media-playback-pause": SP.SP_MediaPause,
    "media-playback-start": SP.SP_MediaPlay,
    "edit-clear-history": SP.SP_DialogResetButton,
    "dialog-warning": SP.SP_MessageBoxWarning,
    "dialog-error": SP.SP_MessageBoxCritical,
    "dialog-information": SP.SP_MessageBoxInformation,
    "edit-copy": SP.SP_FileDialogContentsView,
    "edit-paste": SP.SP_DialogApplyButton,
    "edit-cut": SP.SP_DialogDiscardButton,
    "document-save": SP.SP_DialogSaveButton,
    "utilities-terminal": SP.SP_ComputerIcon,
    "applications-games": SP.SP_DriveCDIcon,
    "arrow-up": SP.SP_ArrowUp,
    "arrow-down": SP.SP_ArrowDown,
    "view-hidden": SP.SP_FileDialogListView,
}


def icon(name: str) -> QIcon:
    ic = QIcon.fromTheme(name)
    if not ic.isNull():
        return ic
    sp = _FALLBACK.get(name)
    if sp is not None and QApplication.instance() is not None:
        return QApplication.style().standardIcon(sp)
    return QIcon()


def app_icon() -> QIcon:
    path = Path(__file__).resolve().parent.parent / "resources" / "openadb.svg"
    return QIcon(str(path)) if path.exists() else icon("folder-remote")
