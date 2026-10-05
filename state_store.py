from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Optional, TypeVar

from game import Room

try:
    import redis.asyncio as redis_async
except ImportError:  # pragma: no cover - only missing in broken installs
    redis_async = None

T = TypeVar("T")
ROOM_TTL_SECONDS = int(os.environ.get("ROOM_TTL_SECONDS", "7200"))
LOCK_SECONDS = int(os.environ.get("ROOM_LOCK_SECONDS", "5"))
TICK_LEASE_MS = int(os.environ.get("ROOM_TICK_LEASE_MS", "180"))


def _dump(room: Room) -> str:
    return json.dumps(room.to_storage(), separators=(",", ":"), ensure_ascii=False)


def _load(raw: str | bytes | None) -> Optional[Room]:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return Room.from_storage(json.loads(raw))


class BaseRoomStore:
    shared = False

    async def close(self) -> None:
        return None

    async def create(self, room: Room) -> bool:
        raise NotImplementedError

    async def get(self, code: str) -> Optional[Room]:
        raise NotImplementedError

    async def mutate(self, code: str, fn: Callable[[Room], T]) -> tuple[Room, T]:
        raise NotImplementedError

    async def claim_tick(self, code: str) -> bool:
        raise NotImplementedError

    async def publish(self, code: str, origin: str) -> None:
        return None

    async def subscribe(self, code: str) -> AsyncIterator[str]:
        if False:
            yield ""  # pragma: no cover
        return


class MemoryRoomStore(BaseRoomStore):
    """Single-process development/test store.

    Production on Vercel intentionally refuses to use this backend because
    containers can scale horizontally and memory is not shared across them.
    """

    def __init__(self):
        self.rooms: dict[str, Room] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.last_tick: dict[str, float] = {}

    def _lock(self, code: str) -> asyncio.Lock:
        return self.locks.setdefault(code, asyncio.Lock())

    async def create(self, room: Room) -> bool:
        async with self._lock(room.code):
            if room.code in self.rooms:
                return False
            self.rooms[room.code] = Room.from_storage(room.to_storage())
            return True

    async def get(self, code: str) -> Optional[Room]:
        async with self._lock(code):
            room = self.rooms.get(code)
            return Room.from_storage(room.to_storage()) if room else None

    async def mutate(self, code: str, fn: Callable[[Room], T]) -> tuple[Room, T]:
        async with self._lock(code):
            room = self.rooms.get(code)
            if room is None:
                raise KeyError(code)
            result = fn(room)
            snapshot = Room.from_storage(room.to_storage())
            return snapshot, result

    async def claim_tick(self, code: str) -> bool:
        now_mono = time.monotonic()
        async with self._lock(f"tick:{code}"):
            previous = self.last_tick.get(code, 0.0)
            if (now_mono - previous) * 1000 < TICK_LEASE_MS:
                return False
            self.last_tick[code] = now_mono
            return True


class RedisRoomStore(BaseRoomStore):
    shared = True

    def __init__(self, url: str):
        if redis_async is None:
            raise RuntimeError("redis package is required when REDIS_URL is configured")
        self.redis = redis_async.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=20,
            socket_connect_timeout=5,
            socket_timeout=5,
        )

    @staticmethod
    def room_key(code: str) -> str:
        return f"nullshift:room:{code}"

    @staticmethod
    def lock_key(code: str) -> str:
        return f"nullshift:lock:{code}"

    @staticmethod
    def tick_key(code: str) -> str:
        return f"nullshift:tick:{code}"

    @staticmethod
    def channel(code: str) -> str:
        return f"nullshift:events:{code}"

    async def close(self) -> None:
        await self.redis.aclose()

    async def ping(self) -> bool:
        return bool(await self.redis.ping())

    async def create(self, room: Room) -> bool:
        return bool(
            await self.redis.set(
                self.room_key(room.code),
                _dump(room),
                nx=True,
                ex=ROOM_TTL_SECONDS,
            )
        )

    async def get(self, code: str) -> Optional[Room]:
        return _load(await self.redis.get(self.room_key(code)))

    @asynccontextmanager
    async def _locked(self, code: str):
        lock = self.redis.lock(
            self.lock_key(code),
            timeout=LOCK_SECONDS,
            blocking_timeout=LOCK_SECONDS,
        )
        acquired = await lock.acquire()
        if not acquired:
            raise RuntimeError("Room is busy; try again")
        try:
            yield
        finally:
            try:
                await lock.release()
            except Exception:
                # The lock can expire if the process is suspended. A later
                # mutation will obtain a fresh lock and authoritative state.
                pass

    async def mutate(self, code: str, fn: Callable[[Room], T]) -> tuple[Room, T]:
        async with self._locked(code):
            key = self.room_key(code)
            room = _load(await self.redis.get(key))
            if room is None:
                raise KeyError(code)
            result = fn(room)
            await self.redis.set(key, _dump(room), ex=ROOM_TTL_SECONDS)
            return room, result

    async def claim_tick(self, code: str) -> bool:
        return bool(
            await self.redis.set(
                self.tick_key(code),
                "1",
                nx=True,
                px=TICK_LEASE_MS,
            )
        )

    async def publish(self, code: str, origin: str) -> None:
        await self.redis.publish(self.channel(code), origin)

    async def subscribe(self, code: str) -> AsyncIterator[str]:
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(self.channel(code))
        try:
            while True:
                message = await pubsub.get_message(timeout=1.0)
                if message and message.get("type") == "message":
                    yield str(message.get("data") or "")
                else:
                    await asyncio.sleep(0.05)
        finally:
            try:
                await pubsub.unsubscribe(self.channel(code))
            finally:
                await pubsub.aclose()


def create_store() -> BaseRoomStore:
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if redis_url:
        return RedisRoomStore(redis_url)

    # Vercel containers are stateless and may scale out. Silently falling back
    # to process memory there would create split-brain multiplayer rooms.
    if os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"):
        raise RuntimeError(
            "REDIS_URL is required on Vercel. Add Redis from the Vercel Marketplace "
            "and redeploy."
        )
    return MemoryRoomStore()
