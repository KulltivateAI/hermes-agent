"""Regression coverage for config.yaml ``discord.allow_bots`` resolution."""

import importlib
import os
import sys
import types

import pytest


@pytest.fixture
def adapter_mod():
    sys.modules.pop("plugins.platforms.discord.adapter", None)
    return importlib.import_module("plugins.platforms.discord.adapter")


def _seed(adapter_mod, yaml_cfg, discord_cfg):
    return adapter_mod._apply_yaml_config(yaml_cfg, discord_cfg)


def _fake_adapter(adapter_mod, extra, env_overrides=None):
    """A DiscordAdapter shell with only the gate-resolution state populated."""
    adapter = object.__new__(adapter_mod.DiscordAdapter)
    adapter.config = types.SimpleNamespace(extra=extra)
    snapshot = {key: "" for key in adapter_mod._GATE_ENV_KEYS}
    snapshot.update(env_overrides or {})
    adapter._gate_env_snapshot = snapshot
    return adapter


def test_yaml_allow_bots_is_seeded_into_extra(adapter_mod, monkeypatch):
    monkeypatch.delenv("DISCORD_ALLOW_BOTS", raising=False)
    monkeypatch.setattr(adapter_mod, "_profile_scoped_config_load", lambda: True)

    seeded = _seed(adapter_mod, {}, {"allow_bots": "HOOK_MENTIONS"})

    assert seeded is not None
    assert seeded.get("allow_bots") == "hook_mentions"


def test_platform_extra_allow_bots_is_seeded(adapter_mod, monkeypatch):
    monkeypatch.delenv("DISCORD_ALLOW_BOTS", raising=False)
    monkeypatch.setattr(adapter_mod, "_profile_scoped_config_load", lambda: True)

    seeded = _seed(
        adapter_mod,
        {"platforms": {"discord": {"extra": {"allow_bots": "mentions"}}}},
        {},
    )

    assert seeded is not None
    assert seeded.get("allow_bots") == "mentions"


def test_adapter_reads_allow_bots_from_extra_without_env(adapter_mod):
    adapter = _fake_adapter(adapter_mod, {"allow_bots": "hook_mentions"})

    assert adapter._get_allow_bots() == "hook_mentions"


def test_explicit_env_precedes_yaml_extra(adapter_mod, monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "ALL")

    seeded = _seed(adapter_mod, {}, {"allow_bots": "hook_mentions"})
    adapter = _fake_adapter(
        adapter_mod,
        seeded,
        {"DISCORD_ALLOW_BOTS": os.environ["DISCORD_ALLOW_BOTS"]},
    )

    assert seeded == {"allow_bots": "hook_mentions"}
    assert os.environ["DISCORD_ALLOW_BOTS"] == "ALL"
    assert adapter._get_allow_bots() == "all"


def test_adapter_rejects_unknown_allow_bots_mode(adapter_mod):
    assert _fake_adapter(adapter_mod, {"allow_bots": "typo"})._get_allow_bots() == "none"
