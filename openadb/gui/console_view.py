"""Integrated console: remote shell on the selected device, or allow-listed ADB client commands."""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from openadb.core.console import (
    ConsoleCommandError,
    build_console_script,
    destructive_reasons,
    parse_adb_command,
    split_console_output,
)
from openadb.core.logbook import new_op_id
from openadb.core.process import CancelToken, ProcessResult
from openadb.gui.context import AppContext
from openadb.gui.i18n import tr
from openadb.gui.icons import icon

MODE_SHELL = "shell"
MODE_ADB = "adb"
HISTORY_LIMIT = 500


class ConsoleView(QWidget):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.serial: str | None = None
        self.cwd: dict[str, str] = {}
        self.history: list[str] = []
        self.hist_pos = 0
        self.cancel: CancelToken | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItem(icon("utilities-terminal"), tr("Remote Shell (runs on the device)"), MODE_SHELL)
        self.mode.addItem(icon("configure"), tr("ADB Commands (adb client)"), MODE_ADB)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        top.addWidget(self.mode)
        self.target = QLabel()
        self.target.setObjectName("hint")
        top.addWidget(self.target, 1)
        top.addWidget(QLabel(tr("Timeout:")))
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(1, 3600)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(ctx.settings.console_timeout)
        top.addWidget(self.timeout)
        lay.addLayout(top)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(20000)
        self.output.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.output.setUndoRedoEnabled(False)
        lay.addWidget(self.output, 1)
        row = QHBoxLayout()
        self.prompt = QLabel("$")
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        f.setWeight(QFont.Weight.Bold)
        self.prompt.setFont(f)
        row.addWidget(self.prompt)
        self.input = QLineEdit()
        self.input.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.input.returnPressed.connect(self.run)
        self.input.installEventFilter(self)
        row.addWidget(self.input, 1)
        self.run_btn = QPushButton(icon("media-playback-start"), tr("Run"))
        self.run_btn.clicked.connect(self.run)
        self.stop_btn = QPushButton(icon("process-stop"), tr("Cancel"))
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        copy_btn = QPushButton(icon("edit-copy"), tr("Copy output"))
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.output.toPlainText()))
        clear_btn = QPushButton(icon("edit-clear-history"), tr("Clear"))
        clear_btn.clicked.connect(self.output.clear)
        for w in (self.run_btn, self.stop_btn, copy_btn, clear_btn):
            row.addWidget(w)
        lay.addLayout(row)
        self._mode_changed()

    # ----------------------------------------------------------------- state
    def set_device(self, serial: str | None) -> None:
        self.serial = serial
        self._mode_changed()

    def _mode_changed(self) -> None:
        shell = self.mode.currentData() == MODE_SHELL
        if shell:
            self.input.setPlaceholderText(tr("Command to run on the device, e.g. ls -lah /userdata  ·  df -h  ·  uname -a"))
            where = self.cwd.get(self.serial or "", "")
            self.target.setText(tr("Target: {dev}").format(dev=self.serial or tr("no device")) + (f" · {where}" if where else ""))
            self.prompt.setText((self.cwd.get(self.serial or "", "") or "") + " $")
        else:
            self.input.setPlaceholderText(tr("ADB command without 'adb', e.g. devices -l  ·  version  ·  connect 192.168.1.50:5555"))
            self.target.setText(tr("Executable: {exe}").format(exe=self.ctx.adb.executable if self.ctx.adb else tr("not found")))
            self.prompt.setText("adb")

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self.input and event.type() == QEvent.Type.KeyPress:
            key = event.key()  # type: ignore[attr-defined]
            if key == Qt.Key.Key_Up and self.history:
                self.hist_pos = max(0, self.hist_pos - 1)
                self.input.setText(self.history[self.hist_pos])
                return True
            if key == Qt.Key.Key_Down and self.history:
                self.hist_pos = min(len(self.history), self.hist_pos + 1)
                self.input.setText(self.history[self.hist_pos] if self.hist_pos < len(self.history) else "")
                return True
        return super().eventFilter(obj, event)

    # ----------------------------------------------------------------- output
    def _write(self, text: str, color: str | None = None, bold: bool = False) -> None:
        cur = self.output.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        if color:
            fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        cur.insertText(text, fmt)
        self.output.setTextCursor(cur)
        self.output.ensureCursorVisible()

    # ----------------------------------------------------------------- run
    def run(self) -> None:
        text = self.input.text().strip()
        if not text or self.cancel is not None:
            return
        if self.ctx.adb is None:
            self._write(tr("ADB executable not available. Configure it in Settings.\n"), "#d9534f")
            return
        mode = self.mode.currentData()
        warnings: list[str] = []
        if mode == MODE_SHELL:
            if not self.serial:
                self._write(tr("No device selected.\n"), "#d9534f")
                return
            warnings = destructive_reasons(text)
        else:
            try:
                parsed = parse_adb_command(text)
            except ConsoleCommandError as exc:
                self._write(f"adb {text}\n", bold=True)
                self._write(str(exc) + "\n", "#d9534f")
                return
            warnings = parsed.warnings
            if parsed.device_scoped and not self.serial:
                self._write(tr("This command needs a selected device.\n"), "#d9534f")
                return
        if warnings and self.ctx.settings.confirm_console_destructive:
            box = QMessageBox(QMessageBox.Icon.Warning, tr("Potentially destructive command"),
                              tr("This command may change or destroy data:") + "\n\n• " + "\n• ".join(warnings) +
                              "\n\n" + text, parent=self)
            run = box.addButton(tr("Run anyway"), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is not run:
                self.ctx.logbook.info("Console command not confirmed", device=self.serial, op_type="shell",
                                      extra={"command": text, "confirmed": False})
                return
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            del self.history[:-HISTORY_LIMIT]
        self.hist_pos = len(self.history)
        self.input.clear()
        self._execute(mode, text)

    def _execute(self, mode: str, text: str) -> None:
        adb = self.ctx.adb
        assert adb is not None
        serial = self.serial
        timeout = float(self.timeout.value())
        cancel = CancelToken()
        self.cancel = cancel
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        op_id = new_op_id("sh" if mode == MODE_SHELL else "adb")
        op_type = "shell" if mode == MODE_SHELL else "adb-command"
        prefix = (self.cwd.get(serial or "", "") + " $ ") if mode == MODE_SHELL else "adb "
        self._write(f"{prefix}{text}\n", "#3d8ee6", bold=True)
        self.ctx.logbook.info(f"Console command started: {text}", device=serial if mode == MODE_SHELL else None,
                              op_id=op_id, op_type=op_type, extra={"timeout": timeout})
        cwd = self.cwd.get(serial or "")
        start = time.monotonic()

        def work() -> ProcessResult:
            if mode == MODE_SHELL:
                return adb.shell_raw(serial, build_console_script(text, cwd), timeout=timeout, cancel=cancel)  # type: ignore[arg-type]
            parsed = parse_adb_command(text)
            return adb.run(parsed.args, serial=serial if parsed.device_scoped else None, timeout=timeout, cancel=cancel)

        def done(res: ProcessResult) -> None:
            self.cancel = None
            self.run_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            out = res.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
            if mode == MODE_SHELL:
                out, new_cwd = split_console_output(out)
                if new_cwd and serial:
                    self.cwd[serial] = new_cwd
                    self._mode_changed()
            if out:
                self._write(out if out.endswith("\n") else out + "\n")
            err = res.stderr_text
            if err:
                self._write(err if err.endswith("\n") else err + "\n", "#d9534f")
            dur = time.monotonic() - start
            if res.timed_out:
                status, color = tr("timed out after {s:.1f}s").format(s=dur), "#d9534f"
            elif res.cancelled:
                status, color = tr("cancelled after {s:.1f}s").format(s=dur), "#e0a030"
            else:
                status = tr("exit {rc} · {s:.2f}s").format(rc=res.returncode, s=dur)
                color = "#5cb85c" if res.returncode == 0 else "#d9534f"
                caps = self.ctx.caps.get(serial or "")
                if mode == MODE_SHELL and caps and not caps.binary_safe:
                    status += " · " + tr("exit status may be unreliable on this device (no shell_v2)")
            self._write(f"[{status}]\n\n", color)
            level = "info" if res.ok else "warning"
            getattr(self.ctx.logbook, level)(
                f"Console command finished: {text}", device=serial if mode == MODE_SHELL else None, op_id=op_id,
                op_type=op_type, duration=dur,
                result="timeout" if res.timed_out else "cancelled" if res.cancelled else ("success" if res.ok else "failed"),
                error=(err.strip()[:500] or None) if not res.ok else None, extra={"exit_code": res.returncode})
            if mode == MODE_ADB and text.split()[0:1] and text.split()[0] in ("connect", "disconnect", "kill-server",
                                                                               "start-server", "reconnect", "usb", "tcpip"):
                self.ctx.refresh_devices()

        def fail(exc: BaseException) -> None:
            self.cancel = None
            self.run_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self._write(f"{exc}\n\n", "#d9534f")
            self.ctx.logbook.error(f"Console command failed: {text}", op_id=op_id, op_type=op_type, error=str(exc))

        self.ctx.tasks.submit(work, on_done=done, on_error=fail)

    def stop(self) -> None:
        if self.cancel is not None:
            self.cancel.cancel()
