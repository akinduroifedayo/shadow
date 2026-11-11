from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from urllib.parse import urlsplit


WATCHING_PHASES = frozenset({"running", "watch"})
ACTIVE_PHASES = WATCHING_PHASES | {"paused"}
RESTORE_PHASES = frozenset({"paused", "undone", "stopped"})
IDLE_PHASES = frozenset({"undone", "manual", "stopped"})
CHAT_MOVE_PHASES = frozenset({"watch", "paused"})


def choose_conversation(conversation_id: str, switch_id: str, share: int) -> bool:
    if share not in (10, 50, 100):
        raise ValueError("share must be 10, 50, or 100")
    value = sha256(f"{len(switch_id)}:{switch_id}{conversation_id}".encode()).digest()
    return int.from_bytes(value, "big") * 100 < share * (1 << 256)


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    base_url: str
    api_mode: str

    def __post_init__(self):
        for name in (self.provider, self.model, self.api_mode):
            if not name or any(c.isspace() for c in name) or name.startswith("-"):
                raise ValueError("invalid route name")
        url = urlsplit(self.base_url)
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("route URL must not contain credentials or parameters")
        if self.base_url and (url.scheme not in {"http", "https"} or not url.hostname):
            raise ValueError("invalid route URL")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))

    def same_wire(self, other: Route) -> bool:
        return (self.provider, self.base_url, self.api_mode) == (other.provider, other.base_url, other.api_mode)

    def matches(self, event: dict, *, model: bool = True) -> bool:
        if not isinstance(event, dict) or not isinstance(event.get("base_url", ""), str):
            return False
        event = dict(event, base_url=event.get("base_url", "").rstrip("/"))
        return all(event.get(key, "") == getattr(self, key)
                   for key in ("provider", "base_url", "api_mode") + (("model",) if model else ()))


@dataclass(frozen=True)
class Proposal:
    test_id: str
    old: Route
    new: Route
    verdict: str
    headline: str = ""

    @property
    def gradual(self) -> bool:
        return self.old.same_wire(self.new)

    @property
    def route_allowed(self) -> bool:
        return self.old != self.new and (self.old.provider != self.new.provider or self.gradual)

    @property
    def allowed(self) -> bool:
        return self.route_allowed and self.verdict in {"better", "holds_up"}

    @property
    def override_reason(self) -> str | None:
        if self.verdict in {'better', 'holds_up'}:
            return None
        if self.verdict == 'worse':
            return 'worse'
        return 'no_passing_test' if self.verdict == 'untested' else 'too_close'


@dataclass
class Turn:
    session_id: str
    task_id: str
    turn_id: str
    target: str
    epoch: int
    response_model: str | None = None
    finished: bool = False
    failed: bool = False
    signals: list[str] = field(default_factory=list)
    requests: dict[str, str] = field(default_factory=dict)
    request_errors: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    sent: bool = False


@dataclass(frozen=True)
class Counts:
    turns: int = 0
    trouble: int = 0


def rollback_reason(new: Counts, old: Counts, failures: int, *, watch: bool = False) -> str | None:
    if failures >= 3:
        return "requests"
    if watch:
        return "trouble" if new.trouble >= 5 else None
    if new.turns >= 10 and new.trouble >= 4:
        if not old.turns:
            return "trouble" if 5 * new.trouble > new.turns else None
        if 5 * (new.trouble * old.turns - old.trouble * new.turns) > new.turns * old.turns:
            return "trouble"
    return None


@dataclass
class SwitchRecord:
    id: str
    test_id: str
    old: Route
    new: Route
    mode: str
    phase: str = "running"
    step: int = 1
    step_turns: int = 20
    step_start: int = 0
    epoch: int = 0
    assignments: dict[str, str] = field(default_factory=dict)
    draining: dict[str, str] = field(default_factory=dict)
    turns: dict[str, Turn] = field(default_factory=dict)
    sessions: dict[str, str] = field(default_factory=dict)
    session_keys: dict[str, str] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)
    last_turn: dict[str, str] = field(default_factory=dict)
    chat_moves: dict[str, str] = field(default_factory=dict)
    failure_streak: int = 0
    last_response_model: str | None = None
    trips: list[dict] = field(default_factory=list)
    resumes: int = 0
    undo: bool = False
    default_changed: bool = False
    config_pending: str | None = None
    notices: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    completed_on: str | None = None
    override: dict | None = None
    version: int = 2

    @property
    def watching(self) -> bool:
        return self.phase in WATCHING_PHASES

    def counts(self, side: str) -> Counts:
        model = getattr(self, side).model
        turns = [t for t in self.turns.values() if t.epoch == self.epoch and t.finished
                 and (self.old.model != self.new.model or t.target == side)
                 and (t.response_model or (getattr(self, t.target).model
                      if t.sent and (t.failed or "stopped_turn" in t.signals) else None)) == model]
        return Counts(len(turns), sum(bool(t.signals) for t in turns))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SwitchRecord:
        data = dict(data)
        data["old"] = Route(**data["old"])
        data["new"] = Route(**data["new"])
        data["turns"] = {key: Turn(**value) for key, value in data["turns"].items()}
        return cls(**data)


def turn_key(session_id: str, task_id: str, turn_id: str) -> str:
    return "".join(f"{len(value)}:{value}" for value in (session_id, task_id, turn_id))


def is_active(record: SwitchRecord | None) -> bool:
    return record is not None and record.phase in ACTIVE_PHASES
