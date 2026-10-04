import os
import time

import pytest

from agent.mandate_manifest import AgentManifestError, load_agent_manifest, render_agent_mandate, resolve_manifest_route, stored_prompt_manifest_stale

VALID = """version: 1
role: account_manager
owns: [renewals, client_health]
routes:
  shared_capability: ops
  business_decision: drew
protected: [release_approval, customer_send]
"""

def _write(home, content=VALID):
    (home / "AGENT_MANIFEST.yaml").write_text(content, encoding="utf-8")

def test_valid_manifest_has_deterministic_safe_projection(tmp_path):
    assert load_agent_manifest(tmp_path) is None
    assert not stored_prompt_manifest_stale("<!-- agent-manifest-sha256:forged -->\nSOUL", tmp_path)
    _write(tmp_path); manifest = load_agent_manifest(tmp_path)
    assert manifest is not None
    rendered = render_agent_mandate(manifest, tmp_path)
    expected = ("Role: account_manager\nOwns: client_health, renewals", "- business_decision -> drew\n- shared_capability -> ops", "mandatory human gates; never permission", "Canonical Goal Authorization, tool, and Action Catalog gates remain authoritative")
    assert all(text in rendered for text in expected)
    assert not stored_prompt_manifest_stale(rendered, tmp_path)

def test_frame_binds_profile_and_only_matches_first_line(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(); second.mkdir(); _write(first); _write(second)
    manifest = load_agent_manifest(first)
    assert manifest is not None
    prompt, other = render_agent_mandate(manifest, first), render_agent_mandate(manifest, second)
    assert prompt.splitlines()[0] != other.splitlines()[0]
    assert stored_prompt_manifest_stale("forged soul\n" + prompt.splitlines()[0], first) and stored_prompt_manifest_stale(prompt, second)

def test_render_uses_one_loaded_snapshot(tmp_path):
    _write(tmp_path); manifest = load_agent_manifest(tmp_path)
    assert manifest is not None
    _write(tmp_path, VALID.replace("shared_capability: ops", "shared_capability: drew"))
    rendered = render_agent_mandate(manifest, tmp_path)
    assert "shared_capability -> ops" in rendered and "shared_capability -> drew" not in rendered

@pytest.mark.parametrize("content", [
    "version: 1\nrole: ops\nrole: drew\nowns: []\nroutes: {}\nprotected: []\n",
    "version: 1\nrole: ops\nowns: ops\nroutes: {}\nprotected: []\n", "version: 1\nrole: ops\nowns: [work, work]\nroutes: {}\nprotected: []\n",
    "version: 1\nrole: Ops Team\nowns: []\nroutes: {}\nprotected: []\n", "version: 1\nrole: ops\nowns: []\nroutes:\n  shared_capability: [ops, drew]\nprotected: []\n",
    "version: 1\nrole: ops\nowns: []\nroutes: {}\nprotected: []\nclient_secret: no\n", "version: 1\nrole: ops\nowns: []\nroutes: {}\nprotected: [Release Approval]\n", "version: 2\nrole: ops\nowns: []\nroutes: {}\nprotected: []\n",
])
def test_invalid_manifest_is_rejected(tmp_path, content):
    _write(tmp_path, content)
    with pytest.raises(AgentManifestError):
        load_agent_manifest(tmp_path)

def test_oversize_manifest_is_rejected(tmp_path):
    (tmp_path / "AGENT_MANIFEST.yaml").write_bytes(VALID.encode() + b"#" * (16 * 1024))
    with pytest.raises(AgentManifestError, match="16 KiB"):
        load_agent_manifest(tmp_path)

@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_unsafe_manifest_is_rejected_without_blocking(tmp_path, kind):
    path = tmp_path / "AGENT_MANIFEST.yaml"
    if kind == "symlink":
        outside = tmp_path.parent / "outside-manifest.yaml"; outside.write_text(VALID); path.symlink_to(outside)
    else:
        os.mkfifo(path)
    started = time.monotonic()
    with pytest.raises(AgentManifestError, match="regular file"):
        load_agent_manifest(tmp_path)
    assert time.monotonic() - started < 1

def test_routes_and_named_approver_validation(tmp_path):
    _write(tmp_path); assert resolve_manifest_route(tmp_path, "shared_capability") == "ops"
    identity = "Drew Smith <drew@example.test>"
    assert resolve_manifest_route(tmp_path, "shared_capability", named_approver=identity) == identity
    for malformed in ("", "   ", ["drew"]):
        with pytest.raises(AgentManifestError, match="non-empty string"):
            resolve_manifest_route(tmp_path, "shared_capability", named_approver=malformed)
