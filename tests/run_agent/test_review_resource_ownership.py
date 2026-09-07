"""Review teardown must not own the live parent's tool resources."""

from types import SimpleNamespace

import pytest

import run_agent
from agent import background_review
from run_agent import AIAgent
from tests.run_agent.test_background_review import _bare_agent


@pytest.mark.parametrize("crash", [False, True])
def test_real_review_lifecycle_closes_only_review_resources(monkeypatch, crash):
    parent = _bare_agent()
    resources = {
        kind: {parent.session_id: SimpleNamespace(alive=True)}
        for kind in ("job", "environment", "browser", "cua")
    }
    parent_resources = {kind: rows[parent.session_id] for kind, rows in resources.items()}
    captured = {}

    def release(kind, task_id):
        resource = resources[kind].pop(task_id, None)
        if resource:
            resource.alive = False

    # Stateful resource fixtures: no actual process killing or backend calls.
    from tools.process_registry import process_registry
    import tools.computer_use as cua
    monkeypatch.setattr(process_registry, "kill_all", lambda task_id: release("job", task_id))
    monkeypatch.setattr(run_agent, "cleanup_vm", lambda task_id: release("environment", task_id))
    monkeypatch.setattr(run_agent, "cleanup_browser", lambda task_id: release("browser", task_id))
    monkeypatch.setattr(cua, "release_computer_use_session", lambda task_id: release("cua", task_id))
    monkeypatch.setattr("hermes_cli.mem_trim.trim_memory", lambda **kw: None)
    monkeypatch.setattr(background_review, "_resolve_review_runtime", lambda agent: {"routed": False})
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda **kw: [])

    class Review(AIAgent):
        def __init__(self, **kwargs):
            # Isolate provider initialization; retain the REAL lifecycle/close.
            self.session_id = "review-constructor-session"
            self._session_messages = []
            self.client = None
            self._session_db = None
            captured["review"] = self

        def run_conversation(self, **kwargs):
            captured["ran"] = True
            captured["task_id"] = kwargs.get("task_id") or "review-generated-turn"
            captured["prompt"] = self._cached_system_prompt
            captured["session_id"] = self.session_id
            captured["resources"] = {}
            for kind, rows in resources.items():
                resource = SimpleNamespace(alive=True)
                rows[captured["task_id"]] = resource
                captured["resources"][kind] = resource
            if crash:
                raise RuntimeError("fixture review failure")

        def shutdown_memory_provider(self):
            pass

    monkeypatch.setattr(run_agent, "AIAgent", Review)
    # Exercise the real review worker, including its success/exception finally.
    background_review._run_review_in_thread(parent, [], "fixture review")

    assert captured.get("ran"), "review did not reach its execution seam"
    assert captured["prompt"] == parent._cached_system_prompt
    assert captured["session_id"] == parent.session_id  # cache attribution unchanged
    assert all(resource.alive for resource in parent_resources.values()), "review closed parent resources"
    assert captured["task_id"] != parent.session_id
    assert all(not resource.alive for resource in captured["resources"].values()), "review leaked its resources"
    assert parent._active_children == []
    assert parent._background_review_agent is None
    # Repeated close remains confined to this review's namespace.
    captured["review"].close()
    assert all(resource.alive for resource in parent_resources.values())
