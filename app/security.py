"""Password/session authentication and a server-side credential vault."""
import hashlib
import os
import secrets
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from time import monotonic
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from sqlalchemy import select, delete
from app.db import Account, LoginSession

passwords = PasswordHasher()
DUMMY_HASH = passwords.hash(secrets.token_urlsafe(32))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def vault(settings):
    if settings.encryption_key:
        return Fernet(settings.encryption_key.encode())
    return Fernet(local_secret(settings, 'encryption.key', Fernet.generate_key))


def local_secret(settings, name, factory):
    if settings.environment == 'production':
        raise ValueError('正式環境缺少必要密鑰。')
    directory = Path(settings.secret_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(factory())
        except FileExistsError:
            pass
    return path.read_bytes().strip()


def setup_secret(settings):
    if settings.setup_token:
        return settings.setup_token
    if settings.environment == 'production':
        return ''
    return local_secret(settings, 'setup-token', lambda: secrets.token_urlsafe(32).encode()).decode()


def verify_password(encoded, plain):
    try:
        return passwords.verify(encoded, plain)
    except (VerificationError, InvalidHashError):
        return False


class RateLimiter:
    """Bounded per-process window; deployment intentionally runs one worker."""
    def __init__(self):
        self.events = defaultdict(deque)
        self.lock = Lock()

    def hit(self, key, limit, seconds=60):
        now = monotonic()
        with self.lock:
            if len(self.events) >= 10000:
                self.events = defaultdict(deque, {k:v for k,v in self.events.items() if v and now-v[-1] < 3600})
                if key not in self.events and len(self.events) >= 10000:
                    raise HTTPException(429, '目前請求過多，請稍後再試。')
            queue = self.events[key]
            while queue and queue[0] <= now-seconds:
                queue.popleft()
            if len(queue) >= limit:
                raise HTTPException(429, '操作次數過多，請稍後再試。', headers={'Retry-After': str(seconds)})
            queue.append(now)


def current_user(request: Request):
    user = getattr(request.state, 'user', None)
    if not user:
        raise HTTPException(401, '請先登入。')
    return user


def admin_user(request: Request):
    user = current_user(request)
    if user.role != 'admin':
        raise HTTPException(403, '此操作僅限管理員。')
    return user


def read_session(db, token):
    session = db.get(LoginSession, digest(token)) if token else None
    if not session or session.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None):
        return None, None
    user = db.get(Account, session.account_id)
    if not user or user.disabled:
        return None, None
    return user, session


def issue_session(db, user, response, settings):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.execute(delete(LoginSession).where(LoginSession.expires_at <= datetime.now(timezone.utc).replace(tzinfo=None)))
    db.add(LoginSession(token_hash=digest(token), account_id=user.id, csrf=csrf,
                        expires_at=datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(hours=settings.session_hours)))
    response.set_cookie('daywork_session', token, httponly=True, secure=settings.environment == 'production',
                        samesite='strict', max_age=settings.session_hours*3600, path='/')
    return csrf
