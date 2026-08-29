from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

Attributes = dict[str, list[str]]
ATTRIBUTE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")


@dataclass(slots=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


@dataclass(slots=True)
class Artifact:
    label: str
    path: str
    sha256: str
    size: int
    query: str
    archive_format: str = "tgz"
    unpacked_size: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "query": self.query,
            "archive_format": self.archive_format,
            "unpacked_size": self.unpacked_size,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Artifact:
        required_strings = ("label", "path", "sha256", "query")
        if any(not isinstance(value.get(name), str) for name in required_strings):
            raise ValueError("mailbox artifact has an invalid string field")
        if any(not value[name] or "\x00" in value[name] for name in ("label", "path", "query")):
            raise ValueError("mailbox artifact has an empty or unsafe string field")
        size = value.get("size")
        unpacked_size = value.get("unpacked_size", 0)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("mailbox artifact size is invalid")
        if (
            not isinstance(unpacked_size, int)
            or isinstance(unpacked_size, bool)
            or unpacked_size < 0
        ):
            raise ValueError("mailbox artifact unpacked size is invalid")
        archive_format = value.get("archive_format", "tgz")
        if archive_format not in {"zip", "tgz"}:
            raise ValueError("mailbox artifact format is unsupported")
        encrypted = value.get("encrypted", False)
        if not isinstance(encrypted, bool):
            raise ValueError("mailbox artifact encryption marker is invalid")
        if encrypted:
            raise ValueError("encrypted mailbox artifacts are not supported")
        checksum = value["sha256"]
        if len(checksum) != 64 or any(
            character not in "0123456789abcdef" for character in checksum
        ):
            raise ValueError("mailbox artifact sha256 is invalid")
        # Schema v1 briefly wrote encryption-era fields even though the payload
        # was plaintext. Accept those archives only when both digests agree.
        plaintext_checksum = value.get("plaintext_sha256", checksum)
        if not isinstance(plaintext_checksum, str) or plaintext_checksum != checksum:
            raise ValueError("encrypted mailbox artifacts are not supported")
        return cls(
            label=value["label"],
            path=value["path"],
            sha256=value["sha256"],
            size=size,
            query=value["query"],
            archive_format=archive_format,
            unpacked_size=unpacked_size,
        )


@dataclass(slots=True)
class EntityRecord:
    kind: str
    name: str
    attributes: Attributes
    aliases: list[str] = field(default_factory=list)
    members: list[str] = field(default_factory=list)
    identities: list[Attributes] = field(default_factory=list)
    signatures: list[Attributes] = field(default_factory=list)
    data_sources: list[Attributes] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    source_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": self.kind,
            "name": self.name,
            "source_id": self.source_id,
            "attributes": self.attributes,
            "aliases": self.aliases,
            "members": self.members,
            "identities": self.identities,
            "signatures": self.signatures,
            "data_sources": self.data_sources,
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EntityRecord:
        kind = value.get("kind")
        name = value.get("name")
        source_id = value.get("source_id")
        if not isinstance(kind, str) or not kind:
            raise ValueError("archive entity kind is invalid")
        if not isinstance(name, str) or not name or _has_control_character(name):
            raise ValueError("archive entity name is invalid")
        if source_id is not None and (
            not isinstance(source_id, str) or not source_id or _has_control_character(source_id)
        ):
            raise ValueError("archive entity source ID is invalid")
        return cls(
            kind=kind,
            name=name,
            source_id=source_id,
            attributes=_attributes(value.get("attributes", {})),
            aliases=_strings(value.get("aliases", []), "aliases"),
            members=_strings(value.get("members", []), "members"),
            identities=[_attributes(item) for item in value.get("identities", [])],
            signatures=[_attributes(item) for item in value.get("signatures", [])],
            data_sources=[_attributes(item) for item in value.get("data_sources", [])],
            artifacts=[Artifact.from_dict(item) for item in _dicts(value.get("artifacts", []))],
        )


def _attributes(value: object) -> Attributes:
    if not isinstance(value, dict):
        raise ValueError("archive attributes are not an object")
    result: Attributes = {}
    for key, values in value.items():
        if (
            not isinstance(key, str)
            or not ATTRIBUTE_NAME.fullmatch(key)
            or not isinstance(values, list)
        ):
            raise ValueError("archive attribute has an invalid shape")
        if any(not isinstance(item, str) or "\x00" in item for item in values):
            raise ValueError("archive attribute contains a non-string value")
        result[key] = list(values)
    return result


def _strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or _has_control_character(item) for item in value
    ):
        raise ValueError(f"archive entity {label} are invalid")
    return list(value)


def _dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("archive entity sections are invalid")
    return value


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
