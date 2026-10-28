"""Transfer queue panel with per-file and batch progress."""

from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QRect, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QProgressBar,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QStyleOptionViewItem,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from openadb.core.progress import ProgressSnapshot, ProgressSource, format_bytes, format_duration
from openadb.core.transfer import ACTIVE_STATES, Direction, JobStatus, TransferJob
from openadb.gui.context import AppContext
from openadb.gui.dialogs import JobDetailsDialog, ask_yes_no, show_error
from openadb.gui.i18n import tr
from openadb.gui.icons import icon

COLS = ("Status", "Transfer", "Device", "Progress", "Files", "Size", "Speed", "ETA", "Destination")
JOB_ROLE = Qt.ItemDataRole.UserRole + 10
PCT_ROLE = Qt.ItemDataRole.UserRole + 11


class JobsModel(QAbstractTableModel):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.progress: dict[str, ProgressSnapshot] = {}
        self.jobs: list[TransferJob] = list(ctx.queue.jobs)

    def reload(self) -> None:
        self.beginResetModel()
        self.jobs = list(self.ctx.queue.jobs)
        self.endResetModel()

    def job_updated(self, job: TransferJob) -> None:
        if [j.id for j in self.jobs] != [j.id for j in self.ctx.queue.jobs]:
            self.reload()
            return
        row = self.jobs.index(job)
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLS) - 1))

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.jobs)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return tr(COLS[section])
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self.jobs):
            return None
        job = self.jobs[index.row()]
        snap = self.progress.get(job.id)
        col = index.column()
        if role == JOB_ROLE:
            return job
        if role == PCT_ROLE:
            if job.status == JobStatus.COMPLETED:
                return 100.0
            if snap and job.status in ACTIVE_STATES:
                return snap.batch_percent
            if job.bytes_total:
                return 100.0 * job.bytes_done / job.bytes_total
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return tr(job.status.value)
            if col == 1:
                return job.title
            if col == 2:
                return self.ctx.display_name(job.serial)
            if col == 3:
                return ""
            if col == 4:
                done = job.files_done + job.files_failed + job.files_skipped
                return f"{done}/{job.files_total}" if job.files_total or job.status not in (JobStatus.PENDING,) else "—"
            if col == 5:
                done = snap.batch_bytes_done if snap and job.status in ACTIVE_STATES else job.bytes_done
                return f"{format_bytes(done)} / {format_bytes(job.bytes_total)}" if job.bytes_total else "—"
            if col == 6:
                return f"{format_bytes(snap.speed)}/s" if snap and snap.speed and job.status in ACTIVE_STATES else ""
            if col == 7:
                return format_duration(snap.eta) if snap and snap.eta is not None and job.status in ACTIVE_STATES else ""
            if col == 8:
                return job.dest_dir
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = f"{job.title}\n{job.direction.value}: {', '.join(job.sources)[:500]}\n→ {job.dest_dir}\n{job.summary_text()}"
            if job.error:
                tip += f"\n{job.error}"
            if job.partial_paths:
                tip += "\n" + tr("Possible partial files:") + "\n" + "\n".join(job.partial_paths)
            return tip
        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            return icon({
                JobStatus.COMPLETED: "dialog-information",
                JobStatus.FAILED: "dialog-error",
                JobStatus.CANCELLED: "process-stop",
                JobStatus.INTERRUPTED: "dialog-warning",
                JobStatus.PENDING: "media-playback-pause",
            }.get(job.status, "media-playback-start"))
        return None


class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> None:
        if index.column() != 3:
            return super().paint(painter, option, index)
        pct = index.data(PCT_ROLE)
        job: TransferJob = index.data(JOB_ROLE)
        opt = QStyleOptionProgressBar()
        opt.rect = QRect(option.rect.adjusted(3, 4, -3, -4))
        opt.minimum = 0
        opt.maximum = 1000
        opt.textVisible = True
        if pct is None:
            opt.progress = 0
            opt.text = tr("indeterminate") if job and job.status in ACTIVE_STATES else ""
        else:
            opt.progress = int(pct * 10)
            opt.text = f"{pct:.0f}%"
        opt.state = option.state | QStyle.StateFlag.State_Enabled
        QApplication.style().drawControl(QStyle.ControlElement.CE_ProgressBar, opt, painter)


