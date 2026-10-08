from repo2skill.redact import redact_text, redact_value


def test_redacts_typed_secret_key_values_without_retaining_them():
    assert redact_value("api_token", "dummy-token-not-secret") == "[REDACTED:secret-key]"
    assert redact_value("numeric_secret", 42) == "[REDACTED:secret-key]"
    assert redact_value("boolean_secret", False) == "[REDACTED:secret-key]"
    assert redact_value("empty_secret", None) == "[REDACTED:secret-key]"


def test_redacts_material_categories_in_text():
    text = "Authorization: Bearer abc\nhttps://name@example.invalid/a\napi_token: dummy-token-not-secret\n"
    result = redact_text(text)
    assert "Bearer abc" not in result
    assert "name:pass" not in result
    assert "[REDACTED:authorization]" in result
    assert "[REDACTED:credential-url]" in result
    assert "dummy-token-not-secret" not in result


def test_redacts_private_key_payload_including_unclosed_material():
    result = redact_text("-----BEGIN RSA PRIVATE KEY-----\nprivate-payload\n")
    assert "private-payload" not in result
    assert result == "[REDACTED:private-key]"


def test_redacts_high_entropy_ascii_tokens():
    token = "A3fJ9KpQ7xV2mN8bR5tY1zC6"
    assert "[REDACTED:high-entropy]" in redact_text(token)


def test_redaction_is_idempotent_for_existing_secret_labels():
    value = "api_token: [REDACTED:secret-key]"
    assert redact_text(redact_text(value)) == redact_text(value)
