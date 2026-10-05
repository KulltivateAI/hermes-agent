"""Strict per-profile Agent Mandate Manifest loading and prompt rendering.

Runtime named-approver precedence belongs in the first real routing consumer
(Solution 5), which must preserve explicit named approvers over manifest routes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import yaml
from yaml.constructor import ConstructorError


MANIFEST_FILENAME, _MAX_BYTES = "AGENT_MANIFEST.yaml", 16 * 1024
_SLUG_RE, _GATE_ID_RE = (re.compile(p) for p in (r"^[a-z0-9][a-z0-9_-]{0,63}$", r"^[a-z0-9]+(?:_[a-z0-9]+)*$"))
_FIELDS = frozenset({"version", "role", "owns", "routes", "protected"})
_FRAME_PREFIX = "<!-- agent-manifest-state:v1;"


class AgentManifestError(ValueError): """The opted-in manifest is invalid, unreadable, or unsafe."""


@dataclass(frozen=True)
class AgentManifest:
    version: int; role: str; owns: tuple[str, ...]; routes: Mapping[str, str]; protected: tuple[str, ...]


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ConstructorError("while constructing a mapping", node.start_mark,
                                       "duplicate mapping key", key_node.start_mark)
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


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


def _read_manifest(path: Path) -> bytes | None:
    """Bounded, nonblocking read of one regular file without following links."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise AgentManifestError("AGENT_MANIFEST.yaml must be a regular file, not a symlink or device")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except Exception as exc:
        raise AgentManifestError("AGENT_MANIFEST.yaml cannot be opened safely") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise AgentManifestError("AGENT_MANIFEST.yaml changed or is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_BYTES + 1)
        if opened.st_size > _MAX_BYTES or len(raw) > _MAX_BYTES:
            raise AgentManifestError("AGENT_MANIFEST.yaml exceeds 16 KiB")
        return raw
    finally:
        os.close(fd)


def load_agent_manifest(profile_home: Path | str) -> AgentManifest | None:
    """Load strict v1 data; only a genuinely missing file is treated as absent."""
    raw = _read_manifest(Path(profile_home) / MANIFEST_FILENAME)
    if raw is None: return None
    try:
        data = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader)
    except Exception as exc:
        raise AgentManifestError("AGENT_MANIFEST.yaml is not valid UTF-8 YAML") from exc
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise AgentManifestError("manifest must contain exactly version, role, owns, routes, protected")
    if type(data["version"]) is not int or data["version"] != 1:
        raise AgentManifestError("version must be 1")
    if not isinstance(data["routes"], dict):
        raise AgentManifestError("routes must be a mapping")
    routes = {_slug(k, "route category"): _slug(v, "route target") for k, v in data["routes"].items()}
    return AgentManifest(1, _slug(data["role"], "role"), _unique_list(data["owns"], "owns", _slug),
                         MappingProxyType(routes), _unique_list(data["protected"], "protected", _gate_id))


def manifest_digest(manifest: AgentManifest) -> str:
    projected = [manifest.version, manifest.role, sorted(manifest.owns),
                 sorted(manifest.routes.items()), sorted(manifest.protected)]
    return hashlib.sha256(json.dumps(projected, separators=(",", ":")).encode()).hexdigest()


def render_manifest_state_frame(manifest: AgentManifest | None, profile_home: Path | str) -> str:
    """Renderer-owned first line; profile identity is bound but never disclosed."""
    identity = hashlib.sha256(str(Path(profile_home).expanduser().resolve()).encode()).hexdigest()
    state = f"digest:{manifest_digest(manifest)}" if manifest is not None else "none"
    return f"{_FRAME_PREFIX}profile-sha256={identity};state={state} -->"


def render_agent_mandate(manifest: AgentManifest, profile_home: Path | str) -> str:
    routes = [f"- {category} -> {_resolve_manifest_route(manifest, category)}" for category in sorted(manifest.routes)]
    return "\n".join([
        render_manifest_state_frame(manifest, profile_home), "# Agent Mandate",
        f"Role: {manifest.role}", f"Owns: {', '.join(sorted(manifest.owns)) or '(none)'}", "Routes:",
        *(routes or ["- (none)"]),
        f"Protected goal gates (mandatory human gates; never permission): {', '.join(sorted(manifest.protected)) or '(none)'}",
        "Canonical Goal Authorization, tool, and Action Catalog gates remain authoritative.",
    ])


def stored_prompt_manifest_stale(prompt: str, profile_home: Path | str) -> bool:
    """Compare only the canonical renderer-owned first line."""
    manifest = load_agent_manifest(profile_home)
    return prompt.split("\n", 1)[0] != render_manifest_state_frame(manifest, profile_home)


def _resolve_manifest_route(manifest: AgentManifest, category: str) -> str | None:
    """Resolve one exact route from the immutable manifest snapshot; never fall back."""
    return manifest.routes.get(category)
