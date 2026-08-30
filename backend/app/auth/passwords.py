"""Password hashing.

Argon2id with parameters at the OWASP minimum for a server that also runs a
database: 64 MiB, 3 iterations, 4 lanes. Verification costs ~50 ms, which is
the point -- it makes offline cracking of a stolen hash expensive and online
guessing slow, and the login endpoint is rate limited on top of that.
"""

from __future__ import annotations

import secrets
import string

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,   # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _hasher.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def generate_password(length: int = 20) -> str:
    """Unambiguous alphabet: no O/0, l/1/I. These get read off a screen and
    typed by hand during installation."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def password_problems(password: str) -> list:
    problems = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if not any(c.islower() for c in password):
        problems.append("Add a lowercase letter.")
    if not any(c.isupper() for c in password):
        problems.append("Add an uppercase letter.")
    if not any(c.isdigit() for c in password):
        problems.append("Add a number.")
    return problems
