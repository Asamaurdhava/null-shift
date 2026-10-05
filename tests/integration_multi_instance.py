import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import ClientSession, web

from server import make_app
from state_store import MemoryRoomStore


class SharedMemoryStore(MemoryRoomStore):
    """Test-only shared backend that emulates Redis pub/sub across two app instances."""

    shared = True

    def __init__(self):
        super().__init__()
        self.subscribers: dict[str, set[asyncio.Queue[str]]] = {}

    async def publish(self, code: str, origin: str) -> None:
        for queue in list(self.subscribers.get(code, set())):
            queue.put_nowait(origin)

    async def subscribe(self, code: str):
        queue: asyncio.Queue[str] = asyncio.Queue()
        self.subscribers.setdefault(code, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            subscribers = self.subscribers.get(code)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self.subscribers.pop(code, None)


async def recv_until(ws, predicate, timeout=8):
    async def inner():
        while True:
            msg = await ws.receive()
            if msg.type.name in {"CLOSE", "CLOSED", "CLOSING"}:
                raise AssertionError(f"socket closed early: {msg.type} {msg.data}")
            if msg.type.name != "TEXT":
                continue
            data = json.loads(msg.data)
            if predicate(data):
                return data

    return await asyncio.wait_for(inner(), timeout)


async def start_site(store):
    app = make_app(store)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def main():
    store = SharedMemoryStore()
    runner_a, url_a = await start_site(store)
    runner_b, url_b = await start_site(store)

    try:
        async with ClientSession() as session:
            a = await session.ws_connect(f"{url_a}/ws")
            b = await session.ws_connect(f"{url_b}/ws")

            await a.send_json({"type": "create_room", "name": "Alpha"})
            ident_a = await recv_until(a, lambda m: m.get("type") == "identity")
            code = ident_a["code"]
            await recv_until(a, lambda m: m.get("type") == "state")

            await b.send_json({"type": "join_room", "code": code, "name": "Bravo"})
            ident_b = await recv_until(b, lambda m: m.get("type") == "identity")
            await recv_until(b, lambda m: m.get("type") == "state" and len(m["state"]["players"]) == 2)
            await recv_until(a, lambda m: m.get("type") == "state" and len(m["state"]["players"]) == 2)

            await a.send_json({"type": "start"})
            await recv_until(a, lambda m: m.get("type") == "state" and m["state"]["phase"] == "COUNTDOWN")
            await recv_until(b, lambda m: m.get("type") == "state" and m["state"]["phase"] == "COUNTDOWN")
            await recv_until(a, lambda m: m.get("type") == "state" and m["state"]["phase"] == "MATCH", timeout=7)
            await recv_until(b, lambda m: m.get("type") == "state" and m["state"]["phase"] == "MATCH", timeout=7)

            await b.send_json({"type": "capture", "nodeId": 0})
            await recv_until(b, lambda m: m.get("type") == "state" and m["state"]["nodes"][0]["owner"] == ident_b["playerId"])
            await recv_until(a, lambda m: m.get("type") == "state" and m["state"]["nodes"][0]["owner"] == ident_b["playerId"])

            # Rejoin through the other server instance. The original connection
            # must be invalidated even though it lives on a different hub.
            b2 = await session.ws_connect(f"{url_a}/ws")
            await b2.send_json({
                "type": "rejoin",
                "code": code,
                "playerId": ident_b["playerId"],
                "token": ident_b["token"],
            })
            await recv_until(b2, lambda m: m.get("type") == "identity")
            state_b2 = (await recv_until(b2, lambda m: m.get("type") == "state"))["state"]
            bravo = next(p for p in state_b2["players"] if p["id"] == ident_b["playerId"])
            assert bravo["connected"] is True

            async def wait_closed():
                while True:
                    msg = await b.receive()
                    if msg.type.name in {"CLOSE", "CLOSED", "CLOSING"}:
                        return msg

            closed = await asyncio.wait_for(wait_closed(), 5)
            if closed.type.name == "CLOSE":
                assert closed.data == 4001

            await a.close()
            await b2.close()
            print("MULTI-INSTANCE INTEGRATION PASS", code)
    finally:
        await runner_a.cleanup()
        await runner_b.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
