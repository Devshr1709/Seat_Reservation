import asyncio
import random
from contextlib import asynccontextmanager

import asyncpg

from .config import DSN, POOL_MAX
from .logging_utils import L

SCHEMA = """
create table if not exists shows(
  id uuid primary key, name text not null, price_paise bigint not null check (price_paise >= 0),
  per_user_limit int not null, total_seats int not null, created_at timestamptz default now());
create table if not exists seats(
  show_id uuid not null references shows(id), seat_id text not null,
  status text not null default 'available' check (status in ('available','held','confirmed')),
  user_id text, reservation_id uuid,
  primary key (show_id, seat_id));
create index if not exists seats_user on seats(show_id, user_id) where user_id is not null;
create index if not exists seats_resv on seats(reservation_id) where reservation_id is not null;
create table if not exists reservations(
  id uuid primary key, show_id uuid not null, user_id text not null, seats text[] not null,
  amount_paise bigint not null, status text not null, created_at timestamptz default now());
create table if not exists idempotency(
  user_id text not null, show_id uuid not null, key text not null,
  request_hash text not null, reservation_id uuid not null,
  primary key (user_id, show_id, key));
"""
POOL = None
READY = False
INIT_LOCK = asyncio.Lock()


class PoolAcquireTimeout(Exception):
    """Raised when a request cannot get a DB connection within its wait budget."""


@asynccontextmanager
async def acquire(timeout=50):
    """Acquire a pooled connection and distinguish pool saturation from query errors."""
    await ensure_db()
    context = POOL.acquire(timeout=timeout)
    try:
        connection = await context.__aenter__()
    except asyncio.TimeoutError as e:
        raise PoolAcquireTimeout from e
    try:
        yield connection
    finally:
        await context.__aexit__(None, None, None)


async def ensure_db():
    global POOL, READY
    if READY:
        return
    async with INIT_LOCK:
        if READY:
            return
        pool = await asyncpg.create_pool(DSN, min_size=2, max_size=POOL_MAX, timeout=5, command_timeout=60)
        async with pool.acquire() as c:
            await c.execute("select pg_advisory_lock(42)")
            try:
                await c.execute(SCHEMA)
            finally:
                await c.execute("select pg_advisory_unlock(42)")
        POOL, READY = pool, True


async def tx(fn, tries=6):
    """Run fn(conn) transactionally, retrying deadlock/serialization failures."""
    for i in range(tries):
        try:
            async with acquire() as c:
                async with c.transaction():
                    return await fn(c)
        except (asyncpg.DeadlockDetectedError, asyncpg.SerializationError):
            if i == tries - 1:
                raise
            await asyncio.sleep(random.uniform(0.005, 0.05) * (i + 1))
