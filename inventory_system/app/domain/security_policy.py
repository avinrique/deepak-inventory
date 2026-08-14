"""Pure password-policy validation — no I/O, no framework. Policy values
themselves live on Organization (see app.models.organization) so they are
real, persisted, per-organization configuration rather than a hardcoded
rule; this module only knows how to check a candidate password against
whatever policy it's handed.

Applied to user-supplied passwords only (UserService.create_user's
initial_password, AuthService.change_password's new_password) — never to
admin-generated temporary passwords (UserService.reset_password), which
are high-entropy random strings by construction and don't need policy
gating.
"""
from dataclasses import dataclass

_SPECIAL_CHARS = set("!@#$%^&*()-_=+[]{}|;:'\",.<>/?`~\\")


@dataclass(frozen=True)
class PasswordPolicy:
    min_length: int
    require_uppercase: bool
    require_number: bool
    require_special_char: bool


def validate_password(password: str, policy: PasswordPolicy) -> list[str]:
    errors = []
    if len(password) < policy.min_length:
        errors.append(f"Password must be at least {policy.min_length} characters long.")
    if policy.require_uppercase and not any(c.isupper() for c in password):
        errors.append("Password must contain at least one uppercase letter.")
    if policy.require_number and not any(c.isdigit() for c in password):
        errors.append("Password must contain at least one number.")
    if policy.require_special_char and not any(c in _SPECIAL_CHARS for c in password):
        errors.append("Password must contain at least one special character.")
    return errors
