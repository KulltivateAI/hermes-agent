"""Integration coverage for denial-dominant pre_tool_call skip directives."""

import json
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import plugins
from hermes_cli.plugins import PluginManager
from model_tools import handle_function_call


@pytest.mark.parametrize("skip_first", [True, False])
def test_skip_prevents_dispatch_regardless_callback_order(monkeypatch, skip_first):
    """An actual registered skip callback vetoes modification in either order."""
    callback_calls = []

    def modify(**_kwargs):
        callback_calls.append("modify")
        return {"action": "modify", "args": {"query": "rewritten"}}

    def skip(**_kwargs):
        callback_calls.append("skip")
        return {"action": "skip", "reason": "denied by test policy"}

    manager = PluginManager()
    manager._discovered = True
    manager._hooks["pre_tool_call"] = [skip, modify] if skip_first else [modify, skip]
    monkeypatch.setattr(plugins, "_plugin_manager", manager)

    registry = MagicMock()
    registry.dispatch.side_effect = AssertionError("denied tool body must not execute")
    with patch("model_tools.registry", registry):
        result = handle_function_call("web_search", {"query": "original"})

    assert callback_calls == (["skip", "modify"] if skip_first else ["modify", "skip"])
    registry.dispatch.assert_not_called()
    assert "denied by test policy" in result


def test_skip_dominates_approval_without_opening_gate(monkeypatch):
    """A later skip vetoes an earlier authorization request without prompting."""
    manager = PluginManager()
    manager._discovered = True
    manager._hooks["pre_tool_call"] = [
        lambda **_kwargs: {"action": "approve", "message": "authorize this"},
        lambda **_kwargs: {"action": "skip", "reason": "policy veto"},
    ]
    monkeypatch.setattr(plugins, "_plugin_manager", manager)

    registry = MagicMock()
    with (
        patch("model_tools.registry", registry),
        patch(
            "tools.approval.request_tool_approval",
            side_effect=AssertionError("skip must dominate authorization"),
        ),
    ):
        result = handle_function_call("web_search", {"query": "original"})

    registry.dispatch.assert_not_called()
    assert "policy veto" in result


def test_skip_reason_is_bounded_and_secret_redacted(monkeypatch):
    sample_credential = "sk-proj-ABCD1234567890EFGHIJKLMNOP"
    manager = PluginManager()
    manager._discovered = True
    manager._hooks["pre_tool_call"] = [
        lambda **_kwargs: {
            "action": "skip",
            "reason": f"credential={sample_credential} " + ("x" * 2_000),
        }
    ]
    monkeypatch.setattr(plugins, "_plugin_manager", manager)

    registry = MagicMock()
    with patch("model_tools.registry", registry):
        result = handle_function_call("web_search", {"query": "original"})

    registry.dispatch.assert_not_called()
    assert sample_credential not in result
    assert "***" in result
    assert len(json.loads(result)["error"]) <= 600


@pytest.mark.parametrize(
    ("tool_name", "args", "dispatch_name"),
    [
        ("tool_search", {"queries": ["secret tools"]}, "dispatch_tool_search"),
        ("tool_describe", {"names": ["secret_tool"]}, "dispatch_tool_describe"),
    ],
)
def test_skip_gates_inline_catalog_reads_before_dispatch(
    monkeypatch, tool_name, args, dispatch_name,
):
    """Direct bridge reads cannot bypass the pre_tool_call denial primitive."""
    manager = PluginManager()
    manager._discovered = True
    manager._hooks["pre_tool_call"] = [
        lambda **_kwargs: {"action": "skip", "reason": "catalog access denied"},
    ]
    monkeypatch.setattr(plugins, "_plugin_manager", manager)

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch(
            f"tools.tool_search.{dispatch_name}",
            side_effect=AssertionError("denied catalog body must not execute"),
        ) as dispatch,
    ):
        result = handle_function_call(tool_name, args)

    dispatch.assert_not_called()
    assert "catalog access denied" in result


def test_skip_gates_direct_tool_call_after_deferred_unwrap(monkeypatch):
    """The direct tool_call bridge applies policy to the resolved deferred tool."""
    seen_names = []

    def skip(**kwargs):
        seen_names.append(kwargs["tool_name"])
        return {"action": "skip", "reason": "deferred access denied"}

    manager = PluginManager()
    manager._discovered = True
    manager._hooks["pre_tool_call"] = [skip]
    monkeypatch.setattr(plugins, "_plugin_manager", manager)

    registry = MagicMock()
    registry.dispatch.side_effect = AssertionError("denied deferred body must not execute")
    with (
        patch("model_tools.registry", registry),
        patch(
            "model_tools._dispatch_bridge_tool",
            side_effect=[(None, ("deferred_operation", {"value": 1})), None],
        ),
    ):
        result = handle_function_call(
            "tool_call",
            {"name": "deferred_operation", "arguments": {"value": 1}},
        )

    assert seen_names == ["deferred_operation"]
    registry.dispatch.assert_not_called()
    assert "deferred access denied" in result


def test_non_denied_mutation_and_execution_are_unchanged(monkeypatch):
    manager = PluginManager()
    manager._discovered = True
    manager._hooks["pre_tool_call"] = [
        lambda **_kwargs: {"action": "modify", "args": {"query": "rewritten"}},
        lambda **_kwargs: None,
    ]
    monkeypatch.setattr(plugins, "_plugin_manager", manager)

    registry = MagicMock()
    registry.dispatch.return_value = '{"ok": true}'
    with patch("model_tools.registry", registry):
        result = handle_function_call("web_search", {"query": "original"})

    assert result == '{"ok": true}'
    registry.dispatch.assert_called_once()
    assert registry.dispatch.call_args.args[:2] == (
        "web_search",
        {"query": "rewritten"},
    )
