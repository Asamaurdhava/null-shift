from __future__ import annotations

import asyncio
import json
import os
import secrets
import traceback
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from game import Room, now, room_code
from state_store import BaseRoomStore, RedisRoomStore, create_store

MAX_MESSAGE_BYTES = 64 * 1024
TICK_SECONDS = 0.25


@dataclass
class Connection:
    ws: web.WebSocketResponse
    pid: str
    session_id: str


class RealtimeHub:
    def __init__(self, store: BaseRoomStore):
        self.store = store
        self.origin_id = secrets.token_hex(6)
        self.sockets: dict[str, dict[str, Connection]] = {}
        self.subscriptions: dict[str, asyncio.Task] = {}

    async def register(self, code: str, connection: Connection) -> None:
        room_sockets = self.sockets.setdefault(code, {})
        old = room_sockets.get(connection.pid)
        room_sockets[connection.pid] = connection
        if old and old.ws is not connection.ws and not old.ws.closed:
            await old.ws.close(code=4001, message=b"replaced")
        if self.store.shared and code not in self.subscriptions:
            self.subscriptions[code] = asyncio.create_task(self._subscription_loop(code))

    async def unregister(self, code: str, pid: str, ws: web.WebSocketResponse) -> None:
        room_sockets = self.sockets.get(code)
        if not room_sockets:
            return
        current = room_sockets.get(pid)
        if current and current.ws is ws:
            room_sockets.pop(pid, None)
        if not room_sockets:
            self.sockets.pop(code, None)
            task = self.subscriptions.pop(code, None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _subscription_loop(self, code: str) -> None:
        try:
            while code in self.sockets:
                try:
                    async for origin in self.store.subscribe(code):
                        if origin == self.origin_id:
                            continue
                        await self.broadcast_local(code)
                        if code not in self.sockets:
                            return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    traceback.print_exc()
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    async def notify(self, code: str) -> None:
        # Local delivery is immediate. Redis pub/sub wakes peers on any other
        # container instance so all connected devices converge on the same room.
        await self.broadcast_local(code)
        if self.store.shared:
            await self.store.publish(code, self.origin_id)

    async def broadcast_local(self, code: str) -> None:
        room_sockets = self.sockets.get(code)
        if not room_sockets:
            return
        room = await self.store.get(code)
        if room is None:
            for connection in list(room_sockets.values()):
                await safe_send(connection.ws, {"type": "error", "message": "Room expired"})
                await connection.ws.close(code=4004, message=b"room expired")
            return

        timestamp = now()
        stale: list[Connection] = []
        deliveries = []
        for connection in list(room_sockets.values()):
            player = room.players.get(connection.pid)
            if not player or player.session_id != connection.session_id:
                stale.append(connection)
                continue
            deliveries.append(
                safe_send(
                    connection.ws,
                    {"type": "state", "state": room.public_state(connection.pid, timestamp)},
                )
            )
        if deliveries:
            await asyncio.gather(*deliveries, return_exceptions=True)
        for connection in stale:
            if not connection.ws.closed:
                await connection.ws.close(code=4001, message=b"replaced")


async def safe_send(ws: web.WebSocketResponse, payload: dict) -> None:
    if not ws.closed:
        await ws.send_str(json.dumps(payload, separators=(",", ":")))


async def allocate_room(store: BaseRoomStore) -> Room:
    for _ in range(100):
        room = Room(room_code())
        if await store.create(room):
            return room
    raise RuntimeError("Could not allocate room code")


def normalize_code(value: object) -> str:
    return str(value or "").upper().strip()[:4]


def origin_allowed(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True
    configured = {
        item.strip().rstrip("/")
        for item in os.environ.get("ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    }
    if origin.rstrip("/") in configured:
        return True
    try:
        return urlparse(origin).netloc.lower() == request.host.lower()
    except ValueError:
        return False


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    if not origin_allowed(request):
        raise web.HTTPForbidden(text="WebSocket origin not allowed")
    store: BaseRoomStore = request.app["store"]
    hub: RealtimeHub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_MESSAGE_BYTES)
    await ws.prepare(request)

    code: str | None = None
    pid: str | None = None
    session_id = secrets.token_urlsafe(16)
    ticker_task: asyncio.Task | None = None
    message_times: deque[float] = deque()

    async def joined_room(new_code: str, new_pid: str, token: str) -> None:
        nonlocal code, pid, ticker_task
        code, pid = new_code, new_pid
        await hub.register(code, Connection(ws=ws, pid=pid, session_id=session_id))
        await safe_send(ws, {"type": "identity", "code": code, "playerId": pid, "token": token})
        await hub.notify(code)
        ticker_task = asyncio.create_task(connection_ticker(store, hub, code, pid, session_id, ws))

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            stamp = time.monotonic()
            while message_times and stamp - message_times[0] > 5.0:
                message_times.popleft()
            if len(message_times) >= 60:
                await safe_send(ws, {"type": "error", "message": "Too many actions; slow down"})
                await ws.close(code=4008, message=b"rate limited")
                break
            message_times.append(stamp)
            try:
                data = json.loads(msg.data)
                if not isinstance(data, dict):
                    raise ValueError("Message must be a JSON object")
                typ = data.get("type")

                if typ == "create_room":
                    if code or pid:
                        raise ValueError("Socket already joined a room")
                    room = await allocate_room(store)
                    room, player = await store.mutate(
                        room.code,
                        lambda current: current.add_player(data.get("name", "Operator"), session_id),
                    )
                    await joined_room(room.code, player.id, player.token)

                elif typ == "join_room":
                    if code or pid:
                        raise ValueError("Socket already joined a room")
                    target = normalize_code(data.get("code"))
                    try:
                        room, player = await store.mutate(
                            target,
                            lambda current: current.add_player(data.get("name", "Operator"), session_id),
                        )
                    except KeyError:
                        raise ValueError("Room not found") from None
                    await joined_room(target, player.id, player.token)

                elif typ == "rejoin":
                    if code or pid:
                        raise ValueError("Socket already joined a room")
                    target = normalize_code(data.get("code"))
                    player_id = str(data.get("playerId", ""))
                    token = str(data.get("token", ""))
                    try:
                        room, player = await store.mutate(
                            target,
                            lambda current: current.reconnect(player_id, token, session_id),
                        )
                    except KeyError:
                        raise ValueError("Room expired") from None
                    await joined_room(target, player.id, player.token)

                elif typ == "ping":
                    await safe_send(ws, {"type": "pong"})

                else:
                    if not code or not pid:
                        raise ValueError("Join a room first")

                    def action(room: Room):
                        player = room.players.get(pid)
                        if not player or player.session_id != session_id:
                            raise ValueError("Session was replaced")
                        if typ == "start":
                            return room.start(pid)
                        if typ == "capture":
                            return room.capture(pid, int(data.get("nodeId", -1)))
                        if typ == "power":
                            node_id = data.get("nodeId")
                            return room.use_power(
                                pid,
                                str(data.get("power", "")),
                                int(node_id) if node_id is not None else None,
                            )
                        if typ == "rematch":
                            return room.rematch(pid)
                        raise ValueError("Unknown message type")

                    try:
                        await store.mutate(code, action)
                    except KeyError:
                        raise ValueError("Room expired") from None
                    await hub.notify(code)

            except (ValueError, RuntimeError) as exc:
                await safe_send(ws, {"type": "error", "message": str(exc)[:160]})
            except Exception:
                traceback.print_exc()
                await safe_send(ws, {"type": "error", "message": "Server synchronization error"})
    except Exception:
        traceback.print_exc()
    finally:
        if ticker_task:
            ticker_task.cancel()
            try:
                await ticker_task
            except asyncio.CancelledError:
                pass
        if code and pid:
            await hub.unregister(code, pid, ws)
            try:
                def mark_disconnected(room: Room):
                    room.disconnect(pid, session_id)
                await store.mutate(code, mark_disconnected)
                await hub.notify(code)
            except KeyError:
                pass
            except Exception:
                traceback.print_exc()
    return ws


async def connection_ticker(
    store: BaseRoomStore,
    hub: RealtimeHub,
    code: str,
    pid: str,
    session_id: str,
    ws: web.WebSocketResponse,
) -> None:
    last_resync = 0.0
    try:
        while not ws.closed:
            await asyncio.sleep(TICK_SECONDS)
            room = await store.get(code)
            if room is None:
                await safe_send(ws, {"type": "error", "message": "Room expired"})
                await ws.close(code=4004, message=b"room expired")
                return
            player = room.players.get(pid)
            if not player or player.session_id != session_id:
                await ws.close(code=4001, message=b"replaced")
                return
            if room.phase not in ("COUNTDOWN", "MATCH"):
                # Pub/sub is best-effort. A lightweight periodic snapshot makes
                # lobby/results state self-healing after transient Redis or
                # subscription interruptions without generating cross-instance
                # write traffic.
                current = now()
                if current - last_resync >= 2.0:
                    last_resync = current
                    await safe_send(ws, {"type": "state", "state": room.public_state(pid, current)})
                continue
            if not await store.claim_tick(code):
                continue
            try:
                await store.mutate(code, lambda current: current.tick())
            except KeyError:
                return
            await hub.notify(code)
    except asyncio.CancelledError:
        pass
    except Exception:
        traceback.print_exc()


async def index_handler(request: web.Request):
    return web.FileResponse(os.path.join(request.app["static_dir"], "index.html"))


async def health_handler(request: web.Request):
    store: BaseRoomStore = request.app["store"]
    backend = "redis" if store.shared else "memory"
    if isinstance(store, RedisRoomStore):
        try:
            await store.ping()
        except Exception:
            traceback.print_exc()
            return web.json_response({"ok": False, "backend": backend, "error": "Redis unavailable"}, status=503)
    return web.json_response({"ok": True, "backend": backend})


async def on_cleanup(app: web.Application):
    hub: RealtimeHub = app["hub"]
    for task in list(hub.subscriptions.values()):
        task.cancel()
    if hub.subscriptions:
        await asyncio.gather(*hub.subscriptions.values(), return_exceptions=True)
    await app["store"].close()


def make_app(store: BaseRoomStore | None = None) -> web.Application:
    app = web.Application()
    app["store"] = store or create_store()
    app["hub"] = RealtimeHub(app["store"])
    app["static_dir"] = os.path.join(os.path.dirname(__file__), "static")
    app.router.add_get("/healthz", health_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/", index_handler)
    app.router.add_static("/", app["static_dir"], show_index=False)
    app.on_cleanup.append(on_cleanup)
    return app


app = make_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    web.run_app(app, host="0.0.0.0", port=port)
