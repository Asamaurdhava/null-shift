# NULL//SHIFT Engineering Audit

Audit date: 2026-10-04 (America/Phoenix)

## Current status

NULL//SHIFT has been migrated from a single-process multiplayer prototype to a Vercel-oriented, horizontally safe architecture while preserving the existing authoritative game engine and browser UI.

### Verification completed in this environment

- Python compilation (`game.py`, `state_store.py`, `server.py`): **PASS**
- JavaScript syntax (`node --check static/app.js`): **PASS**
- Deterministic rule/state tests: **13/13 PASS**
- Real two-client WebSocket integration: **PASS**
- Two-server shared-state synchronization regression: **PASS**
- Cross-instance live-session replacement: **PASS**
- HTTP health endpoint: **PASS** using local memory backend
- Existing browser E2E source remains available; full Chromium execution is environment-dependent

The execution sandbox used for this audit has no outbound package/network access, so it could not install `redis-py` or connect to an external Redis Cloud instance. The production dependency is declared in `requirements.txt`, GitHub CI explicitly imports `redis.asyncio`, and the cross-instance application behavior is exercised through a shared-store emulator. A final deployed smoke test against the actual Vercel Redis resource is still required after the resource is attached.

## Production migration fixes

1. **Removed process-local authoritative room state from production.** Rooms can now be stored in Redis and reconstructed on any instance.
2. **Added distributed room locking.** Simultaneous actions arriving at different server instances serialize through one room lock instead of racing independent Python objects.
3. **Added room pub/sub.** A mutation on one instance wakes sockets attached to other instances, which reload the newest authoritative snapshot.
4. **Added a distributed tick lease.** Multiple connected instances cannot independently advance timers, Reactor income, SHIFTs, or final-collapse events during the same tick window.
5. **Added cross-instance session IDs.** A rejoin from another device/instance invalidates the old live session without letting the old connection mark the new one offline.
6. **Added periodic self-healing snapshots.** Temporary pub/sub interruption cannot leave a lobby/results screen permanently stale.
7. **Added Redis-backed room TTL.** Abandoned rooms expire automatically instead of leaking server memory indefinitely.
8. **Added Vercel fail-closed behavior.** If Vercel starts without `REDIS_URL`, the app refuses the unsafe in-memory multiplayer fallback.
9. **Added `Dockerfile.vercel`.** Vercel can build the existing Python/aiohttp HTTP/WebSocket server directly.
10. **Added `/healthz`.** Deployment readiness exposes whether the live backend is Redis or local memory.
11. **Added same-origin WebSocket protection.** Browser connections from unrelated origins are rejected unless explicitly allow-listed.
12. **Added per-socket action throttling.** Obvious message floods are cut off without affecting normal gameplay.
13. **Sanitized infrastructure failures.** Redis/network implementation details are logged server-side rather than sent verbatim to players.
14. **Added GitHub Actions CI.** Every push/PR compiles Python, imports the production Redis dependency, runs deterministic tests, runs a real WebSocket test, and runs the multi-instance regression.
15. **Removed stale Render-specific deployment configuration** from the Vercel baseline.

## Previously fixed gameplay/protocol defects retained in this build

- Reactor passive scoring truncation at sub-second ticks
- Active-match rematch exploit
- Multi-room socket/ghost-player binding
- Simultaneous rejoin race
- Same-session reconnect ping-pong
- Stale-room browser identity after expiry/restart
- Per-recipient broadcast timestamp drift
- Closed socket cleanup
- INVERSION + SHIELD ownership mismatch
- Shield metadata surviving node collapse
- Stale rematch timing/objective data
- BLACKOUT owner-name leakage
- Incorrect connected-player lobby count
- Repeated scoreboard node scans
- Recreated power constants in hot render paths
- Non-cryptographic room-code generation
- Falsey timestamp injection bugs
- Pytest import-path portability
- Invalid Playwright class matchers
- Hard-coded Chromium executable path

## Deployment acceptance checklist

Before calling the public challenge build production-ready:

1. Import the GitHub repository into Vercel.
2. Attach Vercel Marketplace Redis and verify `REDIS_URL` exists in Production.
3. Deploy and confirm `/healthz` returns `ok: true` and `backend: redis`.
4. Run `tests/integration_ws.py` against the public URL via `NULLSHIFT_TEST_URL=https://...`.
5. Complete one match with at least two physical devices on different networks if possible.
6. Refresh/reconnect one device mid-match and confirm the same operator returns.
7. Open the same operator in a second tab/device and confirm the older session is replaced rather than duplicated.
8. Confirm GitHub Actions is green for the deployed commit.
