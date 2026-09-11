"""Unit tests for the secret scrubber.

The credentials below are FAKE and deliberately shaped like real ones so the
patterns are actually exercised.  Never paste a live key into this file -- it is
published, and the whole point of `redact.py` is to keep secrets out of
searchable text.

The CJK cases matter: Python's `\\b` treats CJK characters as word characters,
so `\\bsk-` silently fails to match in `嗯sk-...`.  That is exactly how a live
key survived the first pass, so these cases are regression tests, not padding.
"""

from agentwiki import redact

# Fake, non-functional credentials.
FAKE_OPENAI = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
FAKE_GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s"
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_BEARER = "Bearer " + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"

LEAKY = [FAKE_OPENAI, FAKE_GITHUB, FAKE_AWS, FAKE_BEARER]


def test_plain_assignments():
    assert "REDACTED" in redact.scrub(f"OPENAI_API_KEY={FAKE_OPENAI}")
    assert "REDACTED" in redact.scrub(f'token = "{FAKE_GITHUB}"')
    assert "REDACTED" in redact.scrub(FAKE_AWS)
    assert "REDACTED" in redact.scrub(FAKE_BEARER)


def test_json_value():
    out = redact.scrub('{"key": "%s"}' % FAKE_OPENAI)
    assert FAKE_OPENAI not in out
    assert "REDACTED" in out


def test_cjk_glued_prefix():
    """A key butted straight against Chinese text must still be caught."""
    out = redact.scrub(f"嗯{FAKE_OPENAI}  deepseek 可以接入任意agent")
    assert FAKE_OPENAI not in out
    assert "REDACTED" in out


def test_cjk_both_sides():
    out = redact.scrub(f"这个是deepseek官网的{FAKE_OPENAI}你完成适配之后看一下")
    assert FAKE_OPENAI not in out


def test_cjk_keyword_assignment():
    out = redact.scrub("密码password=hunter2hunter2hunter2 请勿外传")
    assert "hunter2hunter2hunter2" not in out


def test_no_secret_survives():
    """Belt and braces: nothing from the leaky set may appear in output."""
    blob = " ".join(
        [
            f"OPENAI_API_KEY={FAKE_OPENAI}",
            f"嗯{FAKE_OPENAI}x",
            f"token={FAKE_GITHUB}",
            FAKE_AWS,
            FAKE_BEARER,
        ]
    )
    out = redact.scrub(blob)
    for secret in LEAKY:
        assert secret not in out, f"leaked: {secret}"


def test_empty_and_clean_input():
    assert redact.scrub("") == ""
    assert redact.scrub("nothing to see here") == "nothing to see here"
