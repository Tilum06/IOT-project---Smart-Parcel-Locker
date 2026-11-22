"""Password hashing and opaque, expiring bearer sessions; no built-in credentials."""
import hashlib
import hmac
import secrets
import time
import sqlite3


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    result = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600_000)
    return f'pbkdf2_sha256$600000${salt}${result.hex()}'


def verify_password(password: str, stored: str) -> bool:
    _, iterations, salt, expected = stored.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(iterations))
    return hmac.compare_digest(actual.hex(), expected)


def issue_token(conn: sqlite3.Connection, account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute('INSERT INTO auth_sessions VALUES (?,?,?)',
                 (digest(token), account_id, time.time() + 3600))
    return token
