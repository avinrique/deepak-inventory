from app.domain.user import normalize_email, normalize_username, validate_user


def test_normalize_email_strips_and_lowercases():
    assert normalize_email("  Ada@Acme.Test  ") == "ada@acme.test"


def test_normalize_username_strips_and_lowercases():
    assert normalize_username("  Ada.Owner  ") == "ada.owner"


def test_valid_user_has_no_errors():
    errors = validate_user(full_name="Ada Owner", email="ada@acme.test",
                           username="ada.owner", phone="+1 555-0100")
    assert errors == []


def test_blank_full_name_is_rejected():
    errors = validate_user(full_name="   ", email="ada@acme.test", username="ada")
    assert any("name" in e.lower() for e in errors)


def test_malformed_email_is_rejected():
    errors = validate_user(full_name="Ada", email="not-an-email", username="ada")
    assert any("email" in e.lower() for e in errors)


def test_blank_username_is_rejected():
    errors = validate_user(full_name="Ada", email="ada@acme.test", username="   ")
    assert any("username" in e.lower() for e in errors)


def test_username_with_invalid_characters_is_rejected():
    errors = validate_user(full_name="Ada", email="ada@acme.test", username="Ada Owner!")
    assert any("username" in e.lower() for e in errors)


def test_username_none_is_not_an_error_by_itself():
    # None means "not decided yet, will be derived" — see the docstring;
    # only an explicit blank/invalid string is an error.
    errors = validate_user(full_name="Ada", email="ada@acme.test", username=None)
    assert errors == []


def test_missing_phone_is_not_an_error():
    errors = validate_user(full_name="Ada", email="ada@acme.test", username="ada", phone=None)
    assert errors == []


def test_malformed_phone_is_rejected():
    errors = validate_user(full_name="Ada", email="ada@acme.test", username="ada",
                           phone="call me maybe")
    assert any("phone" in e.lower() for e in errors)


def test_multiple_errors_are_all_reported():
    errors = validate_user(full_name="", email="bad", username="Bad Name!")
    assert len(errors) == 3
