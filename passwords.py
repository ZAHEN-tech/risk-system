"""
Password policy and generation.

Rules (applied to every password a person or an administrator sets):
  * at least MIN_LENGTH characters;
  * at least 3 of the 4 character groups: lower-case, upper-case, digit, symbol;
  * not a well-known weak password and not containing the username.

Administrators never fall back to a shared default password: if they leave the
field blank, generate_password() creates a random one, which is shown to them
once so they can pass it on. The user must replace it at first sign-in.
"""
import re
import secrets

MIN_LENGTH = 10
GENERATED_LENGTH = 14

# Characters that are easy to confuse when read aloud or copied by hand are left out.
_LOWER = "abcdefghijkmnopqrstuvwxyz"
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_DIGITS = "23456789"
_SYMBOLS = "!#$%&*?@+-="

COMMON = {
    "password", "password1", "password12", "password123", "password1234", "passw0rd",
    "123456789", "1234567890", "qwerty123", "qwertyuiop", "letmein123", "welcome123",
    "iloveyou", "admin123", "administrator", "changeme", "university", "student123",
}


def generate_password(length=GENERATED_LENGTH):
    """Random password guaranteed to contain all four character groups."""
    groups = [_LOWER, _UPPER, _DIGITS, _SYMBOLS]
    chars = [secrets.choice(g) for g in groups]
    pool = "".join(groups)
    chars += [secrets.choice(pool) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def strength_problem(password, username=None):
    """Return a plain-English reason the password is too weak, or None if it is fine."""
    if len(password) < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters long."
    groups = sum(bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    if groups < 3:
        return ("Password must mix at least three of: lower-case letters, upper-case letters, "
                "numbers and symbols.")
    if password.lower() in COMMON:
        return "That password is too common. Please choose another."
    if username and len(username) >= 3 and username.lower() in password.lower():
        return "Password must not contain the username."
    return None
