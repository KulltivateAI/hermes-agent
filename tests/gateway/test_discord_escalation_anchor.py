"""Receiver-only envelope contract, including the real YAML→adapter bridge."""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from discord_escalation_protocol import (
    build_escalation_anchor,
    is_trusted_escalation_anchor,
)


def anchor_message():
    return {
        **build_escalation_anchor("Needs a decision"),
        "author": {"id": "42", "bot": True},
        "type": 0,
    }


@pytest.mark.parametrize("severity,prefix", [("warning", "🟡"), ("critical", "🔴")])
def test_constructor(severity, prefix):
    assert build_escalation_anchor("Needs a decision", severity) == {
        "content": "",
        "embeds": [{
            "title": f"{prefix} Needs a decision",
            "url": "https://hermes-agent.nousresearch.com/escalation-anchor/v1",
        }],
        "allowed_mentions": {"parse": []},
    }
    assert len(build_escalation_anchor("x" * 198, severity)["embeds"][0]["title"]) == 200


@pytest.mark.parametrize("summary", ["", "  ", "x\ny", "x\ry", "<@42>", "<@&42>", "@everyone", "@here", "x" * 199, None, 1])
def test_invalid_summary(summary):
    with pytest.raises(ValueError):
        build_escalation_anchor(summary)


@pytest.mark.parametrize("severity", ["info", "Warning", None, [], 1])
def test_invalid_severity(severity):
    with pytest.raises(ValueError):
        build_escalation_anchor("summary", severity)


def test_exact_authenticated_envelope_and_sdk_objects():
    message = anchor_message()
    assert is_trusted_escalation_anchor(message, frozenset({"42"}))
    message["author"]["id"] = 42
    message["content"] = None
    message["embeds"][0].update(type="rich", flags=0)
    assert is_trusted_escalation_anchor(message, {"42"})
    embed = SimpleNamespace(to_dict=lambda: message["embeds"][0])
    obj = SimpleNamespace(**{**message, "author": SimpleNamespace(**message["author"]), "embeds": [embed], "type": SimpleNamespace(value=0)})
    assert is_trusted_escalation_anchor(obj, {"42"})


@pytest.mark.parametrize("author", [None, {}, {"id": "42", "bot": False}, {"id": "42", "bot": 1}, {"id": "43", "bot": True}, {"id": True, "bot": True}, {"id": [], "bot": True}])
def test_only_trusted_bot_identity(author):
    message = anchor_message()
    message["author"] = author
    assert not is_trusted_escalation_anchor(message, {"42"})


@pytest.mark.parametrize("key,value", [
    ("webhook_id", "12"), ("webhook_id", 0), ("content", " "), ("content", "hello"),
    ("type", 19), ("type", False), ("type", None), ("type", "0"),
    ("embeds", []), ("embeds", [{}, {}]), ("embeds", None), ("embeds", "url"),
    ("attachments", [{}]), ("stickers", [{}]), ("sticker_items", [{}]),
    ("components", [{}]), ("mentions", [42]), ("role_mentions", [42]),
    ("mention_roles", ["42"]), ("mention_everyone", True),
])
def test_extra_content_is_not_an_anchor(key, value):
    message = anchor_message()
    message[key] = value
    assert not is_trusted_escalation_anchor(message, {"42"})


@pytest.mark.parametrize("key,value", [
    ("url", "https://example.com"), ("url", None), ("title", "copied marker"),
    ("title", "🟡 "), ("title", "🟡 x\ny"), ("title", "🔴 @here"),
    ("title", "🟡 " + "x" * 199), ("title", None), ("type", "link"),
    *[(key, None) for key in ("description", "footer", "fields", "image", "thumbnail", "video", "author", "timestamp", "color", "provider", "unknown")],
])
def test_strict_embed_grammar(key, value):
    message = anchor_message()
    message["embeds"][0][key] = value
    assert not is_trusted_escalation_anchor(message, {"42"})


@pytest.mark.parametrize("message", [None, True, 1, "", [], {}, {"embeds": [object()]}])
def test_total_on_malformed_shapes(message):
    assert not is_trusted_escalation_anchor(message, {"42"})


