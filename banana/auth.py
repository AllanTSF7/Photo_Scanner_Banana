"""Operator accounts and browser sessions.

Passwords: scrypt from the standard library (no new dependency), stored as
`scrypt$n$r$p$salt$hash` so the cost can be raised later without breaking old hashes.
Sessions: a random token in an HttpOnly, SameSite=Strict cookie; the database keeps only its SHA-256.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from banana.models import AuthSession, User, utcnow

COOKIE_NAME = "ps_session"
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
MIN_PASSWORD = 8


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def normalize_username(username: str) -> str:
    name = username.strip().lower()
    if not _USERNAME.match(name):
        raise ValueError("username: 1-32 characters, lowercase letters, digits, '.', '_' or '-'")
    return name


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")


def create_user(session: Session, username: str, password: str) -> User:
    name = normalize_username(username)
    check_password_rules(password)
    if session.exec(select(User).where(User.username == name)).first():
        raise ValueError(f"user {name!r} already exists")
    user = User(username=name, password_hash=hash_password(password))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def set_password(session: Session, username: str, password: str) -> None:
    check_password_rules(password)
    user = _get(session, username)
    user.password_hash = hash_password(password)
    session.add(user)
    _revoke_user_sessions(session, user.id)  # a new password signs out every other browser
    session.commit()


def set_disabled(session: Session, username: str, disabled: bool) -> None:
    user = _get(session, username)
    user.disabled = disabled
    session.add(user)
    if disabled:
        _revoke_user_sessions(session, user.id)
    session.commit()


def any_users(session: Session) -> bool:
    return session.exec(select(User.id)).first() is not None


def authenticate(session: Session, username: str, password: str) -> User | None:
    try:
        name = normalize_username(username)
    except ValueError:
        return None
    user = session.exec(select(User).where(User.username == name)).first()
    if user is None:
        verify_password(password, hash_password("timing-equalizer"))  # same cost whether or not the user exists
        return None
    if user.disabled or not verify_password(password, user.password_hash):
        return None
    return user


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(session: Session, user: User, days: int) -> str:
    token = secrets.token_urlsafe(32)
    session.add(AuthSession(token_hash=_token_hash(token), user_id=user.id, expires_at=utcnow() + timedelta(days=days)))
    session.commit()
    return token


def resolve_session(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    row = session.get(AuthSession, _token_hash(token))
    if row is None:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        session.delete(row)
        session.commit()
        return None
    user = session.get(User, row.user_id)
    return None if user is None or user.disabled else user


def end_session(session: Session, token: str | None) -> None:
    if token and (row := session.get(AuthSession, _token_hash(token))) is not None:
        session.delete(row)
        session.commit()


def _revoke_user_sessions(session: Session, user_id: int) -> None:
    for row in session.exec(select(AuthSession).where(AuthSession.user_id == user_id)):
        session.delete(row)


def _get(session: Session, username: str) -> User:
    user = session.exec(select(User).where(User.username == normalize_username(username))).first()
    if user is None:
        raise ValueError(f"no user {username!r}")
    return user
