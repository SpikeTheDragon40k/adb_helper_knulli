"""Exception hierarchy used across the backend."""

from __future__ import annotations


class OpenADBError(Exception):
    """Base class for all application errors."""


class AdbNotFoundError(OpenADBError):
    """The ADB executable could not be located."""


class CommandTimeoutError(OpenADBError):
    """A command exceeded its time limit and was terminated."""


class OperationCancelled(OpenADBError):
    """The user cancelled an operation."""


class DeviceUnavailableError(OpenADBError):
    """The target device disappeared, went offline or is unauthorized."""


class AdbCommandError(OpenADBError):
    """An ADB command finished with a failure status."""

    def __init__(self, message: str, returncode: int | None = None, output: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.output = output


class RemoteOperationError(AdbCommandError):
    """A remote filesystem operation failed."""


class UnsafePathError(OpenADBError):
    """A path cannot be handled safely (invalid characters, traversal, ...)."""


class ProtectedPathError(UnsafePathError):
    """A destructive operation targeted a protected location."""


class ParseError(OpenADBError):
    """Output from the device could not be parsed unambiguously."""