def test_no_trust_and_broken_embed_are_not_suppressed():
    message = anchor_message()
    assert not is_trusted_escalation_anchor(message, set())
    assert not is_trusted_escalation_anchor(message, None)
    class BrokenEmbed:
        def to_dict(self):
            raise ValueError("malformed")
    message["embeds"] = [BrokenEmbed()]
    assert not is_trusted_escalation_anchor(message, {"42"})


def test_protocol_import_has_no_sdk_or_gateway_dependency():
    result = subprocess.run([sys.executable, "-c", """
import sys
class BlockImports:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'discord', 'plugins', 'gateway'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, BlockImports())
from discord_escalation_protocol import build_escalation_anchor
assert build_escalation_anchor('summary')['content'] == ''
"""], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def load_adapter(tmp_path, monkeypatch, name, policy):
    import yaml
    from gateway.config import Platform, load_gateway_config
    from plugins.platforms.discord.adapter import DiscordAdapter
    home = tmp_path / name
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = load_gateway_config().platforms[Platform.DISCORD]
    adapter = DiscordAdapter(config)
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=999, bot=True))
    return adapter


@pytest.mark.parametrize("policy,expected", [
    ({"discord": {"escalation_anchor_sender_ids": [42, "42", "0043"]}}, {"42", "43"}),
    ({"platforms": {"discord": {"extra": {"escalation_anchor_sender_ids": [42]}}}}, {"42"}),
    ({"discord": {"escalation_anchor_sender_ids": []}, "platforms": {"discord": {"extra": {"escalation_anchor_sender_ids": [42]}}}}, set()),
    ({"discord": {"escalation_anchor_sender_ids": [43]}, "platforms": {"discord": {"extra": {"escalation_anchor_sender_ids": [42]}}}}, {"43"}),
    ({"discord": {}}, set()),
])
def test_real_config_bridge_and_two_profile_isolation(tmp_path, monkeypatch, policy, expected):
    first = load_adapter(tmp_path, monkeypatch, "one", {"discord": {"escalation_anchor_sender_ids": [42]}})
    second = load_adapter(tmp_path, monkeypatch, "two", policy)
    assert first._escalation_anchor_sender_ids == frozenset({"42"})
    assert second._escalation_anchor_sender_ids == frozenset(expected)
    assert isinstance(second._escalation_anchor_sender_ids, frozenset)
    assert "escalation_anchor_sender_ids" in second.config.extra
    # Keep both adapters alive after switching profiles; no env read may change policy.
    monkeypatch.setenv("DISCORD_ESCALATION_ANCHOR_SENDER_IDS", "43")
    from plugins.platforms.discord.adapter import discord
    # Existing plugin test discovery may supply an opaque MagicMock SDK enum.
    monkeypatch.setattr(discord, "MessageType", SimpleNamespace(default=0, reply=19))
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "all")
    message = SimpleNamespace(**anchor_message())
    message.id = 123
    message.author = SimpleNamespace(id=42, bot=True)
    message.type = discord.MessageType.default
    message.channel = SimpleNamespace(id=777, parent_id=None)
    message.mentions = []
    assert first._discord_message_admission(message, claim=True) == (False, False)
    assert not first._dedup.contains("123")
    assert second._discord_message_admission(message, claim=False)[0] == ("42" not in expected)
    assert first._escalation_anchor_sender_ids == frozenset({"42"})
    assert second._escalation_anchor_sender_ids == frozenset(expected)


@pytest.mark.parametrize("invalid", [None, "42", 42, {}, [True], [None], [{}], [-1], [0], ["-2"], ["1.0"], [" 42"], [42, False], ["٤٢"]])
def test_invalid_policy_warns_and_disables_whole_list(tmp_path, monkeypatch, caplog, invalid):
    adapter = load_adapter(tmp_path, monkeypatch, "invalid", {"discord": {"escalation_anchor_sender_ids": invalid}})
    assert adapter._escalation_anchor_sender_ids == frozenset()
    assert "escalation_anchor_sender_ids" in caplog.text
    assert "WARNING" in caplog.text


def test_default_policy_registration():
    from hermes_cli.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["discord"]["escalation_anchor_sender_ids"] == []
