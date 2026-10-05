from __future__ import annotations

import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

COLORS = ["cyan", "magenta", "lime", "gold", "orange", "violet", "ice", "crimson"]
MAX_PLAYERS = 8
GRID = 5
REACTOR_ID = 12
SHIFT_INTERVAL = 25.0
SHIFT_DURATION = 10.0
MATCH_SECONDS = 120.0
FINAL_SECONDS = 20.0
ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
POWER_SPECS = {
    "shield": (18, 14),
    "jam": (22, 18),
    "overload": (25, 16),
    "chaos": (30, 24),
}
OBJECTIVES = (
    ("REACTOR", "Own the Reactor when the system collapses", 250),
    ("CORNERS", "Control at least 3 corner nodes at collapse", 250),
    ("SEVEN", "Finish with exactly 7 active nodes", 300),
    ("EDGE", "Control at least 5 edge nodes at collapse", 250),
    ("ENERGY", "Finish with at least 70 energy", 220),
    ("DOMINATE", "Control at least 9 active nodes", 280),
    ("SURVIVOR", "Own at least 4 nodes during final collapse", 220),
    ("CENTER_RING", "Control 3+ nodes adjacent to the Reactor", 260),
)


def now() -> float:
    return time.time()


def room_code(length: int = 4) -> str:
    return "".join(secrets.choice(ROOM_ALPHABET) for _ in range(length))


def clean_name(name: str) -> str:
    cleaned = "".join(ch for ch in (name or "").strip() if ch.isalnum() or ch in " _-")[:16]
    return cleaned or "Operator"


def neighbors(node_id: int) -> List[int]:
    r, c = divmod(node_id, GRID)
    out = []
    for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        rr, cc = r + dr, c + dc
        if 0 <= rr < GRID and 0 <= cc < GRID:
            out.append(rr * GRID + cc)
    return out


@dataclass
class Player:
    id: str
    name: str
    color: str
    token: str
    energy: float = 70.0
    score: float = 0.0
    connected: bool = True
    jammed_until: float = 0.0
    cooldowns: Dict[str, float] = field(default_factory=lambda: {"shield": 0, "jam": 0, "overload": 0, "chaos": 0})
    secret: Dict = field(default_factory=dict)
    session_id: str = ""


@dataclass
class Node:
    id: int
    owner: Optional[str] = None
    shielded_by: Optional[str] = None
    shield_until: float = 0.0
    collapsed: bool = False


