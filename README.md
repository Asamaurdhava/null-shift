# NULL//SHIFT

NULL//SHIFT is a real-time multiplayer sabotage game for 2–8 players. Friends join from phones or laptops with a four-character room code, then compete on the same synchronized 5×5 network: capture nodes, fight for the Reactor, deploy powers, survive global SHIFT events, and complete private objectives before the system collapses.

No accounts or client installation are required.

## Why this build is deployment-safe

The browser never owns authoritative game state. It only sends intents such as `capture`, `power`, `start`, and `rematch`. The server validates them and publishes personalized snapshots back to each player.

Production room state lives in Redis rather than a Python process. That matters on Vercel because containers/functions can scale horizontally and restart independently. Redis provides:

- one authoritative room state shared by every server instance;
- distributed room mutation locks to prevent simultaneous writes from diverging;
- pub/sub notifications so sockets connected to different instances update together;
- reconnect/session replacement state that works across instances;
- automatic room expiry after inactivity.

For local development, the server falls back to a single-process in-memory store. On Vercel, it deliberately refuses that unsafe fallback and requires `REDIS_URL`.

## Gameplay

- Capture neutral or enemy nodes by tapping them. Enemy nodes cost more energy.
- The center Reactor grants extra energy and **3 score per second** while held.
- `SHIELD` protects one owned node for 8 seconds.
- `JAM` disables the current leading opponent for 3 seconds.
- `OVERLOAD` neutralizes an enemy non-Reactor node.
- `CHAOS` immediately forces a random global SHIFT.
- Global SHIFTs include BLACKOUT, OVERCLOCK, CHAIN, and INVERSION.
- During the final 20 seconds, nodes begin collapsing.
- Final score combines live score, active territory, remaining energy, and a secret-objective bonus.

## Local development

Python 3.12+ is recommended.

```bash
python -m pip install -r requirements.txt
python server.py
```

Open `http://localhost:8080` in two browser windows or devices. Without `REDIS_URL`, local development uses the in-memory backend.

To exercise the production storage path locally, point `REDIS_URL` at a Redis-compatible server:

```bash
export REDIS_URL=redis://localhost:6379
python server.py
```

Check backend readiness at:

```text
http://localhost:8080/healthz
```

A production-ready response should report `{"ok": true, "backend": "redis"}`.

## Tests

Install development dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Run deterministic gameplay/state tests:

```bash
pytest -q
```

Run a real two-client WebSocket protocol test while the server is running on port 8765:

```bash
PORT=8765 python server.py
```

In another terminal:

```bash
python tests/integration_ws.py
```

Run the two-server shared-state regression test:

```bash
python tests/integration_multi_instance.py
```

That test emulates two independent application instances sharing one authoritative backend and verifies cross-instance synchronization plus live-session replacement.

Optional browser E2E:

```bash
python -m pip install -r requirements-e2e.txt
playwright install chromium
PORT=8765 python server.py
python tests/e2e_multiplayer.py
```

Set `NULLSHIFT_TEST_URL` to test another deployment. Set `CHROMIUM_EXECUTABLE` only when using a system Chromium instead of Playwright's managed browser.

GitHub Actions runs compilation, Redis dependency import, unit tests, the two-client WebSocket test, and the multi-instance regression test on every push to `main` and on pull requests.

## Deploy on Vercel from GitHub

This repository contains `Dockerfile.vercel`. Vercel can build it directly as a containerized HTTP/WebSocket service; no framework rewrite is required.

1. Push/import this repository into GitHub.
2. In Vercel, choose **Add New → Project** and import the GitHub repository.
3. Add **Redis** from the Vercel Marketplace to that project before production traffic. The Redis integration should expose a `REDIS_URL` environment variable to the deployment.
4. Deploy or redeploy the project. `Dockerfile.vercel` is auto-detected and `server.py` listens on Vercel's `$PORT`.
5. Open `https://YOUR_DEPLOYMENT/healthz` and confirm the backend says `redis`.
6. Open the public game URL on at least two separate devices, create a room on one, join from the other, and run a complete match.

WebSocket connections may be recycled by the hosting platform. The client already uses authenticated rejoin plus exponential reconnect behavior, so the same operator restores into the current authoritative Redis room instead of creating a duplicate player.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `REDIS_URL` | **Yes on Vercel** | Shared room state, locks, and pub/sub |
| `ROOM_TTL_SECONDS` | No | Inactive room lifetime; defaults to `7200` |
| `ALLOWED_ORIGINS` | No | Additional comma-separated WebSocket origins; same-host browser origins are allowed automatically |
| `PORT` | Supplied by host | HTTP/WebSocket listen port |

Never commit a real `.env` file. `.env.example` contains placeholders only.

## Multiplayer architecture

```text
Phone / Laptop A ──┐
Phone / Laptop B ──┼── WebSocket ── Vercel instances
Phone / Laptop C ──┘                    │
                                        │ lock + state + pub/sub
                                        ▼
                                      Redis
```

Each connected server instance keeps only its local socket objects. Everything required to reconstruct gameplay—players, reconnect sessions, nodes, timers, powers, scores, objectives, and phase—is serialized into the shared room record.

A short Redis tick lease ensures only one connected instance advances a room during each server tick. All mutations are then performed under a distributed room lock. Pub/sub wakes other instances, which load the newest authoritative state before delivering personalized snapshots to their locally connected players. Periodic self-healing snapshots cover transient pub/sub misses.

## Security and resilience

- Client messages are capped at 64 KiB.
- Browser WebSockets are same-origin by default; additional trusted origins can be explicitly configured.
- A per-socket action ceiling limits basic message flooding.
- Reconnect tokens are random and never included in public room snapshots.
- Secret objectives and cooldown details are included only in the viewing player's snapshot.
- A stale tab/device cannot disconnect a newer replacement session.
- Unexpected infrastructure exceptions are logged server-side but returned to players as generic synchronization errors.
- Vercel without `REDIS_URL` fails closed instead of silently creating split-brain rooms.

## Repository map

- `game.py` — authoritative rules, scoring, state machine, serialization
- `state_store.py` — memory/Redis state backends, locks, tick leases, pub/sub
- `server.py` — aiohttp HTTP + WebSocket protocol and local socket hub
- `static/index.html` — responsive game UI
- `static/style.css` — cyber visual system
- `static/app.js` — rendering, interactions, timers, reconnect behavior
- `Dockerfile.vercel` — Vercel container build
- `.github/workflows/ci.yml` — regression CI
- `tests/test_game.py` — deterministic gameplay/storage tests
- `tests/integration_ws.py` — real two-client WebSocket protocol test
- `tests/integration_multi_instance.py` — cross-instance synchronization regression test
- `tests/e2e_multiplayer.py` — optional Chromium browser flow
- `DEBUG_REPORT.md` — engineering audit and verification record
