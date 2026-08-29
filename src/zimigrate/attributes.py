from __future__ import annotations

import re
from collections.abc import Callable

from zimigrate.errors import CommandError
from zimigrate.models import Attributes

SENSITIVE_ATTRIBUTE = re.compile(
    r"(?:password|secret|token|credential|private.?key|authkey|dkimkey)", re.IGNORECASE
)

NEVER_EXPORT_ATTRIBUTES = {
    # Migrating live sessions would let source sessions authenticate to the target.
    "zimbraAuthTokens",
    "zimbraAuthTokenValidityValue",
    "zimbraCsrfTokenData",
}

# Account/identity prefs that store a signature UUID. Applied after signatures are
# created so destination IDs replace source IDs. zimbraPrefMailSignatureContactId is
# a contact UUID (zimbra-attrs.xml), not a signature UUID, so it is omitted.
SIGNATURE_REFERENCE_ATTRIBUTES = {
    "zimbraPrefDefaultSignatureId",
    "zimbraPrefForwardReplySignatureId",
    "zimbraPrefCalendarAutoAcceptSignatureId",
    "zimbraPrefCalendarAutoDeclineSignatureId",
    "zimbraPrefCalendarAutoDenySignatureId",
    "zimbraPrefCalendarAcceptSignatureId",
    "zimbraPrefCalendarTentativeSignatureId",
    "zimbraPrefCalendarDeclineSignatureId",
}

# ProvUtil.printAttr emits these as ldapsearch "::" base64. zmprov argv cannot restore
# DER/JPEG without ldapmodify; applying the LDIF alphabet would corrupt the value.
LDAP_BINARY_TRANSFER_ATTRIBUTES = {
    "userCertificate",
    "userSMIMECertificate",
    "jpegPhoto",
}

COMMON_READ_ONLY = {
    "objectClass",
    "zimbraCreateTimestamp",
    "zimbraId",
    "zimbraLastLogonTimestamp",
    "zimbraPasswordModifiedTime",
    "zimbraPasswordLockoutFailureTime",
    "zimbraPasswordLockoutLockedTime",
    "zimbraMailHost",
    "zimbraMailTransport",
    "zimbraMailDeliveryAddress",
    "zimbraMailAlias",
    "zimbraCOSId",
    "zimbraDomainDefaultCOSId",
    "zimbraDomainAliasTargetId",
    "zimbraDomainType",
    "zimbraGalAccountId",
    "zimbraACE",
    "zimbraSignatureId",
    "zimbraPrefMailSignatureContactId",
    "zimbraDataSourceId",
    "zimbraIdentityId",
    "zimbraUCServiceId",
    "uid",
    "mail",
    "dc",
}

KIND_READ_ONLY: dict[str, set[str]] = {
    "account": {"name", "zimbraAccountStatus", *SIGNATURE_REFERENCE_ATTRIBUTES},
    "calendar_resource": {"name", "zimbraAccountStatus", *SIGNATURE_REFERENCE_ATTRIBUTES},
    "domain": {"name", "zimbraDomainName"},
    "cos": {"name"},
    "distribution_list": {"name"},
    "dynamic_distribution_list": {"name"},
    "server": {"name", "zimbraServiceHostname"},
    "signature": {"name", "zimbraSignatureName"},
    "identity": {"name", "zimbraPrefIdentityName"},
    "data_source": {"name", "zimbraDataSourceName", "zimbraDataSourceType"},
}


def first(attributes: Attributes, name: str, default: str | None = None) -> str | None:
    values = attributes.get(name)
    return values[0] if values else default


def without_secrets(attributes: Attributes) -> Attributes:
    return {
        name: list(values)
        for name, values in attributes.items()
        if not SENSITIVE_ATTRIBUTE.search(name)
    }


def exportable_attributes(attributes: Attributes, *, include_secrets: bool) -> Attributes:
    result = {
        name: list(values)
        for name, values in attributes.items()
        if name not in NEVER_EXPORT_ATTRIBUTES
    }
    return result if include_secrets else without_secrets(result)


def mutable_attributes(
    kind: str,
    attributes: Attributes,
    *,
    allowlist: tuple[str, ...] | None = None,
    allow_sensitive: bool = True,
) -> Attributes:
    blocked = COMMON_READ_ONLY | KIND_READ_ONLY.get(kind, set()) | LDAP_BINARY_TRANSFER_ATTRIBUTES
    allowed = set(allowlist) if allowlist is not None else None
    return {
        name: list(values)
        for name, values in attributes.items()
        if name not in blocked
        and (allowed is None or name in allowed)
        and (allow_sensitive or not SENSITIVE_ATTRIBUTE.search(name))
    }


def attribute_operations(attributes: Attributes) -> list[tuple[str, list[str]]]:
    return [(name, values) for name, values in sorted(attributes.items()) if values]


def flatten_operations(attributes: list[tuple[str, list[str]]]) -> list[str]:
    result: list[str] = []
    for name, values in attributes:
        result.extend([name, values[0]])
        for value in values[1:]:
            result.extend([f"+{name}", value])
    return result


def remap_values(attributes: Attributes, mapping: dict[str, str]) -> Attributes:
    return {
        name: [mapping.get(value, value) for value in values] for name, values in attributes.items()
    }


def apply_attributes_resiliently(
    attributes: Attributes,
    apply: Callable[[list[str], bool], None],
    warn: Callable[[str, str], None],
    *,
    strict: bool,
) -> None:
    """Apply in batches, bisecting failures to identify unsupported attributes."""
    items = attribute_operations(attributes)
    for offset in range(0, len(items), 40):
        _apply_batch(items[offset : offset + 40], apply, warn, strict=strict)


def _apply_batch(
    items: list[tuple[str, list[str]]],
    apply: Callable[[list[str], bool], None],
    warn: Callable[[str, str], None],
    *,
    strict: bool,
) -> None:
    if not items:
        return
    # Attribute values can contain hashes, filters, forwarding addresses, or key data
    # even when the schema name itself is not obviously sensitive.
    sensitive = True
    try:
        apply(flatten_operations(items), sensitive)
        return
    except Exception as exc:
        if isinstance(exc, CommandError) and not exc.attribute_rejection:
            raise
        if len(items) > 1:
            midpoint = len(items) // 2
            _apply_batch(items[:midpoint], apply, warn, strict=strict)
            _apply_batch(items[midpoint:], apply, warn, strict=strict)
            return
        name = items[0][0]
        if strict:
            raise
        warn(name, f"target rejected attribute: {type(exc).__name__}")