class Room:
    def __init__(self, code: str):
        self.code = code
        self.players: Dict[str, Player] = {}
        self.host_id: Optional[str] = None
        self.nodes = [Node(i) for i in range(GRID * GRID)]
        self.phase = "LOBBY"
        self.created_at = now()
        self.countdown_ends_at = 0.0
        self.started_at = 0.0
        self.ends_at = 0.0
        self.next_shift_at = 0.0
        self.shift: Optional[Dict] = None
        self.final_collapse = False
        self.last_collapse_at = 0.0
        self.last_tick = now()
        self.results: List[Dict] = []
        self.feed: List[Dict] = []

    def log(self, text: str, kind: str = "info"):
        self.feed.append({"id": secrets.token_hex(4), "text": text, "kind": kind, "at": int(now() * 1000)})
        self.feed = self.feed[-8:]

    def add_player(self, name: str, session_id: str = "") -> Player:
        if self.phase != "LOBBY":
            raise ValueError("Match already started")
        if len(self.players) >= MAX_PLAYERS:
            raise ValueError("Room is full")
        pid = secrets.token_hex(4)
        used = {p.color for p in self.players.values()}
        color = next(c for c in COLORS if c not in used)
        player = Player(pid, clean_name(name), color, secrets.token_urlsafe(18), session_id=session_id)
        self.players[pid] = player
        if not self.host_id:
            self.host_id = pid
        self.log(f"{player.name} connected", "join")
        return player

    def reconnect(self, pid: str, token: str, session_id: str = "") -> Player:
        p = self.players.get(pid)
        if not p or p.token != token:
            raise ValueError("Invalid reconnect token")
        p.connected = True
        p.session_id = session_id
        self.log(f"{p.name} reconnected", "join")
        return p

    def disconnect(self, pid: str, session_id: str = ""):
        p = self.players.get(pid)
        if not p:
            return
        if session_id and p.session_id and p.session_id != session_id:
            return
        p.connected = False
        p.session_id = ""
        if self.host_id == pid:
            connected = [x.id for x in self.players.values() if x.connected]
            if connected:
                self.host_id = connected[0]
                self.log(f"{self.players[self.host_id].name} is now host", "info")

    def start(self, pid: str):
        if pid != self.host_id:
            raise ValueError("Only the host can start")
        if self.phase != "LOBBY":
            raise ValueError("Game is not in lobby")
        if len([p for p in self.players.values() if p.connected]) < 2:
            raise ValueError("Need at least 2 players")
        self._reset_match_data()
        self._assign_secrets()
        self.phase = "COUNTDOWN"
        self.countdown_ends_at = now() + 4.0
        self.log("NULL handshake accepted", "system")

    def _reset_match_data(self):
        self.nodes = [Node(i) for i in range(GRID * GRID)]
        self.results = []
        self.final_collapse = False
        self.shift = None
        self.feed = []
        self.countdown_ends_at = 0.0
        self.started_at = 0.0
        self.ends_at = 0.0
        self.next_shift_at = 0.0
        self.last_collapse_at = 0.0
        self.last_tick = now()
        for p in self.players.values():
            p.energy = 70.0
            p.score = 0.0
            p.jammed_until = 0
            p.cooldowns = {"shield": 0, "jam": 0, "overload": 0, "chaos": 0}
            p.secret = {}

    def _assign_secrets(self):
        shuffled = list(OBJECTIVES)
        random.shuffle(shuffled)
        for idx, p in enumerate(self.players.values()):
            key, text, bonus = shuffled[idx % len(shuffled)]
            p.secret = {"key": key, "text": text, "bonus": bonus}

    def begin_match_if_due(self, t: float):
        if self.phase == "COUNTDOWN" and t >= self.countdown_ends_at:
            self.phase = "MATCH"
            self.started_at = t
            self.ends_at = t + MATCH_SECONDS
            self.next_shift_at = t + SHIFT_INTERVAL
            self.last_tick = t
            self.log("SIGNAL LIVE — capture the grid", "system")

    def tick(self, t: Optional[float] = None):
        t = now() if t is None else t
        self.begin_match_if_due(t)
        if self.phase != "MATCH":
            return
        dt = max(0.0, min(t - self.last_tick, 2.0))
        self.last_tick = t

        # Passive energy + reactor bonus.
        reactor_owner = self.nodes[REACTOR_ID].owner if not self.nodes[REACTOR_ID].collapsed else None
        overclock = self.shift and self.shift.get("type") == "OVERCLOCK" and t < self.shift.get("until", 0)
        for p in self.players.values():
            if not p.connected:
                continue
            regen = 1.6 * (2 if overclock else 1)
            if reactor_owner == p.id:
                regen += 2.4
                # Keep fractional score internally so a sub-second server tick
                # still accumulates the intended 3 points/second.
                p.score += 3.0 * dt
            p.energy = min(100.0, p.energy + regen * dt)

        if self.shift and t >= self.shift.get("until", 0):
            self.log(f"{self.shift['type']} ended", "shift")
            self.shift = None

        if t >= self.next_shift_at and t < self.ends_at - FINAL_SECONDS:
            self.trigger_shift(t=t)
            self.next_shift_at = t + SHIFT_INTERVAL

        if not self.final_collapse and t >= self.ends_at - FINAL_SECONDS:
            self.final_collapse = True
            self.last_collapse_at = t
            self.log("SYSTEM COLLAPSE — final 20 seconds", "danger")

        if self.final_collapse and t - self.last_collapse_at >= 3.0:
            self.last_collapse_at = t
            candidates = [n for n in self.nodes if not n.collapsed and n.id != REACTOR_ID]
            if candidates:
                n = random.choice(candidates)
                n.collapsed = True
                n.owner = None
                n.shielded_by = None
                n.shield_until = 0.0
                self.log(f"Node {n.id + 1:02d} collapsed", "danger")

        if t >= self.ends_at:
            self.finish()

    def _require_player_action(self, pid: str, t: float) -> Player:
        if self.phase != "MATCH":
            raise ValueError("Match is not active")
        p = self.players.get(pid)
        if not p or not p.connected:
            raise ValueError("Player unavailable")
        if t < p.jammed_until:
            raise ValueError("Controls are jammed")
        return p

    def capture(self, pid: str, node_id: int, t: Optional[float] = None):
        t = now() if t is None else t
        p = self._require_player_action(pid, t)
        if not 0 <= node_id < len(self.nodes):
            raise ValueError("Invalid node")
        n = self.nodes[node_id]
        if n.collapsed:
            raise ValueError("Node has collapsed")
        if n.owner == pid:
            raise ValueError("You already control this node")
        if n.shield_until > t and n.shielded_by != pid:
            raise ValueError("Node is shielded")
        overclock = self.shift and self.shift.get("type") == "OVERCLOCK" and t < self.shift.get("until", 0)
        cost = (8 if n.owner is None else 12) - (3 if overclock else 0)
        if p.energy < cost:
            raise ValueError("Not enough energy")
        previous = n.owner
        p.energy -= cost
        n.owner = pid
        n.shielded_by = None
        n.shield_until = 0
        p.score += 18 if previous else 12
        self.log(f"{p.name} captured node {node_id + 1:02d}", "capture")

        chain = self.shift and self.shift.get("type") == "CHAIN" and t < self.shift.get("until", 0)
        if chain:
            chain_targets = [self.nodes[i] for i in neighbors(node_id) if not self.nodes[i].collapsed and self.nodes[i].owner is None]
            if chain_targets:
                target = random.choice(chain_targets)
                target.owner = pid
                p.score += 8
                self.log(f"Chain reaction seized node {target.id + 1:02d}", "shift")

    def use_power(self, pid: str, power: str, node_id: Optional[int] = None, t: Optional[float] = None):
        t = now() if t is None else t
        p = self._require_player_action(pid, t)
        power = power.lower()
        if power not in POWER_SPECS:
            raise ValueError("Unknown power")
        cost, cooldown = POWER_SPECS[power]
        if p.cooldowns.get(power, 0) > t:
            raise ValueError("Power is cooling down")
        if p.energy < cost:
            raise ValueError("Not enough energy")

        if power == "shield":
            if node_id is None or not 0 <= node_id < len(self.nodes):
                raise ValueError("Select one of your nodes")
            n = self.nodes[node_id]
            if n.owner != pid or n.collapsed:
                raise ValueError("Shield requires one of your active nodes")
            n.shielded_by = pid
            n.shield_until = t + 8
            self.log(f"{p.name} shielded node {node_id + 1:02d}", "power")

        elif power == "overload":
            if node_id is None or not 0 <= node_id < len(self.nodes):
                raise ValueError("Select an enemy node")
            n = self.nodes[node_id]
            if n.owner in (None, pid) or n.collapsed or n.id == REACTOR_ID:
                raise ValueError("Overload requires an enemy non-Reactor node")
            if n.shield_until > t:
                raise ValueError("Target is shielded")
            victim = self.players.get(n.owner)
            n.owner = None
            if victim:
                victim.score = max(0, victim.score - 8)
            self.log(f"{p.name} overloaded node {node_id + 1:02d}", "danger")

        elif power == "jam":
            opponents = [x for x in self.players.values() if x.id != pid and x.connected]
            if not opponents:
                raise ValueError("No target available")
            def owned_count(x: Player):
                return sum(1 for n in self.nodes if n.owner == x.id and not n.collapsed)
            target = max(opponents, key=lambda x: (owned_count(x), x.score))
            target.jammed_until = max(target.jammed_until, t + 3.0)
            self.log(f"{p.name} jammed {target.name}", "danger")

        elif power == "chaos":
            self.trigger_shift(force=True, t=t)
            self.log(f"{p.name} forced a chaos SHIFT", "shift")

        p.energy -= cost
        p.cooldowns[power] = t + cooldown
        p.score += 5

    def trigger_shift(self, force: bool = False, t: Optional[float] = None):
        t = now() if t is None else t
        choices = ["BLACKOUT", "OVERCLOCK", "CHAIN", "INVERSION"]
        if self.shift and not force:
            return
        shift_type = random.choice(choices)
        self.shift = {"type": shift_type, "until": t + SHIFT_DURATION}
        if shift_type == "INVERSION":
            active = [p.id for p in self.players.values() if p.connected]
            if len(active) > 1:
                active_sorted = sorted(active, key=lambda pid: self.players[pid].score, reverse=True)
                rotated = active_sorted[1:] + active_sorted[:1]
                mapping = dict(zip(active_sorted, rotated))
                for n in self.nodes:
                    if n.owner in mapping and not n.collapsed:
                        n.owner = mapping[n.owner]
                        if n.shielded_by in mapping and n.shield_until > t:
                            n.shielded_by = mapping[n.shielded_by]
        self.log(f"SHIFT: {shift_type}", "shift")

    def _secret_completed(self, p: Player) -> bool:
        owned = [n.id for n in self.nodes if n.owner == p.id and not n.collapsed]
        key = p.secret.get("key")
        if key == "REACTOR":
            return REACTOR_ID in owned
        if key == "CORNERS":
            return sum(i in owned for i in [0, 4, 20, 24]) >= 3
        if key == "SEVEN":
            return len(owned) == 7
        if key == "EDGE":
            edges = {i for i in range(25) if i // 5 in (0, 4) or i % 5 in (0, 4)}
            return len(edges.intersection(owned)) >= 5
        if key == "ENERGY":
            return p.energy >= 70
        if key == "DOMINATE":
            return len(owned) >= 9
        if key == "SURVIVOR":
            return self.final_collapse and len(owned) >= 4
        if key == "CENTER_RING":
            return sum(i in owned for i in neighbors(REACTOR_ID)) >= 3
        return False

    def finish(self):
        if self.phase == "RESULTS":
            return
        self.phase = "RESULTS"
        scored = []
        for p in self.players.values():
            owned = sum(1 for n in self.nodes if n.owner == p.id and not n.collapsed)
            completed = self._secret_completed(p)
            final_score = int(p.score) + owned * 30 + int(p.energy)
            bonus = p.secret.get("bonus", 0) if completed else 0
            final_score += bonus
            scored.append({
                "id": p.id, "name": p.name, "color": p.color, "score": final_score,
                "owned": owned, "energy": int(p.energy), "secret": p.secret.get("text", ""),
                "secretCompleted": completed, "secretBonus": bonus,
            })
        self.results = sorted(scored, key=lambda x: (x["score"], x["owned"]), reverse=True)
        if self.results:
            self.log(f"{self.results[0]['name']} survived NULL", "winner")

    def rematch(self, pid: str):
        if pid != self.host_id:
            raise ValueError("Only the host can restart")
        if self.phase != "RESULTS":
            raise ValueError("Rematch is only available after results")
        self.phase = "LOBBY"
        self._reset_match_data()
        self.log("Rematch ready", "system")

    def to_storage(self) -> Dict:
        return {
            "code": self.code,
            "host_id": self.host_id,
            "phase": self.phase,
            "created_at": self.created_at,
            "countdown_ends_at": self.countdown_ends_at,
            "started_at": self.started_at,
            "ends_at": self.ends_at,
            "next_shift_at": self.next_shift_at,
            "shift": self.shift,
            "final_collapse": self.final_collapse,
            "last_collapse_at": self.last_collapse_at,
            "last_tick": self.last_tick,
            "results": self.results,
            "feed": self.feed,
            "players": {
                pid: {
                    "id": p.id, "name": p.name, "color": p.color, "token": p.token,
                    "energy": p.energy, "score": p.score, "connected": p.connected,
                    "jammed_until": p.jammed_until, "cooldowns": p.cooldowns,
                    "secret": p.secret, "session_id": p.session_id,
                }
                for pid, p in self.players.items()
            },
            "nodes": [
                {
                    "id": n.id, "owner": n.owner, "shielded_by": n.shielded_by,
                    "shield_until": n.shield_until, "collapsed": n.collapsed,
                }
                for n in self.nodes
            ],
        }

    @classmethod
    def from_storage(cls, data: Dict) -> "Room":
        room = cls(str(data["code"]))
        room.host_id = data.get("host_id")
        room.phase = data.get("phase", "LOBBY")
        room.created_at = float(data.get("created_at", now()))
        room.countdown_ends_at = float(data.get("countdown_ends_at", 0.0))
        room.started_at = float(data.get("started_at", 0.0))
        room.ends_at = float(data.get("ends_at", 0.0))
        room.next_shift_at = float(data.get("next_shift_at", 0.0))
        room.shift = data.get("shift")
        room.final_collapse = bool(data.get("final_collapse", False))
        room.last_collapse_at = float(data.get("last_collapse_at", 0.0))
        room.last_tick = float(data.get("last_tick", now()))
        room.results = list(data.get("results", []))
        room.feed = list(data.get("feed", []))
        room.players = {}
        for pid, raw in data.get("players", {}).items():
            room.players[pid] = Player(
                id=str(raw.get("id", pid)),
                name=str(raw.get("name", "Operator")),
                color=str(raw.get("color", "cyan")),
                token=str(raw.get("token", "")),
                energy=float(raw.get("energy", 70.0)),
                score=float(raw.get("score", 0.0)),
                connected=bool(raw.get("connected", False)),
                jammed_until=float(raw.get("jammed_until", 0.0)),
                cooldowns={k: float(v) for k, v in raw.get("cooldowns", {}).items()},
                secret=dict(raw.get("secret", {})),
                session_id=str(raw.get("session_id", "")),
            )
        raw_nodes = data.get("nodes") or []
        room.nodes = [
            Node(
                id=int(raw.get("id", idx)),
                owner=raw.get("owner"),
                shielded_by=raw.get("shielded_by"),
                shield_until=float(raw.get("shield_until", 0.0)),
                collapsed=bool(raw.get("collapsed", False)),
            )
            for idx, raw in enumerate(raw_nodes)
        ]
        if len(room.nodes) != GRID * GRID:
            room.nodes = [Node(i) for i in range(GRID * GRID)]
        return room

    def public_state(self, viewer_id: Optional[str] = None, t: Optional[float] = None) -> Dict:
        t = now() if t is None else t
        players = []
        for p in self.players.values():
            players.append({
                "id": p.id,
                "name": p.name,
                "color": p.color,
                "energy": round(p.energy, 1),
                "score": int(p.score),
                "connected": p.connected,
                "isHost": p.id == self.host_id,
                "jammedUntil": int(p.jammed_until * 1000),
                "cooldowns": {k: int(v * 1000) for k, v in p.cooldowns.items()} if p.id == viewer_id else {},
                "secret": p.secret if p.id == viewer_id else None,
            })
        return {
            "code": self.code,
            "phase": self.phase,
            "serverNow": int(t * 1000),
            "countdownEndsAt": int(self.countdown_ends_at * 1000),
            "startedAt": int(self.started_at * 1000),
            "endsAt": int(self.ends_at * 1000),
            "nextShiftAt": int(self.next_shift_at * 1000),
            "shift": {**self.shift, "until": int(self.shift["until"] * 1000)} if self.shift else None,
            "finalCollapse": self.final_collapse,
            "hostId": self.host_id,
            "viewerId": viewer_id,
            "players": players,
            "nodes": [
                {
                    "id": n.id,
                    "owner": n.owner,
                    "shieldedBy": n.shielded_by if n.shield_until > t else None,
                    "shieldUntil": int(n.shield_until * 1000) if n.shield_until > t else 0,
                    "collapsed": n.collapsed,
                    "reactor": n.id == REACTOR_ID,
                }
                for n in self.nodes
            ],
            "results": self.results,
            "feed": self.feed,
        }