class TransfersView(QWidget):
    partial_cleanup_requested = Signal(str, list)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.model = JobsModel(ctx)
        self.current_job_id: str | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        # progress card -------------------------------------------------------
        card = QFrame()
        card.setObjectName("progressCard")
        grid = QGridLayout(card)
        self.lbl_title = QLabel(tr("No active transfer"))
        f = self.lbl_title.font()
        f.setBold(True)
        self.lbl_title.setFont(f)
        self.lbl_paths = QLabel()
        self.lbl_paths.setObjectName("hint")
        self.lbl_paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_bar = QProgressBar()
        self.file_bar.setRange(0, 1000)
        self.file_bar.setFormat(tr("Current file"))
        self.batch_bar = QProgressBar()
        self.batch_bar.setRange(0, 1000)
        self.lbl_file = QLabel()
        self.lbl_batch = QLabel()
        self.lbl_stats = QLabel()
        self.lbl_source = QLabel()
        self.lbl_source.setObjectName("hint")
        self.lbl_queue = QLabel()
        grid.addWidget(self.lbl_title, 0, 0, 1, 3)
        grid.addWidget(self.lbl_paths, 1, 0, 1, 3)
        grid.addWidget(QLabel(tr("File:")), 2, 0)
        grid.addWidget(self.file_bar, 2, 1)
        grid.addWidget(self.lbl_file, 2, 2)
        grid.addWidget(QLabel(tr("Batch:")), 3, 0)
        grid.addWidget(self.batch_bar, 3, 1)
        grid.addWidget(self.lbl_batch, 3, 2)
        grid.addWidget(self.lbl_stats, 4, 0, 1, 2)
        grid.addWidget(self.lbl_queue, 4, 2)
        grid.addWidget(self.lbl_source, 5, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        lay.addWidget(card)

        # toolbar -------------------------------------------------------------
        tb = QToolBar()
        self.a_pause = tb.addAction(icon("media-playback-pause"), tr("Pause queue"), self.toggle_pause)
        self.a_pause.setToolTip(tr("Pause/resume the queue at job boundaries (the running transfer finishes)"))
        self.a_cancel = tb.addAction(icon("process-stop"), tr("Cancel"), self.cancel_selected)
        self.a_retry = tb.addAction(icon("view-refresh"), tr("Retry"), self.retry_selected)
        self.a_up = tb.addAction(icon("arrow-up"), tr("Move up"), lambda: self.move_selected(-1))
        self.a_down = tb.addAction(icon("arrow-down"), tr("Move down"), lambda: self.move_selected(1))
        self.a_details = tb.addAction(icon("document-properties"), tr("Details"), self.show_details)
        self.a_partial = tb.addAction(icon("edit-delete"), tr("Remove partial files…"), self.remove_partials)
        tb.addSeparator()
        self.a_remove = tb.addAction(icon("edit-delete"), tr("Remove from list"), self.remove_selected)
        self.a_clear = tb.addAction(icon("edit-clear-history"), tr("Clear completed"), self.clear_completed)
        lay.addWidget(tb)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(3, ProgressDelegate(self.table))
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.doubleClicked.connect(lambda _i: self.show_details())
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, w in ((0, 110), (2, 150), (3, 160), (4, 70), (5, 150), (6, 90), (7, 60), (8, 200)):
            hh.resizeSection(col, w)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_actions())
        lay.addWidget(self.table, 1)

        ctx.bridge.job_changed.connect(self._on_job_changed)
        ctx.bridge.progress.connect(self._on_progress)
        self._update_actions()
        self._update_card(None)

    # ----------------------------------------------------------------- signals
    def _on_job_changed(self, job: TransferJob) -> None:
        self.model.job_updated(job)
        if job.status in ACTIVE_STATES:
            self.current_job_id = job.id
        if job.id == self.current_job_id and job.finished_state:
            self._update_card(self.model.progress.get(job.id), job)
        self._update_actions()
        self._update_queue_label()

    def _on_progress(self, snap: ProgressSnapshot) -> None:
        self.model.progress[snap.job_id] = snap
        self.current_job_id = snap.job_id
        job = self.ctx.queue.get(snap.job_id)
        if job and job in self.model.jobs:
            self.model.job_updated(job)
        self._update_card(snap, job)

    def _update_queue_label(self) -> None:
        finished, total, active = self.ctx.queue.overall()
        txt = tr("Queue: {f}/{t} finished, {a} running").format(f=finished, t=total, a=active)
        if self.ctx.queue.paused:
            txt += " · " + tr("PAUSED")
        self.lbl_queue.setText(txt)

    def _update_card(self, snap: ProgressSnapshot | None, job: TransferJob | None = None) -> None:
        self._update_queue_label()
        if snap is None:
            self.lbl_title.setText(tr("No active transfer"))
            self.lbl_paths.setText("")
            for bar in (self.file_bar, self.batch_bar):
                bar.setRange(0, 1000)
                bar.setValue(0)
            self.lbl_file.setText("")
            self.lbl_batch.setText("")
            self.lbl_stats.setText("")
            self.lbl_source.setText("")
            return
        title = job.title if job else snap.job_id
        phase = tr(job.status.value) if job else snap.phase
        self.lbl_title.setText(f"{phase}: {title}" + (f" — {snap.current_file}" if snap.current_file else ""))
        if snap.source or snap.destination:
            self.lbl_paths.setText(f"{snap.source}  →  {snap.destination}")
        elif snap.message:
            self.lbl_paths.setText(snap.message)
        # current file
        finished = job is not None and job.finished_state
        if snap.file_percent is None and not finished:
            self.file_bar.setRange(0, 0)  # busy indicator: progress unknown
            self.lbl_file.setText(tr("indeterminate") + (f" · {format_bytes(snap.file_total)}" if snap.file_total else ""))
        else:
            self.file_bar.setRange(0, 1000)
            pct = 100.0 if finished and job and job.status == JobStatus.COMPLETED else (snap.file_percent or 0.0)
            self.file_bar.setValue(int(pct * 10))
            self.file_bar.setFormat(f"{pct:.1f}%")
            done = snap.file_bytes_done
            self.lbl_file.setText(f"{format_bytes(done)} / {format_bytes(snap.file_total)}" if done is not None
                                  else f"? / {format_bytes(snap.file_total)}")
        # batch
        bp = snap.batch_percent
        if bp is None:
            self.batch_bar.setRange(0, 0)
        else:
            self.batch_bar.setRange(0, 1000)
            self.batch_bar.setValue(int(bp * 10))
            self.batch_bar.setFormat(f"{bp:.1f}%")
        self.lbl_batch.setText(tr("{fd}/{ft} files · {bd} / {bt}").format(
            fd=snap.batch_files_done, ft=snap.batch_files_total, bd=format_bytes(snap.batch_bytes_done),
            bt=format_bytes(snap.batch_total)))
        speed = f"{format_bytes(snap.speed)}/s" if snap.speed else tr("measuring…") if not finished else "—"
        self.lbl_stats.setText(tr("Speed: {s} · Elapsed: {e} · Remaining: {r}").format(
            s=speed, e=format_duration(snap.elapsed), r=format_duration(snap.eta) if snap.eta is not None else "—"))
        source_text = {
            ProgressSource.ADB_PERCENT: tr("Progress source: percentage reported by adb"),
            ProgressSource.ADB_BYTES: tr("Progress source: byte count reported by adb"),
            ProgressSource.DESTINATION_SIZE: tr("Progress source: measured size of the destination file"),
            ProgressSource.COMPLETED: tr("File completed"),
            ProgressSource.NONE: tr("Progress for this file is indeterminate: adb reports no progress here"),
        }[snap.file_source]
        self.lbl_source.setText(source_text if not finished else (job.summary_text() if job else ""))

    # ----------------------------------------------------------------- actions
    def selected_job(self) -> TransferJob | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return rows[0].data(JOB_ROLE)

    def _update_actions(self) -> None:
        job = self.selected_job()
        self.a_pause.setText(tr("Resume queue") if self.ctx.queue.paused else tr("Pause queue"))
        self.a_pause.setIcon(icon("media-playback-start" if self.ctx.queue.paused else "media-playback-pause"))
        self.a_cancel.setEnabled(bool(job and (job.status == JobStatus.PENDING or job.status in ACTIVE_STATES)))
        self.a_retry.setEnabled(bool(job and job.status in (JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED)))
        pending = bool(job and job.status == JobStatus.PENDING)
        self.a_up.setEnabled(pending)
        self.a_down.setEnabled(pending)
        self.a_details.setEnabled(job is not None)
        self.a_remove.setEnabled(bool(job and job.status not in ACTIVE_STATES))
        self.a_partial.setEnabled(bool(job and job.partial_paths and job.finished_state))

    def toggle_pause(self) -> None:
        if self.ctx.queue.paused:
            self.ctx.queue.resume()
        else:
            self.ctx.queue.pause()
        self._update_actions()
        self._update_queue_label()

    def cancel_selected(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        if job.status in ACTIVE_STATES and not ask_yes_no(
            self, tr("Cancel transfer"),
            tr("Cancel the running transfer?\n\nThe file being transferred will be left incomplete at the destination; "
               "you will be offered to remove it afterwards.")):
            return
        self.ctx.queue.cancel(job.id)

    def retry_selected(self) -> None:
        job = self.selected_job()
        if job and self.ctx.queue.retry(job.id):
            self.model.reload()

    def move_selected(self, delta: int) -> None:
        job = self.selected_job()
        if job and self.ctx.queue.move(job.id, delta):
            self.model.reload()
            row = self.model.jobs.index(job)
            self.table.selectRow(row)

    def remove_selected(self) -> None:
        job = self.selected_job()
        if job and self.ctx.queue.remove(job.id):
            self.model.progress.pop(job.id, None)
            self.model.reload()
        self._update_actions()

    def clear_completed(self) -> None:
        self.ctx.queue.clear_finished()
        self.model.reload()
        self._update_actions()
        self._update_queue_label()

    def show_details(self) -> None:
        job = self.selected_job()
        if job:
            JobDetailsDialog(job, self).exec()

    def remove_partials(self) -> None:
        job = self.selected_job()
        if not job or not job.partial_paths:
            return
        text = tr("Delete these possibly incomplete files?\n\n{paths}").format(paths="\n".join(job.partial_paths))
        if not ask_yes_no(self, tr("Remove partial files"), text):
            return
        paths = list(job.partial_paths)
        if job.direction == Direction.DOWNLOAD:
            errors = []
            for p in paths:
                try:
                    if os.path.lexists(p):
                        os.unlink(p)
                    self.ctx.logbook.info("Partial download removed", op_id=job.id, op_type="delete", source=p,
                                          result="success", extra={"confirmed": True})
                except OSError as exc:
                    errors.append(f"{p}: {exc}")
            job.partial_paths = []
            if errors:
                show_error(self, tr("Remove partial files"), "\n".join(errors))
        else:
            from openadb.core.operations import delete_remote

            def work():  # type: ignore[no-untyped-def]
                return delete_remote(self.ctx.rfs_for(job.serial), paths, recursive=False, confirmed=True,
                                     logbook=self.ctx.logbook)

            def done(outcome) -> None:  # type: ignore[no-untyped-def]
                job.partial_paths = [r.path for r in outcome.failed]
                if outcome.failed:
                    show_error(self, tr("Remove partial files"), "\n".join(f"{r.path}: {r.message}" for r in outcome.failed))
                self._update_actions()

            self.ctx.tasks.submit(work, on_done=done, on_error=lambda e: show_error(self, tr("Remove partial files"), str(e)))
        self._update_actions()

    def _menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        m = QMenu(self)
        for a in (self.a_cancel, self.a_retry, self.a_up, self.a_down, self.a_details, self.a_partial, self.a_remove):
            m.addAction(a)
        m.exec(self.table.viewport().mapToGlobal(pos))

