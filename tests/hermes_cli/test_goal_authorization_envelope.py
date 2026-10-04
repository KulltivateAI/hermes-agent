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


def _judge(disposition="", reason="working", verdict="continue", gate_id="", evidence_stage=None):
    directive = {"disposition": disposition}
    if gate_id:
        directive["gate_id"] = gate_id
    if evidence_stage is not None:
        directive["evidence_stage"] = evidence_stage
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


@pytest.mark.parametrize("outcome,boundaries,stage", [
    ("Deploy the fix live", "", "deployed"),
    ("Ship the release", "", "deployed"),
    ("Merge the fix", "", "merged"),
    ("Open a PR for the fix", "", "pr"),
    ("Deploy the fix live", "stop at PR", "pr"),
    ("Deploy the fix live", "do not deploy", "merged"),
    ("Ship a draft release note", "", "none"),
])
def test_completion_stage_is_derived_from_outcome_and_boundaries(outcome, boundaries, stage):
    mgr = _set(outcome, outcome=outcome, verification="anything", boundaries=boundaries)
    assert mgr.state.contract.completion_stage == stage


@pytest.mark.parametrize("outcome", [
    "Deploy the fix live",
    "Deploy after PR review",
    "Deploy live; PR is not stopping point",
])
def test_explicit_live_outcome_wins_over_pr_mentions(outcome):
    mgr = _set(outcome, goal="fix it", outcome=outcome, verification="PR URL exists")
    assert "merge/deploy workflow" in mgr.state.contract.authority.lower()
    assert "does not grant tool permission" in mgr.state.contract.authority


@pytest.mark.parametrize("verification", [
    "PR exists", "opened pull request exists", "release PR URL exists",
    "PR checks pass", "pull request #7 exists",
])
@pytest.mark.parametrize("outcome", ["Deploy the fix live", "Merge the fix"])
def test_pr_evidence_cannot_complete_deployed_or_merged_goal_regardless_of_verification_wording(verification, outcome):
    mgr = _set(verification, goal="fix it", outcome=outcome, verification=verification)
    with patch.object(goals, "judge_goal", return_value=_judge(
            reason="PR verified", verdict="done", evidence_stage="pr")):
        decision = mgr.evaluate_after_turn("Opened pull request #7")
    assert decision["verdict"] == "continue"
    assert decision["status"] == "active"
    assert f"completion_stage={goals._completion_stage(mgr.state.contract)}" in decision["continuation_prompt"]
    assert "evidence_stage=pr" in decision["continuation_prompt"]


@pytest.mark.parametrize("verification", [
    "PR exists", "opened pull request exists", "release PR URL exists",
    "PR checks pass", "pull request #7 exists",
])
def test_pr_evidence_can_complete_pr_only_goal_regardless_of_verification_wording(verification):
    mgr = _set("pr", goal="fix it", outcome="Open a PR for the fix", verification=verification)
    with patch.object(goals, "judge_goal", return_value=_judge(
            reason="PR verified", verdict="done", evidence_stage="pr")):
        decision = mgr.evaluate_after_turn("Opened pull request #7")
    assert decision["verdict"] == "done" and mgr.state.status == "done"


@pytest.mark.parametrize("outcome,evidence_stage", [
    ("Merge the fix", "merged"), ("Merge the fix", "deployed"), ("Deploy the fix live", "deployed"),
])
def test_sufficient_merged_or_deployed_evidence_can_complete(outcome, evidence_stage):
    mgr = _set(evidence_stage, goal="ship it", outcome=outcome, verification="release evidence exists")
    with patch.object(goals, "judge_goal", return_value=_judge(
            reason="delivery verified", verdict="done", evidence_stage=evidence_stage)):
        assert mgr.evaluate_after_turn("Delivery verified")["verdict"] == "done"


@pytest.mark.parametrize("evidence_stage", [None, "bogus", "pr"])
def test_missing_invalid_or_insufficient_stage_keeps_delivery_goal_active(evidence_stage):
    mgr = _set(str(evidence_stage), goal="ship it", outcome="Merge the fix", verification="release evidence")
    with patch.object(goals, "judge_goal", return_value=_judge(
            reason="done", verdict="done", evidence_stage=evidence_stage)):
        decision = mgr.evaluate_after_turn("done")
    assert decision["verdict"] == "continue"
    assert "completion_stage=merged" in decision["continuation_prompt"]


def test_old_judge_result_still_completes_non_delivery_legacy_goal():
    mgr = _set("legacy", goal="write report", outcome="Write a report", verification="report exists")
    with patch.object(goals, "judge_goal", return_value=_judge(reason="report verified", verdict="done")):
        assert mgr.evaluate_after_turn("Report exists")["verdict"] == "done"


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


@pytest.mark.parametrize("value,expected", [("pr", "pr"), ("garbage", "invalid")])
def test_judge_evidence_stage_is_structured_and_validated(value, expected):
    verdict, _, failed, directive = goals._parse_judge_response(
        json.dumps({"verdict": "done", "evidence_stage": value, "reason": "verified"}))
    assert (verdict, failed, directive["evidence_stage"]) == ("done", False, expected)


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
