LOCK_TTL_S = 120


class TurnLock:
    """One turn per conversation at a time: two tabs or a reconnect must not interleave the agent's memory.

    The key expires on its own, so a crashed worker cannot lock a conversation forever. It is a plain
    "set if absent" lock without an owner token; a turn that outlives the TTL (the model call times out at 30 s,
    so this does not happen in practice) would let a second turn in.
    """

    def __init__(self, redis, conv_id: str) -> None:
        self._redis = redis
        self._key = f"chat-lock:{conv_id}"

    async def acquire(self) -> bool:
        return bool(await self._redis.set(self._key, "1", nx=True, ex=LOCK_TTL_S))

    async def release(self) -> None:
        await self._redis.delete(self._key)
