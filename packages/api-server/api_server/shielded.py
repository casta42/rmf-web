"""F-138 (E6 run-2 blocker 2): cancellation-safe execution for DB work
driven by cancellable tasks (websocket handlers, request handlers).

A coroutine cancelled inside an open transaction never rolls back — the
connection returns to the pool 'idle in transaction' and is lost until
the pool starves (observed live: 5 leaked connections, oldest 30 h,
every DB endpoint hanging with no operator-visible failure). Running
the work through shielded() guarantees the inner operation completes
(commit or rollback) even when the caller is cancelled; the
cancellation is then re-raised so the caller still dies promptly.
"""
import asyncio
from typing import Any, Coroutine


async def shielded(coro: Coroutine) -> Any:
    """Await `coro` so that outer cancellation never aborts it mid-way:
    on cancel, the inner task runs to completion first, then the
    CancelledError propagates."""
    op = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(op)
    except asyncio.CancelledError:
        await op
        raise
