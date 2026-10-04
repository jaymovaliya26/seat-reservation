import asyncpg
import pytest

from app.db import with_retries


def failing_then_ok(*errors: Exception) -> tuple[list[int], object]:
    calls: list[int] = []

    async def run() -> str:
        calls.append(1)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return "done"

    return calls, run


@pytest.mark.parametrize(
    "error",
    [
        asyncpg.DeadlockDetectedError("deadlock detected"),
        asyncpg.SerializationError("could not serialize access"),
        asyncpg.TooManyConnectionsError("sorry, too many clients already"),
        asyncpg.CannotConnectNowError("the database system is starting up"),
    ],
)
async def test_retries_what_is_safe_to_repeat(error: Exception) -> None:
    calls, run = failing_then_ok(error, error)
    assert await with_retries(run) == "done"  # type: ignore[arg-type]
    assert len(calls) == 3


async def test_gives_up_after_three_attempts() -> None:
    error = asyncpg.TooManyConnectionsError("sorry, too many clients already")
    calls, run = failing_then_ok(error, error, error)
    with pytest.raises(asyncpg.TooManyConnectionsError):
        await with_retries(run)  # type: ignore[arg-type]
    assert len(calls) == 3


async def test_does_not_retry_other_errors() -> None:
    calls, run = failing_then_ok(asyncpg.UniqueViolationError("duplicate key"))
    with pytest.raises(asyncpg.UniqueViolationError):
        await with_retries(run)  # type: ignore[arg-type]
    assert len(calls) == 1
