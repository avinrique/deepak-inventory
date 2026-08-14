"""Password hashing for User.hashed_password, using Argon2id (argon2-cffi's
default variant since v18 — winner of the Password Hashing Competition,
resistant to both GPU-cracking and side-channel attacks, which is why it's
used here instead of scrypt/bcrypt/PBKDF2).

hash_password() output is self-describing (embeds algorithm + cost
parameters, e.g. "$argon2id$v=19$m=65536,t=3,p=4$..."), so verify_password()
never needs to know what parameters a given hash was created with, and
needs_rehash() lets AuthService transparently upgrade older hashes (e.g.
after a cost-parameter change) the next time that user logs in.
"""
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    return _hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return _hasher.verify(encoded, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(encoded: str) -> bool:
    """True if ``encoded`` was hashed with different parameters than the
    current ``_hasher`` uses — call after a successful verify_password() and
    re-hash+store if so, to migrate users onto new cost parameters over
    time without forcing a mass password reset.
    """
    try:
        return _hasher.check_needs_rehash(encoded)
    except InvalidHashError:
        return True
