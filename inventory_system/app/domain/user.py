"""Pure User field validation/normalization — no I/O, no repository access.
Mirrors app.domain.sales.validate_customer / app.domain.purchasing.
validate_supplier: UserService orchestrates this plus UserRepository: this
module only decides what a User's fields are allowed to look like.
"""
import re

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_RE = re.compile(r"^[a-z0-9._-]+$")
_PHONE_RE = re.compile(r"^[0-9+()\-.\s]{7,32}$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_user(*, full_name: str, email: str, username: str | None,
                  phone: str | None = None) -> list[str]:
    """username=None means "not decided yet, will be derived from email" —
    UserService.create_user's only caller of that shape — and is not
    itself an error; every other caller (update_user, and create_user
    once a username is known) always passes a real string.
    """
    errors = []
    if not full_name.strip():
        errors.append("Full name is required.")

    normalized_email = normalize_email(email)
    if not normalized_email or not _EMAIL_RE.match(normalized_email):
        errors.append("A valid email address is required.")

    if username is not None:
        normalized_username = normalize_username(username)
        if not normalized_username:
            errors.append("Username is required.")
        elif not _USERNAME_RE.match(normalized_username):
            errors.append(
                "Username may only contain lowercase letters, numbers, '.', '_', and '-'.")

    if phone is not None and phone.strip() and not _PHONE_RE.match(phone.strip()):
        errors.append("Phone number format is invalid.")

    return errors
