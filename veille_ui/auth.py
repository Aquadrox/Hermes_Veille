"""Authentification : un compte administrateur, session signée, CSRF, anti-force brute."""
from __future__ import annotations

import hmac
import os
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, URLSafeTimedSerializer

COOKIE = "veille_session"
SESSION_MAX_AGE = 12 * 3600
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60

_hasher = PasswordHasher()
_failures: dict[str, list[float]] = {}
_lock = threading.Lock()


class Settings:
    def __init__(self) -> None:
        self.password_hash = os.environ.get("VEILLE_UI_PASSWORD_HASH", "").strip().strip("'\"")
        self.secret = os.environ.get("VEILLE_UI_SECRET", "").strip().strip("'\"")
        self.cookie_secure = os.environ.get("VEILLE_UI_COOKIE_SECURE", "false").lower() == "true"
        extra = os.environ.get("VEILLE_UI_ALLOWED_HOSTS", "")
        self.allowed_hosts = ["localhost", "127.0.0.1", "[::1]"] + [h.strip() for h in extra.split(",") if h.strip()]

    @property
    def configured(self) -> bool:
        return bool(self.password_hash and self.secret)


def serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret or "unconfigured", salt="veille-ui-session")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(settings: Settings, password: str) -> bool:
    if not settings.configured or not password:
        return False
    try:
        return _hasher.verify(settings.password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def new_session(settings: Settings) -> str:
    return serializer(settings).dumps({"u": "admin", "csrf": secrets.token_urlsafe(32)})


def read_session(settings: Settings, cookie: str | None) -> dict | None:
    if not cookie or not settings.configured:
        return None
    try:
        data = serializer(settings).loads(cookie, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    return data if isinstance(data, dict) and data.get("u") == "admin" else None


def csrf_ok(session: dict, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(str(session.get("csrf", "")), str(token))


def throttled(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        recent = [t for t in _failures.get(key, []) if now - t < FAILURE_WINDOW]
        _failures[key] = recent
        return len(recent) >= MAX_FAILURES


def record_failure(key: str) -> None:
    with _lock:
        _failures.setdefault(key, []).append(time.monotonic())


def reset_failures(key: str) -> None:
    with _lock:
        _failures.pop(key, None)
