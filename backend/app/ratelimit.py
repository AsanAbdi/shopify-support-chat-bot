"""Redis fixed-window rate limiter."""

from fastapi import HTTPException, Request, status

from app.observability import BLOCKED
from app.security import client_ip


async def allow(redis, bucket: str, ident: str, limit: int, window_s: int = 60) -> bool:
    key = f"rl:{bucket}:{ident}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_s)
    return count <= limit


def http_limit(bucket: str, limit_attr: str):
    """FastAPI dependency factory: `Depends(http_limit('login', 'rate_limit_login_per_min'))`."""

    async def dep(request: Request) -> None:
        c = request.app.state.container
        limit = getattr(c.settings, limit_attr)
        if not await allow(c.redis, bucket, client_ip(request), limit):
            BLOCKED.labels(f"rate_{bucket}").inc()
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests", headers={"Retry-After": "60"})

    return dep
