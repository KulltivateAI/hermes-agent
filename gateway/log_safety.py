"""Safe log projections for inbound gateway events."""

from __future__ import annotations


_REDACTED_WEBHOOK_PREVIEW = "[webhook payload redacted]"


def inbound_message_preview(platform: str, text: object, *, limit: int = 80) -> str:
    """Return a bounded preview without persisting private webhook payloads.

    Signed webhook bodies can carry short-lived capabilities (for example a
    generation claim token). Their content belongs in the trusted turn, never in
    process logs. Human chat platforms keep the existing bounded diagnostic
    preview.
    """
    if str(platform).strip().lower() == "webhook":
        return _REDACTED_WEBHOOK_PREVIEW
    return str(text or "")[:limit].replace("\n", " ")
