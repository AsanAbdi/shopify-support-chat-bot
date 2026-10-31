import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import User

_bearer = HTTPBearer(auto_error=False)
_ALG = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


# A hash to compare against when the user does not exist, so timing does not reveal valid usernames.
_DUMMY_HASH = hash_password("not-a-real-password")


def check_credentials(user: User | None, password: str) -> bool:
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    return bool(user) and user.is_active and ok


def create_token(user: User) -> str:
    s = get_settings()
    exp = datetime.now(UTC) + timedelta(minutes=s.jwt_ttl_minutes)
    return jwt.encode({"sub": str(user.id), "role": user.role, "exp": exp}, s.jwt_secret.get_secret_value(), algorithm=_ALG)


async def current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    try:
        payload = jwt.decode(creds.credentials, get_settings().jwt_secret.get_secret_value(), algorithms=[_ALG])
    except jwt.PyJWTError:
        raise unauthorized from None
    user = (await session.execute(select(User).where(User.id == int(payload["sub"])))).scalar_one_or_none()
    if user is None or not user.is_active:  # deactivation takes effect immediately, not at token expiry
        raise unauthorized
    return user


async def require_admin(user: Annotated[User, Depends(current_user)]) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def ip_hash(ip: str) -> str:
    return hashlib.sha256((ip + get_settings().jwt_secret.get_secret_value()).encode()).hexdigest()[:32]
