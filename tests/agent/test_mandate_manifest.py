import pytest

from agent.mandate_manifest import (
    AgentManifestError,
    load_agent_manifest,
    render_agent_mandate,
    resolve_manifest_route,
)


VALID = """\
version: 1
role: account_manager
owns: [renewals, client_health]
routes:
  shared_capability: ops
  business_decision: drew
protected: [release_approval, customer_send]
"""


def test_missing_manifest_is_absent(tmp_path):
    assert load_agent_manifest(tmp_path) is None


def test_valid_manifest_has_deterministic_safe_projection(tmp_path):
    (tmp_path / "AGENT_MANIFEST.yaml").write_text(VALID, encoding="utf-8")

    manifest = load_agent_manifest(tmp_path)

    assert manifest is not None
    assert render_agent_mandate(manifest) == (
        "# Agent Mandate\n"
        "Role: account_manager\n"
        "Owns: client_health, renewals\n"
        "Routes:\n"
        "- business_decision -> drew\n"
        "- shared_capability -> ops\n"
        "Protected goal gates: customer_send, release_approval"
    )


@pytest.mark.parametrize("content", [
    "version: 1\nrole: ops\nrole: drew\nowns: []\nroutes: {}\nprotected: []\n",
    "version: 1\nrole: ops\nowns: ops\nroutes: {}\nprotected: []\n",
    "version: 1\nrole: ops\nowns: [work, work]\nroutes: {}\nprotected: []\n",
    "version: 1\nrole: Ops Team\nowns: []\nroutes: {}\nprotected: []\n",
    "version: 1\nrole: ops\nowns: []\nroutes:\n  shared_capability: [ops, drew]\nprotected: []\n",
    "version: 1\nrole: ops\nowns: []\nroutes: {}\nprotected: []\nclient_secret: do-not-project\n",
    "version: 1\nrole: ops\nowns: []\nroutes: {}\nprotected: [Release Approval]\n",
    "version: 2\nrole: ops\nowns: []\nroutes: {}\nprotected: []\n",
])
def test_invalid_manifest_is_rejected_without_partial_state(tmp_path, content):
    (tmp_path / "AGENT_MANIFEST.yaml").write_text(content, encoding="utf-8")
    with pytest.raises(AgentManifestError):
        load_agent_manifest(tmp_path)


def test_oversize_manifest_is_rejected(tmp_path):
    path = tmp_path / "AGENT_MANIFEST.yaml"
    path.write_bytes(VALID.encode() + b"#" * (16 * 1024))
    with pytest.raises(AgentManifestError, match="16 KiB"):
        load_agent_manifest(tmp_path)


def test_routes_are_single_target_and_named_approver_wins_exactly(tmp_path):
    (tmp_path / "AGENT_MANIFEST.yaml").write_text(VALID, encoding="utf-8")

    assert resolve_manifest_route(tmp_path, "shared_capability") == "ops"
    assert resolve_manifest_route(tmp_path, "business_decision") == "drew"
    assert resolve_manifest_route(tmp_path, "unknown") is None
    assert resolve_manifest_route(
        tmp_path, "shared_capability", named_approver="Drew Smith <drew@example.test>"
    ) == "Drew Smith <drew@example.test>"
