from app.domain.security_policy import PasswordPolicy, validate_password

_LENIENT = PasswordPolicy(min_length=4, require_uppercase=False, require_number=False,
                          require_special_char=False)
_STRICT = PasswordPolicy(min_length=10, require_uppercase=True, require_number=True,
                         require_special_char=True)


def test_lenient_policy_accepts_a_short_plain_password():
    assert validate_password("abcd", _LENIENT) == []


def test_lenient_policy_rejects_below_minimum_length():
    errors = validate_password("abc", _LENIENT)
    assert len(errors) == 1
    assert "4 characters" in errors[0]


def test_strict_policy_rejects_password_missing_every_requirement():
    errors = validate_password("short", _STRICT)
    assert len(errors) == 4  # length, uppercase, number, special char


def test_strict_policy_accepts_a_password_meeting_every_requirement():
    assert validate_password("Str0ng!Pass", _STRICT) == []


def test_strict_policy_reports_each_missing_requirement_independently():
    errors = validate_password("alllowercase", _STRICT)  # long enough, nothing else
    assert len(errors) == 3
    assert any("uppercase" in e for e in errors)
    assert any("number" in e for e in errors)
    assert any("special character" in e for e in errors)
