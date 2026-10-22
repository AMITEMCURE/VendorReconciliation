"""Lightweight auth for the reconciliation portal.

Mirrors the import payment portal's approach (JWT via python-jose, bcrypt via
passlib) but keeps users in a small config list rather than a database, since
this portal has no DB of its own. Users come from settings.PORTAL_USERS.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def _users() -> dict[str, dict]:
    """username -> {password, full_name}. Parsed from settings.PORTAL_USERS,
    a comma-separated list of 'username:password:Full Name' triples."""
    out: dict[str, dict] = {}
    for entry in settings.PORTAL_USERS.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) < 2:
            continue
        username = parts[0].strip()
        password = parts[1].strip()
        full_name = parts[2].strip() if len(parts) > 2 else username
        if username and password:
            out[username] = {"password": password, "full_name": full_name}
    return out


def authenticate(username: str, password: str) -> dict | None:
    user = _users().get((username or "").strip())
    if not user:
        return None
    if password != user["password"]:
        return None
    return {"username": username, "full_name": user["full_name"]}


def create_access_token(username: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": username, "exp": expire}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    cred_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise cred_exc
    except JWTError:
        raise cred_exc
    user = _users().get(username)
    if not user:
        raise cred_exc
    return {"username": username, "full_name": user["full_name"]}
