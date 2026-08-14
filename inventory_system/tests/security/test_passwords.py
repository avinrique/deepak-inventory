import pytest

from app.security.passwords import hash_password, needs_rehash, verify_password


def test_hash_uses_argon2id():
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("$argon2id$")


def test_hash_then_verify_round_trips():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded) is True


def test_wrong_password_fails():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("wrong password", encoded) is False


def test_hash_is_salted_differently_each_time():
    a = hash_password("same password")
    b = hash_password("same password")
    assert a != b
    assert verify_password("same password", a) is True
    assert verify_password("same password", b) is True


def test_empty_password_rejected():
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_rejects_garbage_encoded_value():
    assert verify_password("anything", "not-a-real-hash") is False


def test_needs_rehash_false_for_current_parameters():
    encoded = hash_password("correct horse battery staple")
    assert needs_rehash(encoded) is False


def test_needs_rehash_true_for_garbage():
    assert needs_rehash("not-a-real-hash") is True
