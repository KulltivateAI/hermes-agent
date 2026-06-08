"""Tests for the open-PR completion guard in ``kanban_db.complete_task``.

Regression coverage for the false-completion class bug: when a worker
(or a dead-worker reconcile sweep, or a manual CLI completion) hands off a
summary that admits the referenced PR is still open/unmerged, the task must
be re-blocked (review-required) instead of marked ``done`` — otherwise the
board reports unshipped work as shipped (the t_7c98d480 / PR #836 incident).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# _detect_open_pr_in_handoff — the pure detector
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        # Present-tense open assertions → detected.
        ("Worker (pid None) dead, PR #836 is OPEN", 836),
        ("review-required: PR #831 still open", 831),
        ("PR #840 unmerged", 840),
        ("PR #842 is not yet merged", 842),
        ("PR #843 is not merged", 843),
        ("the PR #844 remains open pending review", 844),
        ("PR #845 currently open", 845),
        # Past-tense narration with a merge confirmation → NOT detected
        # (the open mention is historical).
        ("PR #836 was OPEN but BEHIND, squash-merged to main", None),
        ("PR #850 was open; has been merged into main", None),
        ("PR #851 was not merged earlier but is now merged", None),
        ("opened PR #852, then auto-merged after CI green", None),
        # No PR reference at all.
        ("all tests pass, shipping", None),
        ("", None),
        (None, None),
        # A merged-only handoff with no open assertion → not detected.
        ("PR #860 squash-merged to main, branch deleted", None),
        # --- review-finding regressions (commit follow-up) ---------------
        # FUTURE / IMPERATIVE merge intent must NOT count as a merge
        # confirmation — these are unshipped-work handoffs and must be
        # detected as still-open (fail-safe direction).
        ("PR #880 still open, needs to be merged into main", 880),
        ("PR #881 is OPEN but will be merged to main next sprint", 881),
        ("PR #882 is open; should be merged into main after review", 882),
        # "open for discussion / open to feedback" describes a review state,
        # not unmerged code → NOT detected (no false re-block).
        ("PR #883 is open for discussion", None),
        ("PR #884 open to feedback from the team", None),
        ("PR #885 is open for review", None),
        # ...but "not merged to main" is a genuine open assertion and the
        # for/to exclusion must NOT swallow it (fail-safe direction).
        ("PR #886 is not merged to main yet", 886),
        ("PR #887 unmerged to main", 887),
    ],
)
def test_detect_open_pr_in_handoff(text, expected):
    assert kb._detect_open_pr_in_handoff(text) == expected


def test_detect_returns_first_open_pr_when_multiple():
    # Two open PRs mentioned — the first one is surfaced.
    txt = "PR #870 is open and PR #871 is also unmerged"
    assert kb._detect_open_pr_in_handoff(txt) == 870


# ---------------------------------------------------------------------------
# complete_task guard — the real incident path
# ---------------------------------------------------------------------------

def _running_task(conn, title="impl: ship the guard"):
    """Create a task and transition it ready -> running, as a worker would."""
    tid = kb.create_task(conn, title=title, assignee="backend")
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None, "task should have been claimable (ready -> running)"
    return tid


def test_complete_with_open_pr_handoff_reblocks_instead_of_done(kanban_home):
    """The core regression: a dead-worker handoff that says the PR is still
    open must move the task to ``blocked`` and raise — never ``done``."""
    with kb.connect() as conn:
        tid = _running_task(conn)

        with pytest.raises(kb.OpenPRCompletionError) as ei:
            kb.complete_task(
                conn, tid,
                summary="Worker (pid None) died. PR #836 is OPEN — guard not yet in main.",
            )
        assert ei.value.pr_number == 836
        assert ei.value.completing_task_id == tid

        # Task is now blocked (review-required), NOT done.
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", f"expected blocked, got {task.status}"


def test_complete_with_open_pr_records_audit_event(kanban_home):
    with kb.connect() as conn:
        tid = _running_task(conn)
        with pytest.raises(kb.OpenPRCompletionError):
            kb.complete_task(conn, tid, result="PR #899 still open, needs merge")

        events = kb.list_events(conn, tid)
        kinds = [e.get("kind") if isinstance(e, dict) else getattr(e, "kind", None)
                 for e in events]
        assert "completion_redirected_open_pr" in kinds


def test_complete_succeeds_when_merge_confirmed_despite_open_narration(kanban_home):
    """Past-tense narration of an open PR that also confirms the merge must
    NOT trip the guard — a genuine merge handoff completes normally."""
    with kb.connect() as conn:
        tid = _running_task(conn)
        ok = kb.complete_task(
            conn, tid,
            summary="PR #836 was OPEN but BEHIND; rebased and squash-merged to main.",
        )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "done"


def test_complete_succeeds_for_clean_handoff(kanban_home):
    """A normal completion with no PR-open assertion is unaffected."""
    with kb.connect() as conn:
        tid = _running_task(conn)
        ok = kb.complete_task(conn, tid, summary="All gates green, merged, board updated.")
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"


def test_allow_open_pr_bypasses_guard(kanban_home):
    """The escape hatch: ``allow_open_pr=True`` lets a genuinely terminal
    completion that narrates an open PR (e.g. for a follow-up task) proceed."""
    with kb.connect() as conn:
        tid = _running_task(conn)
        ok = kb.complete_task(
            conn, tid,
            summary="Closing this tracker; PR #900 is still open under the follow-up task.",
            allow_open_pr=True,
        )
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"


def test_already_blocked_task_stays_blocked_and_raises(kanban_home):
    """The real dead-worker incident shape: the task was ALREADY blocked
    (review-required), then a reconcile sweep tries to complete it. The
    guard must still prevent blocked -> done and raise."""
    with kb.connect() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="review-required: PR #836 open")
        assert kb.get_task(conn, tid).status == "blocked"

        with pytest.raises(kb.OpenPRCompletionError) as ei:
            kb.complete_task(conn, tid, summary="reconcile sweep: PR #836 is OPEN")
        assert ei.value.pr_number == 836
        # Still blocked — never silently flipped to done.
        assert kb.get_task(conn, tid).status == "blocked"
