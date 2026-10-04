"""Authorization-envelope regressions for structured /goal contracts."""
import json
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import goals
from hermes_cli.goal_command import dispatch_goal_command


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    yield
    goals._DB_CACHE.clear()


def _judge(disposition, reason, verdict="blocked"):
    return verdict, reason, False, {"disposition": disposition}, False


def _set(sid="goal", **contract):
    mgr = goals.GoalManager(sid)
    mgr.set("fix and ship it live", contract=goals.GoalContract(verification="pytest", **contract))
    return mgr


def test_fields_parse_render_roundtrip_and_draft_drops_authorization():
    headline, contract = goals.parse_contract(
        "Ship fix\nauthority: reviewed deploy\nhuman gates: Alice approves pricing\nexceptions: /review"
    )
    assert headline == "Ship fix"
    restored = goals.GoalState.from_json(goals.GoalState("x", contract=contract).to_json())
    assert "Authority: reviewed deploy" in restored.contract.render_block()
    payload = json.dumps({"outcome": "send offer", "verification": "receipt", "authority": "charge cards"})
    response = MagicMock(choices=[MagicMock(message=MagicMock(content=payload))])
    with patch("agent.auxiliary_client.call_llm", return_value=response):
        assert goals.draft_contract("send offer").authority == ""


def test_old_structured_json_and_bare_goals_remain_semantically_empty():
    old = goals.GoalState.from_json(json.dumps({"goal": "old", "contract": {"verification": "pytest"}}))
    with patch.object(goals, "load_goal", return_value=old):
        assert goals.GoalManager("old").state.contract.authority == ""
    mgr = goals.GoalManager("bare")
    mgr.set("write a poem")
    assert not mgr.has_contract()
    assert mgr.next_continuation_prompt() == goals.CONTINUATION_PROMPT_TEMPLATE.format(goal="write a poem")


@pytest.mark.parametrize("outcome,authorized", [
    ("build the fix", True), ("ship it live", True),
    ("launch the site", False), ("deploy the site", False), ("review the fix", False),
])
def test_authority_inference_uses_only_approved_action_semantics(outcome, authorized):
    mgr = goals.GoalManager(outcome)
    mgr.set(outcome, contract=goals.GoalContract(verification="check"))
    assert ("reviewed merge and deployment" in mgr.state.contract.authority) is authorized


@pytest.mark.parametrize("verdict,reason,response", [
    ("done", "looks good", "Ready to send customer emails"),
    ("continue", "Delete the production records next", "working"),
    ("blocked", "Production database schema change needs approval", "waiting"),
])
def test_protected_language_forces_human_gate_under_every_verdict(verdict, reason, response):
    mgr = _set(verdict)
    with patch.object(goals, "judge_goal", return_value=_judge("routine_choice", reason, verdict)):
        decision = mgr.evaluate_after_turn(response)
    assert decision["status"] == "paused"
    assert decision["disposition"] == "human_gate"


def test_named_gate_requires_exact_identity_and_default_accepts_named_approver():
    alice = goals._authorization_envelope("fix", goals.GoalContract(human_gates="Alice Smith approves release"))
    assert goals._matches_declared_human_gate("Alice Smith must approve", alice)
    assert not goals._matches_declared_human_gate("Alice Jones must approve", alice)
    assert not goals._matches_declared_human_gate("Alice must approve", alice)
    default = goals._authorization_envelope("fix", goals.GoalContract(verification="pytest"))
    assert goals._matches_declared_human_gate("Drew must approve", default)
    assert not goals._matches_declared_human_gate("send the report", default)


def test_undeclared_ordinary_choice_continues_correctively():
    mgr = _set("ordinary")
    with patch.object(goals, "judge_goal", return_value=_judge("human_gate", "Someone chooses a library")):
        decision = mgr.evaluate_after_turn("Need a choice")
    assert decision["should_continue"] and decision["disposition"] == "routine_choice"


def test_exception_review_fence_blocks_resume_until_admin_resolution():
    mgr = _set("routine")
    blocked = _judge("routine_choice", "Need someone to choose a library")
    with patch.object(goals, "judge_goal", return_value=blocked):
        mgr.evaluate_after_turn("choice?")
        assert mgr.evaluate_after_turn("still blocked")["disposition"] == "exception_review"
    with pytest.raises(RuntimeError, match="exception review"):
        mgr.resume()
    denied = dispatch_goal_command(mgr, "exception-reviewed approve", authorize_gate=lambda: "admin only")
    assert denied.error and denied.output == "admin only"
    approved = dispatch_goal_command(mgr, "exception-reviewed approve", authorize_gate=lambda: None)
    assert not approved.error
    assert mgr.resume().status == "active"


def test_rejecting_exception_review_clears_goal():
    mgr = _set("reject")
    mgr.state.status, mgr.state.blocked_disposition = "paused", "exception_review"
    mgr._save()
    result = dispatch_goal_command(mgr, "exception-reviewed reject", authorize_gate=lambda: None)
    assert not result.error and not goals.GoalManager("reject").has_goal()


@pytest.mark.parametrize("response", [
    "CI passed, PR opened, awaiting maintainer approval",
    "Pull request opened and CI passed",
    "Awaiting review and approval",
])
def test_partial_delivery_cannot_take_done_path(response):
    mgr = _set(response)
    with patch.object(goals, "judge_goal", return_value=_judge("", "looks good", verdict="done")):
        decision = mgr.evaluate_after_turn(response)
    assert decision["verdict"] == "continue" and mgr.state.status == "active"
    assert "open PR" in goals.JUDGE_SYSTEM_PROMPT and "green CI" in goals.JUDGE_SYSTEM_PROMPT


@pytest.mark.parametrize("disposition", ["external_prerequisite", "no_safe_path"])
def test_terminal_block_dispositions_park(disposition):
    mgr = _set(disposition)
    with patch.object(goals, "judge_goal", return_value=_judge(disposition, "cannot proceed")):
        assert mgr.evaluate_after_turn("blocked")["disposition"] == disposition
