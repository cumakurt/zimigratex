"""Project-specific exceptions."""


MISSING_OBJECT_MARKERS = (
    "account.no_such_account",
    "account.no_such_calendar_resource",
    "account.no_such_cos",
    "account.no_such_distribution_list",
    "account.no_such_domain",
    "account.no_such_group",
    "account.no_such_server",
)
ALREADY_EXISTS_MARKERS = (
    "account.account_exists",
    "account.alias_exists",
    "account.cos_exists",
    "account.data_source_exists",
    "account.distribution_list_exists",
    "account.domain_exists",
    "account.identity_exists",
    "account.member_exists",
    "account.server_exists",
    "account.signature_exists",
)


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
        zimbra_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.retryable = retryable
        self.attribute_rejection = attribute_rejection
        self.zimbra_code = zimbra_code


class ArchiveError(ZimigrateError):
    """Raised when an archive is incomplete, corrupt, or unsupported."""


class CompatibilityError(ZimigrateError):
    """Raised when a source or destination does not satisfy preflight checks."""


class Interrupted(ZimigrateError):
    """Raised when the operator interrupts export or import."""
