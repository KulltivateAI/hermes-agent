"""Pure, inert Discord escalation navigation envelopes (no SDK or gateway imports)."""

_ANCHOR_URL = "https://hermes-agent.nousresearch.com/escalation-anchor/v1"
_SEVERITIES = {"warning": "🟡", "critical": "🔴"}
_MENTION_TOKENS = ("<@", "@everyone", "@here")


def _valid_summary(summary):
    return (
        isinstance(summary, str)
        and bool(summary.strip())
        and "\r" not in summary
        and "\n" not in summary
        and not any(token in summary for token in _MENTION_TOKENS)
    )


def build_escalation_anchor(summary: str, severity: str = "warning") -> dict:
    """Build a navigation-only payload; reject unsafe/oversized titles, never truncate."""
    if not isinstance(severity, str) or severity not in _SEVERITIES:
        raise ValueError("severity must be warning or critical")
    if not _valid_summary(summary) or len(summary) + 2 > 200:
        raise ValueError("summary must be nonblank, single-line, mention-free and fit a 200-character title")
    return {
        "content": "",
        "embeds": [{"title": f"{_SEVERITIES[severity]} {summary}", "url": _ANCHOR_URL}],
        "allowed_mentions": {"parse": []},
    }


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def is_trusted_escalation_anchor(message, trusted_sender_ids) -> bool:
    """Recognize only an exact envelope from a Discord-authenticated trusted bot.

    Malformed or extra-content messages are not anchors and retain normal admission
    rules. The URL is a discriminator, never a network destination to fetch.
    """
    try:
        if not trusted_sender_ids:
            return False
        author = _get(message, "author")
        author_id = _get(author, "id")
        if type(author_id) not in (str, int):
            return False
        author_id = str(author_id)
        if not author_id.isascii() or not author_id.isdecimal():
            return False
        author_id = author_id.lstrip("0")
        if not author_id or author_id not in trusted_sender_ids or _get(author, "bot") is not True:
            return False
        if _get(message, "webhook_id") is not None:
            return False
        kind = _get(message, "type")
        kind = getattr(kind, "value", kind)
        if type(kind) is not int or kind != 0:
            return False
        content = _get(message, "content")
        if content is not None and (not isinstance(content, str) or content != ""):
            return False
        if any(_get(message, key) for key in (
            "attachments", "stickers", "sticker_items", "components", "mentions",
            "role_mentions", "mention_roles", "mention_everyone",
        )):
            return False
        embeds = _get(message, "embeds")
        if not isinstance(embeds, (list, tuple)) or len(embeds) != 1:
            return False
        embed = embeds[0]
        if not isinstance(embed, dict):
            embed = embed.to_dict()
        if not isinstance(embed, dict) or set(embed) - {"title", "url", "type", "flags"}:
            return False
        if embed.get("url") != _ANCHOR_URL or embed.get("type", "rich") != "rich":
            return False
        title = embed.get("title")
        return (
            isinstance(title, str)
            and len(title) <= 200
            and title[:2] in ("🟡 ", "🔴 ")
            and _valid_summary(title[2:])
        )
    except Exception:
        # Duck-typed SDK objects/fixtures may expose malformed properties or embeds.
        # Recognition must never turn malformed input into an admission exception.
        return False
