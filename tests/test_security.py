from src.security import (
    create_session_token,
    hash_password,
    read_session_token,
    verify_password,
)


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("A-secure-password")
    second = hash_password("A-secure-password")
    assert first != second
    assert verify_password("A-secure-password", first)
    assert not verify_password("wrong", first)


def test_signed_session_round_trip():
    token = create_session_token("user-1", "user")
    assert read_session_token(token)["sub"] == "user-1"
    assert read_session_token(token + "tampered") is None

