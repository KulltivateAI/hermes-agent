"""Native Discord regressions need the real locked SDK on every Linux slice."""
from pathlib import Path
import shlex

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_linux_slices_provision_locked_native_messaging():
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    job = workflow["jobs"]["test"]
    assert job["runs-on"] == "ubuntu-latest"
    assert not job.get("continue-on-error", False)
    step, = [s for s in job["steps"] if s.get("name") == "Install dependencies"]
    assert "if" not in step  # Not tied to a movable slice number.
    assert not step.get("continue-on-error", False)
    assert step["uses"] == "./.github/actions/retry"
    command = shlex.split(step["with"]["command"])
    assert command[:2] == ["uv", "sync"]
    assert "--locked" in command
    assert command[command.index("--python") + 1] == "3.11"
    extras = {command[i + 1] for i, token in enumerate(command) if token == "--extra"}
    assert {"all", "dev", "anthropic", "mistral", "fal", "modal", "daytona",
            "hindsight", "parallel-web", "messaging"} <= extras
