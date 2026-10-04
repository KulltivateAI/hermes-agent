"""Workflow-scope and structured human-gate regressions for /goal contracts."""
import json
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import goals
from hermes_cli.goal_command import dispatch_goal_command, is_goal_control


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    yield
    goals._DB_CACHE.clear()


def _judge(disposition="", reason="working", verdict="continue", gate_id=""):
    directive = {"disposition": disposition}
    if gate_id:
        directive["gate_id"] = gate_id
    return verdict, reason, False, directive, False


def _set(sid="goal", *, goal="fix it", **contract):
    mgr = goals.GoalManager(sid)
    mgr.set(goal, contract=goals.GoalContract(**contract))
    return mgr


def test_fields_roundtrip_and_draft_strips_model_invented_workflow_scope():
    headline, contract = goals.parse_contract(
        "Ship release\nauthority: merge after review\nhuman gates: release_approval"
    )
    assert headline == "Ship release"
    restored = goals.GoalState.from_json(goals.GoalState("x", contract=contract).to_json())
    assert "Workflow scope (not tool permission): merge after review" in restored.contract.render_block()
    payload = json.dumps({"outcome": "send offer", "verification": "receipt", "authority": "charge cards"})
    response = MagicMock(choices=[MagicMock(message=MagicMock(content=payload))])
    with patch("agent.auxiliary_client.call_llm", return_value=response):
        assert goals.draft_contract("send offer").authority == ""


def test_old_contracts_and_bare_goals_do_not_gain_new_metadata():
    old = goals.GoalState.from_json(json.dumps({"goal": "old", "contract": {"verification": "pytest"}}))
    assert old.contract.authority == "" and old.contract.human_gates == ""
    mgr = goals.GoalManager("bare")
    mgr.set("write a poem")
    assert not mgr.has_contract()


@pytest.mark.parametrize("outcome,verification,boundaries", [
    ("Open a PR for the fix", "PR URL exists", ""),
    ("Review the fix", "review verdict exists", "review only"),
    ("Draft a deployment plan", "plan file exists", "do not deploy"),
    ("Build the fix locally", "local tests pass", "local only; no merge"),
    ("Do not deploy the fix", "local tests pass", "no deployment"),
    ("Build the fix", "PR URL exists", "stop at PR"),
    ("Ship a draft release note", "draft note exists", ""),
])
def test_explicit_stopping_boundary_wins_over_fix_or_deploy_words(outcome, verification, boundaries):
    mgr = _set(outcome, goal="fix it", outcome=outcome, verification=verification, boundaries=boundaries)
    assert "No merge or deployment workflow scope" in mgr.state.contract.authority
    assert "not tool permission" in mgr.render_contract()


@pytest.mark.parametrize("outcome", [
    "Deploy the fix live",
    "Deploy after PR review",
    "Deploy live; PR is not stopping point",
])
def test_explicit_live_outcome_wins_over_pr_mentions(outcome):
    mgr = _set(outcome, goal="fix it", outcome=outcome, verification="PR URL exists")
    assert "merge/deploy workflow" in mgr.state.contract.authority.lower()
    assert "does not grant tool permission" in mgr.state.contract.authority


def test_live_outcome_rejects_done_when_contract_only_verifies_pr():
    mgr = _set("live-pr-proof", goal="fix it", outcome="Deploy the fix live", verification="PR URL exists")
    with patch.object(goals, "judge_goal", return_value=_judge(reason="PR URL verified", verdict="done")):
        decision = mgr.evaluate_after_turn("Opened https://github.com/acme/repo/pull/7")
    assert decision["verdict"] == "continue"
    assert decision["status"] == "active"
    assert "live verification" in decision["continuation_prompt"].lower()
    assert "correct the completion contract" in decision["continuation_prompt"].lower()


def test_pr_only_contract_can_complete_at_pr_url():
    mgr = _set("pr", goal="fix it", outcome="Open a PR for the fix", verification="PR URL exists")
    with patch.object(goals, "judge_goal", return_value=_judge(reason="PR URL verified", verdict="done")):
        decision = mgr.evaluate_after_turn("Opened https://github.com/acme/repo/pull/7")
    assert decision["verdict"] == "done" and mgr.state.status == "done"


def test_live_contract_continues_until_judge_confirms_live_verification():
    mgr = _set("prod", goal="ship it live", outcome="Deploy the fix live", verification="production check passes")
    with patch.object(goals, "judge_goal", return_value=_judge(reason="PR exists but production is unverified")):
        assert mgr.evaluate_after_turn("PR opened")["verdict"] == "continue"
    with patch.object(goals, "judge_goal", return_value=_judge(reason="production check passed", verdict="done")):
        assert mgr.evaluate_after_turn("Production check passed")["verdict"] == "done"


def test_exact_normalized_structured_gate_id_parks():
    mgr = _set("gate", verification="pytest", human_gates="release_approval")
    with patch.object(goals, "judge_goal", return_value=_judge(
        "human_gate", "release approval is required", "blocked", " Release-Approval ")):
        decision = mgr.evaluate_after_turn("waiting")
    assert decision["status"] == "paused" and decision["gate_id"] == "release_approval"


def test_structured_gate_id_survives_judge_json_parsing():
    verdict, _, failed, directive = goals._parse_judge_response(
        '{"verdict":"blocked","disposition":"human_gate","gate_id":"release_approval","reason":"waiting"}'
    )
    assert directive is not None
    assert (verdict, failed, directive["gate_id"]) == ("blocked", False, "release_approval")


@pytest.mark.parametrize("gate_id,reason", [
    ("", "Payment approval is required"),
    ("billing_approval", "A charge review is required"),
    ("", "No customer-send approval is required; continue"),
    ("", "Get sign-off before publishing"),
])
def test_missing_unmatched_or_synonym_prose_is_not_a_human_gate(gate_id, reason):
    mgr = _set(reason, verification="pytest", human_gates="release_approval")
    with patch.object(goals, "judge_goal", return_value=_judge("human_gate", reason, "blocked", gate_id)):
        decision = mgr.evaluate_after_turn("working")
    assert decision["status"] == "active" and decision["disposition"] == "routine_choice"


def test_two_routine_blockers_require_fresh_delegate_review_without_admin_bypass():
    mgr = _set("routine", verification="pytest")
    blocked = _judge("routine_choice", "Need someone to choose a library", "blocked")
    with patch.object(goals, "judge_goal", return_value=blocked):
        first = mgr.evaluate_after_turn("choice?")
        second = mgr.evaluate_after_turn("still blocked")
    persisted = goals.load_goal(mgr.session_id)
    assert persisted is not None
    assert first["status"] == second["status"] == "active"
    assert "delegate_task" in second["continuation_prompt"]
    assert "fresh independent exception review" in second["continuation_prompt"]
    assert f"Goal ID: {persisted.goal_id}" in second["continuation_prompt"]
    assert f"Goal generation: {persisted.generation}" in second["continuation_prompt"]
    assert "exception-reviewed" not in second["continuation_prompt"]
    assert is_goal_control("exception-reviewed approve")


def test_exception_review_text_cannot_clear_or_resume_a_goal():
    mgr = _set("fake-review", verification="pytest")
    original = mgr.state.goal
    result = dispatch_goal_command(mgr, "exception-reviewed approve", authorize_gate=lambda: None)
    assert result.error and "disabled" in result.output
    assert mgr.state.goal == original
    assert mgr.state.status == "active"
