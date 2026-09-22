from gateway.log_safety import inbound_message_preview


def test_private_webhook_payload_is_never_logged():
    token = "claim-token-that-must-not-reach-logs"
    payload = '{"request_id":"r1","claim_token":"%s"}' % token

    preview = inbound_message_preview("webhook", payload)

    assert preview == "[webhook payload redacted]"
    assert token not in preview
    assert "request_id" not in preview


def test_human_platform_keeps_bounded_diagnostic_preview():
    preview = inbound_message_preview("discord", "hello\nworld" + ("x" * 100))

    assert preview.startswith("hello world")
    assert "\n" not in preview
    assert len(preview) == 80
