"""PII redaction is a security control, so it gets adversarial tests."""
from __future__ import annotations

from app.security.pii import RegexRedactor, redact_messages


def test_redacts_card_number_passing_luhn():
    result = RegexRedactor().redact("My card is 4532015112830366 please help")
    assert "4532015112830366" not in result.text
    assert result.found_pii


def test_ignores_digit_run_failing_luhn():
    """A random 16-digit number that isn't a card shouldn't be tagged CARD."""
    result = RegexRedactor().redact("Reference 1234567812345678 for the ticket")
    assert "CARD" not in "".join(result.counts)


def test_redacts_email_and_phone():
    result = RegexRedactor().redact("Reach me at alex@example.com or +1 215 555 0142")
    assert "alex@example.com" not in result.text
    assert "555" not in result.text


def test_redacts_account_number():
    result = RegexRedactor().redact("Account 90014455 has the money")
    assert "90014455" not in result.text


def test_short_numbers_survive():
    result = RegexRedactor().redact("I have 500 dollars and 12 accounts")
    assert "500" in result.text


def test_vault_round_trip_restores_original():
    original = "Send it to alex@example.com"
    result = RegexRedactor().redact(original)
    assert result.restore(result.text) == original


def test_placeholders_are_unique_per_occurrence():
    result = RegexRedactor().redact("a@b.com and c@d.com")
    assert len(set(result.vault)) == 2


def test_redact_messages_leaves_structure_intact():
    messages = [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "My IBAN is US33BANK00090014455"},
    ]
    out = redact_messages(messages)
    assert len(out) == 2
    assert out[0]["role"] == "system"
    assert "US33BANK00090014455" not in out[1]["content"]
    # Original must not be mutated in place.
    assert "US33BANK00090014455" in messages[1]["content"]
