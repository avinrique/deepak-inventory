"""Authentication and authorization.

- passwords.py — Argon2id hashing/verification (User.hashed_password)
- permissions.py — canonical Permission/Role catalog, seeded by scripts/init_db.py
- session.py — in-process Session/SessionManager with idle timeout
- authorization.py — @require_permission, the actual enforcement boundary

See app/services/auth_service.py (login/logout/password change) and
app/services/user_service.py (activation, admin password reset) for how
these compose.
"""
