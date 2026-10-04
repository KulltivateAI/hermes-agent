"""Strict per-profile Agent Mandate Manifest loading and routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from yaml.constructor import ConstructorError

MANIFEST_FILENAME = "AGENT_MANIFEST.yaml"
_MAX_BYTES = 16 * 1024
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_GATE_ID_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_FIELDS = frozenset({"version", "role", "owns", "routes", "protected"})


class AgentManifestError(ValueError):
    """The manifest exists but does not satisfy the strict v1 schema."""


@dataclass(frozen=True)
class AgentManifest:
    version: int
    role: str
    owns: tuple[str, ...]
    routes: dict[str, str]
    protected: tuple[str, ...]


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError("while constructing a mapping", node.start_mark,
                                   "duplicate mapping key", key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _slug(value, field: str) -> str:
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value):
        raise AgentManifestError(f"{field} must be a lowercase slug")
    return value


def _unique_list(value, field: str, validator) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AgentManifestError(f"{field} must be a list")
    normalized = tuple(validator(item, field) for item in value)
    if len(set(normalized)) != len(normalized):
        raise AgentManifestError(f"{field} must not contain duplicates")
    return normalized


def _gate_id(value, field: str) -> str:
    if not isinstance(value, str) or not _GATE_ID_RE.fullmatch(value):
        raise AgentManifestError(f"{field} must contain normalized goal gate IDs")
    return value


def load_agent_manifest(profile_home: Path | str) -> Optional[AgentManifest]:
    """Load and validate ``AGENT_MANIFEST.yaml`` from an explicit profile home."""
    path = Path(profile_home) / MANIFEST_FILENAME
    try:
        if not path.exists():
            return None
        with path.open("rb") as handle:
            raw = handle.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise AgentManifestError("AGENT_MANIFEST.yaml exceeds 16 KiB")
        data = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader)
    except AgentManifestError:
        raise
    except Exception as exc:
        raise AgentManifestError("AGENT_MANIFEST.yaml is not valid YAML") from exc
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise AgentManifestError("manifest must contain exactly version, role, owns, routes, protected")
    if type(data["version"]) is not int or data["version"] != 1:
        raise AgentManifestError("version must be 1")
    role = _slug(data["role"], "role")
    owns = _unique_list(data["owns"], "owns", _slug)
    routes_raw = data["routes"]
    if not isinstance(routes_raw, dict):
        raise AgentManifestError("routes must be a mapping")
    routes = {
        _slug(category, "route category"): _slug(target, "route target")
        for category, target in routes_raw.items()
    }
    protected = _unique_list(data["protected"], "protected", _gate_id)
    return AgentManifest(1, role, owns, routes, protected)


def render_agent_mandate(manifest: AgentManifest) -> str:
    """Render only normalized schema fields in a fixed deterministic form."""
    route_lines = [f"- {category} -> {target}" for category, target in sorted(manifest.routes.items())]
    return "\n".join([
        "# Agent Mandate",
        f"Role: {manifest.role}",
        f"Owns: {', '.join(sorted(manifest.owns)) or '(none)'}",
        "Routes:",
        *(route_lines or ["- (none)"]),
        f"Protected goal gates: {', '.join(sorted(manifest.protected)) or '(none)'}",
    ])


def resolve_manifest_route(
    profile_home: Path | str, category: str, *, named_approver=None
) -> Optional[str]:
    """Resolve exactly one target; an explicit named approver always wins unchanged."""
    if named_approver is not None:
        return named_approver
    manifest = load_agent_manifest(profile_home)
    return manifest.routes.get(category) if manifest is not None and isinstance(category, str) else None
