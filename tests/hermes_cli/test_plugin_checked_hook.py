import pytest

from hermes_cli.plugins import PluginManager


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    return PluginManager()


def test_checked_hook_reports_swallowed_callback_failure(manager):
    attempts = []

    def fails_once(**_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient finalizer failure")
        return "ok"

    manager._hooks.setdefault("on_session_end", []).append(fails_once)
    first_results, first_complete = manager.invoke_hook_checked("on_session_end", session_id="s1")
    second_results, second_complete = manager.invoke_hook_checked("on_session_end", session_id="s1")

    assert first_results == []
    assert first_complete is False
    assert second_results == ["ok"]
    assert second_complete is True


def test_checked_bounded_hook_reports_system_exit_incomplete(manager):
    def exits(**_kwargs):
        raise SystemExit(2)

    manager._hooks.setdefault("on_session_end", []).append(exits)
    results, complete = manager.invoke_hook_checked("on_session_end", session_id="s2")

    assert results == []
    assert complete is False


def test_checked_bounded_hook_reports_keyboard_interrupt_incomplete(manager):
    def interrupts(**_kwargs):
        raise KeyboardInterrupt()

    manager._hooks.setdefault("on_session_end", []).append(interrupts)
    results, complete = manager.invoke_hook_checked("on_session_end", session_id="s3")

    assert results == []
    assert complete is False


@pytest.mark.parametrize("failure", [RuntimeError("boom"), SystemExit(2), KeyboardInterrupt()])
def test_pre_tool_callback_failures_remain_fail_closed(manager, failure):
    def fails(**_kwargs):
        raise failure

    manager._hooks.setdefault("pre_tool_call", []).append(fails)
    results, complete = manager.invoke_hook_checked("pre_tool_call", tool_name="terminal", args={})

    assert complete is False
    assert results and results[-1]["action"] == "block"
