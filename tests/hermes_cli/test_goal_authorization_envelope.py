"""Authorization-envelope behavior for structured /goal contracts."""

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


def _judge(verdict, disposition, reason):
    return (verdict, reason, False, {"disposition": disposition}, False)


def test_judge_parser_preserves_block_disposition():
    verdict, _, failed, directive = goals._parse_judge_response(
        '{"verdict":"blocked","disposition":"routine_choice","reason":"choose"}'
    )
    assert (verdict, failed, directive) == ("blocked", False, {"disposition": "routine_choice"})


def test_authorization_fields_parse_render_and_roundtrip():
    headline, contract = goals.parse_contract(
        "Ship the fix\nauthority: code and reviewed deploy\n"
        "human gates: Drew approves pricing\nexceptions: use /review"
    )
    assert headline == "Ship the fix"
    assert contract.authority == "code and reviewed deploy"
    assert contract.human_gates == "Drew approves pricing"
    assert contract.exceptions == "use /review"
    restored = goals.GoalState.from_json(goals.GoalState("x", contract=contract).to_json())
    assert "Authority: code and reviewed deploy" in restored.contract.render_block()
    assert restored.contract.exceptions == "use /review"


def test_legacy_and_bare_free_form_goals_remain_unchanged():
    state = goals.GoalState.from_json(json.dumps({"goal": "old", "contract": {"verification": "pytest"}}))
    assert state.contract.authority == ""
    mgr = goals.GoalManager("bare")
    mgr.set("write a poem")
    assert not mgr.has_contract()
    assert mgr.next_continuation_prompt() == goals.CONTINUATION_PROMPT_TEMPLATE.format(goal="write a poem")


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("Build and ship the fix live", "reviewed merge and deployment"),
     ("Review the proposed fix", "not authorized")],
)
def test_merge_deploy_authority_is_conservatively_inferred_and_visible(outcome, expected):
    mgr = goals.GoalManager(f"authority-{expected}")
    mgr.set(outcome, contract=goals.GoalContract(verification="pytest passes"))
    assert expected in mgr.state.contract.authority.lower()
    shown = dispatch_goal_command(mgr, "show", authorize_gate=lambda: None).output
    assert "Authority:" in shown
    assert "payments/charges" in shown
    assert "any named approver" in shown


def test_explicit_authority_cannot_override_protected_gates():
    mgr = goals.GoalManager("protected")
    mgr.set("change auth", contract=goals.GoalContract(
        verification="auth tests pass", authority="change auth and deploy",
    ))
    assert "never overrides protected human gates" in mgr.state.contract.authority.lower()
    assert "auth/session" in mgr.state.contract.human_gates


def test_goal_draft_cannot_invent_protected_authorization():
    payload = json.dumps({
        "outcome": "send customers an offer", "verification": "receipt exists",
        "authority": "charge cards and send customers", "human_gates": "none",
        "exceptions": "skip review",
    })
    response = MagicMock(choices=[MagicMock(message=MagicMock(content=payload))])
    with patch("agent.auxiliary_client.call_llm", return_value=response):
        contract = goals.draft_contract("send customers an offer")
    assert contract.authority == ""
    assert contract.human_gates == ""
    assert contract.exceptions == ""


def test_declared_human_gate_parks_but_undeclared_gate_continues_correctively():
    contract = goals.GoalContract(verification="pytest", human_gates="Drew approves pricing")
    mgr = goals.GoalManager("declared")
    mgr.set("fix pricing", contract=contract)
    with patch.object(goals, "judge_goal", return_value=_judge("blocked", "human_gate", "Drew approves pricing")):
        decision = mgr.evaluate_after_turn("Need Drew to approve pricing")
    assert decision["status"] == "paused"
    assert decision["disposition"] == "human_gate"

    mgr = goals.GoalManager("undeclared")
    mgr.set("fix the parser", contract=goals.GoalContract(verification="pytest"))
    with patch.object(goals, "judge_goal", return_value=_judge("blocked", "human_gate", "Drew chooses a library")):
        decision = mgr.evaluate_after_turn("Need Drew to choose")
    assert decision["should_continue"] is True
    assert decision["disposition"] == "routine_choice"
    assert "smallest durable" in decision["continuation_prompt"]


def test_second_routine_blocker_parks_for_existing_review_path():
    mgr = goals.GoalManager("routine")
    mgr.set("fix parser", contract=goals.GoalContract(verification="pytest"))
    blocked = _judge("blocked", "routine_choice", "Need someone to choose a library")
    with patch.object(goals, "judge_goal", return_value=blocked):
        first = mgr.evaluate_after_turn("Which library?")
        second = mgr.evaluate_after_turn("Still need a choice")
    assert first["should_continue"] is True
    assert second["status"] == "paused"
    assert second["disposition"] == "exception_review"
    assert "/review" in second["message"]
    assert "Drew" not in second["message"]
    assert goals.GoalManager("routine").state.blocked_disposition == "exception_review"


@pytest.mark.parametrize("disposition", ["external_prerequisite", "no_safe_path"])
def test_terminal_block_dispositions_park(disposition):
    mgr = goals.GoalManager(disposition)
    mgr.set("ship it", contract=goals.GoalContract(verification="live check"))
    with patch.object(goals, "judge_goal", return_value=_judge("blocked", disposition, "cannot proceed")):
        decision = mgr.evaluate_after_turn("blocked")
    assert decision["status"] == "paused"
    assert decision["disposition"] == disposition
