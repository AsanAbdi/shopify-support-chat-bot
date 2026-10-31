from app.services.turn_lock import LOCK_TTL_S, TurnLock


async def test_the_first_turn_gets_the_lock_and_a_second_one_for_the_same_conversation_does_not(redis):
    first, second = TurnLock(redis, "conv-1"), TurnLock(redis, "conv-1")

    assert await first.acquire() is True
    assert await second.acquire() is False


async def test_other_conversations_are_independent(redis):
    assert await TurnLock(redis, "conv-1").acquire()
    assert await TurnLock(redis, "conv-2").acquire()


async def test_releasing_makes_the_conversation_available_again(redis):
    lock = TurnLock(redis, "conv-1")
    await lock.acquire()
    await lock.release()

    assert await TurnLock(redis, "conv-1").acquire() is True


async def test_the_lock_expires_by_itself_so_a_crashed_turn_cannot_block_a_conversation_forever(redis):
    await TurnLock(redis, "conv-1").acquire()

    ttl = await redis.ttl("chat-lock:conv-1")

    assert 0 < ttl <= LOCK_TTL_S == 120


async def test_releasing_a_lock_that_is_not_held_is_harmless(redis):
    await TurnLock(redis, "never-taken").release()
