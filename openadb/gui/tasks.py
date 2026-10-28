"""Run blocking work off the GUI thread and deliver results back on it."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

_log = logging.getLogger("openadb.tasks")


class TaskRunner(QObject):
    """Thread pool whose completion callbacks run on the thread owning this object."""

    _finished = Signal(object, object, object, object)  # on_done, on_error, result, exception

    def __init__(self, max_workers: int = 6, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="openadb-task")
        self._finished.connect(self._deliver)
        self._closed = False

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        **kwargs: Any,
    ) -> Future[Any]:
        def job() -> Any:
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - forwarded to the GUI thread
                _log.debug("Task failed: %s", "".join(traceback.format_exception(exc)))
                if not self._closed:
                    self._finished.emit(on_done, on_error, None, exc)
                raise
            if not self._closed:
                self._finished.emit(on_done, on_error, result, None)
            return result

        return self._pool.submit(job)

    @Slot(object, object, object, object)
    def _deliver(self, on_done: Any, on_error: Any, result: Any, exc: Any) -> None:
        try:
            if exc is not None:
                if on_error:
                    on_error(exc)
                else:
                    _log.error("Unhandled background error: %s", exc)
            elif on_done:
                on_done(result)
        except RuntimeError as err:  # widget deleted meanwhile
            _log.debug("Callback target gone: %s", err)

    def shutdown(self) -> None:
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
