"""Project-specific exceptions."""


class ZimigrateError(Exception):
    """Base error for expected migration failures."""


class ConfigurationError(ZimigrateError):
    """Raised when configuration is missing or unsafe."""


class CommandError(ZimigrateError):
    """Raised when a Zimbra command fails."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        retryable: bool = False,
        attribute_rejection: bool = False,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.retryable = retryable
        self.attribute_rejection = attribute_rejection


class ArchiveError(ZimigrateError):
    """Raised when an archive is incomplete, corrupt, or cannot be decrypted."""


class CompatibilityError(ZimigrateError):
    """Raised when a source or destination does not satisfy preflight checks."""


class Interrupted(ZimigrateError):
    """Raised when the operator interrupts export or import."""
