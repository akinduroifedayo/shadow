from __future__ import annotations

from dataclasses import asdict
from contextlib import ExitStack
from datetime import datetime, timezone
import logging
import re

logger = logging.getLogger(__name__)
import shlex
from threading import Lock
from time import monotonic
from functools import wraps
from uuid import uuid4

try:
    import fcntl
except ImportError:
    fcntl = None

from .domain import CHAT_MOVE_PHASES, RESTORE_PHASES, Proposal, SwitchRecord, Turn, choose_conversation, is_active, rollback_reason, turn_key
from .store import Store


USER_PLATFORMS = frozenset({"cli", "tui", "desktop", "acp", "telegram", "discord", "slack", "signal", "whatsapp", "matrix"})
CONFIG_RETRY_SECONDS = 30
SHADOW_MARKERS = ("[shadow:controller]", "[shadow:replay]", "shadow-controller:", "shadow-setup-check:")


def private_event(method):
    @wraps(method)
    def handle(self, **event):
        self._sync_identity()
        event["_chooser_id"] = event.get("session_id", "")
        for name in ("session_id", "old_session_id", "task_id", "turn_id", "api_request_id", "session_key"):
            if event.get(name):
                event[name] = self.store.identity(event[name])
        return method(self, **event)
    return handle


class Engine:
    def __init__(self, store: Store, *, verified_pair=None, verified_sealed_pair=None, set_default=None):
        self.store = store
        self.verified_pair = verified_pair or (lambda old, new: False)
        self.verified_sealed_pair = verified_sealed_pair or (lambda old, new: False)
        self.set_default = set_default
        self.excluded_sessions = set()
        self.routing_turns = {}
        self.sessions = {}
        self.config_lock = Lock()
        self.identity_epoch = store.identity_epoch
        self.config_retry = None

    def _sync_identity(self):
        epoch = self.store.identity_epoch
        if epoch != self.identity_epoch:
            self.excluded_sessions.clear()
            self.routing_turns.clear()
            self.sessions.clear()
            self.identity_epoch = epoch

    @property
    def record(self) -> SwitchRecord | None:
        return self.store.read()

    def validate_routes(self, proposal: Proposal):
        from .decision import SwitchBlocked, require_model_change

        require_model_change(proposal.old, proposal.new)
        if not proposal.route_allowed:
            raise SwitchBlocked('trial.switch_incompatible')
        if proposal.gradual and not self.verified_pair(proposal.old, proposal.new):
            raise SwitchBlocked('switch.error.compatibility')

    def start(self, proposal: Proposal, *, switch_id=None, step_turns=20, confirm_override=False):
        self.validate_routes(proposal)
        if not proposal.allowed and not (confirm_override is True and proposal.override_reason):
            raise ValueError("switch is not allowed by this test")
        gradual = proposal.gradual
        if step_turns < 1:
            raise ValueError("step_turns must be positive")
        if fcntl is None and self.store.salt_path is not None:
            raise RuntimeError("default config locking is unavailable on this platform")
        with self.store.transaction(create=True) as holder:
            current = holder[0]
            if is_active(current) or (current and current.config_pending and current.phase != "manual"):
                raise ValueError("a switch is in progress")
            record = SwitchRecord(switch_id or uuid4().hex, proposal.test_id, proposal.old, proposal.new,
                                  "gradual" if gradual else "all_at_once", step_turns=step_turns)
            holder[0] = record
            if proposal.override_reason:
                from .decision import switch_warning
                record.override = {'by': 'owner', 'reason': proposal.override_reason,
                                   'headline': proposal.headline or switch_warning(proposal),
                                   'at': datetime.now(timezone.utc).isoformat()}
                record.history.append({'kind': 'override', **record.override})
            if record.mode == "all_at_once":
                record.phase = "watch"
                record.config_pending = "new"
            self._notice(record, "step1" if gradual else "watch_start")
        self.reconcile_config()
        return self.record

    def _notice(self, record, kind, **values):
        notice = {"id": uuid4().hex, "kind": kind, **values}
        record.notices.append(notice)
        record.history.append({"at": datetime.now(timezone.utc).isoformat(), **notice})

    def drain_notices(self) -> list[dict]:
        if self.record is None:
            return []
        with self.store.transaction() as holder:
            notices = holder[0].notices[:]
            holder[0].notices.clear()
            return notices

    def acknowledge_notices(self, switch_id, notices):
        with self.store.transaction() as holder:
            if holder is not None and holder[0] is not None and holder[0].id == switch_id:
                holder[0].notices = [notice for notice in holder[0].notices if notice not in notices]

    @private_event
    def session_started(self, *, session_id, platform, session_key=None, **_):
        try:
            record = self.record
            if record is None or not session_id or platform not in USER_PLATFORMS or session_id in self.excluded_sessions:
                return
            if not record.watching:
                return
            with self.store.transaction() as holder:
                record = holder[0]
                if (self.store.identity_epoch != self.identity_epoch or not record.watching
                        or not session_id or platform not in USER_PLATFORMS):
                    return
                self.sessions[session_id] = platform
                if session_id not in record.excluded:
                    record.sessions[session_id] = platform
                    if session_key:
                        record.session_keys[session_key] = session_id
        finally:
            self.reconcile_config()

    def exclude_session(self, session_id: str):
        self._sync_identity()
        self._exclude_session(self.store.identity(session_id))

    def _exclude_session(self, session_id: str):
        self.excluded_sessions.add(session_id)
        self.sessions.pop(session_id, None)
        for key, turn in list(self.routing_turns.items()):
            if turn.session_id == session_id:
                self.routing_turns.pop(key, None)
        record = self.record
        if record is None or not record.watching:
            return
        with self.store.transaction() as holder:
            record = holder[0]
            if not record.watching:
                return
            if session_id not in record.excluded:
                record.excluded.append(session_id)
            record.sessions.pop(session_id, None)
            record.turns = {k: t for k, t in record.turns.items() if t.session_id != session_id}

    @private_event
    def compacted(self, *, session_id, old_session_id="", **_):
        if not session_id or not old_session_id or session_id == old_session_id:
            return
        if old_session_id in self.excluded_sessions:
            self._exclude_session(session_id)
            return
        record = self.record
        if record is None or old_session_id not in record.sessions:
            return
        with self.store.transaction() as holder:
            record = holder[0]
            for mapping in (record.sessions, record.assignments, record.draining, record.chat_moves):
                if old_session_id in mapping:
                    mapping[session_id] = mapping.pop(old_session_id)
            if old_session_id in self.sessions:
                self.sessions[session_id] = self.sessions.pop(old_session_id)
            record.session_keys = {key: session_id if value == old_session_id else value
                                   for key, value in record.session_keys.items()}
            replacements = {}
            for key, turn in list(record.turns.items()):
                if turn.session_id == old_session_id:
                    turn.session_id = session_id
                    new_key = turn_key(session_id, turn.task_id, turn.turn_id)
                    replacements[key] = new_key
                    record.turns[new_key] = record.turns.pop(key)
            if old_session_id in record.last_turn:
                key = record.last_turn.pop(old_session_id)
                record.last_turn[session_id] = replacements.get(key, key)
        for key, turn in list(self.routing_turns.items()):
            if turn.session_id == old_session_id:
                turn.session_id = session_id
                self.routing_turns[turn_key(session_id, turn.task_id, turn.turn_id)] = self.routing_turns.pop(key)

    @private_event
    def admit(self, *, session_id, task_id="", turn_id="", model="", platform="", parent_session_id="", user_message="", _chooser_id="", **_):
        try:
            if not all((session_id, task_id, turn_id, model)) or parent_session_id:
                if not all((task_id, turn_id, model)):
                    logger.log(logging.WARNING if self.record is not None else logging.DEBUG,
                               "shadow switch admit failed closed: missing task_id, turn_id, or model")
                return
            record = self.record
            if record is None or record.phase == "manual" or session_id in self.excluded_sessions:
                return
            if platform not in USER_PLATFORMS or any(marker in str(user_message) for marker in SHADOW_MARKERS):
                self._exclude_session(session_id)
                return
            if not record.watching:
                eligible = record.sessions if record.phase == "paused" else record.draining
                if session_id not in eligible:
                    return
                if model not in {record.old.model, record.new.model}:
                    return
                key = turn_key(session_id, task_id, turn_id)
                target = record.assignments.get(session_id, "new") if record.phase == "done" else "old"
                self.routing_turns.setdefault(key, Turn(session_id, task_id, turn_id, target, record.epoch))
                return
            with self.store.transaction() as holder:
                record = holder[0]
                if not record.watching or session_id not in record.sessions or session_id in record.excluded:
                    return
                if (model not in {record.old.model, record.new.model}
                        or (record.mode == "gradual" and model != record.old.model)):
                    if any(t.session_id == session_id for t in record.turns.values()):
                        self._manual(record, model)
                    else:
                        record.excluded.append(session_id)
                        record.sessions.pop(session_id, None)
                    return
                key = turn_key(session_id, task_id, turn_id)
                if key in record.turns:
                    return
                record.last_turn.pop(session_id, None)
                if record.phase == "running":
                    target = record.assignments.setdefault(session_id, "new" if choose_conversation(_chooser_id, record.id, (10, 50, 100)[record.step - 1]) else "old")
                elif record.phase == "watch":
                    target = "new" if model == record.new.model else "old"
                else:
                    return
                record.turns[key] = Turn(session_id, task_id, turn_id, target, record.epoch)
        finally:
            self.reconcile_config()

    def _turn(self, record, event):
        if record is None or not all(event.get(k) for k in ("session_id", "task_id", "turn_id")):
            return None
        return record.turns.get(turn_key(event["session_id"], event["task_id"], event["turn_id"]))

    def _guard_sealed_history(self, record, turn, request, original):
        from .reasoning import _encrypted_blobs

        if (original == record.old and not turn.sent and turn.target == "new" and any(_encrypted_blobs(request))
                and not self.verified_sealed_pair(record.old, record.new)):
            turn.target = "old"

    @private_event
    def rewrite(self, **event):
        record = self.record
        if (record is None or event.get("session_id") in self.excluded_sessions
                or not all(event.get(k) for k in ("session_id", "task_id", "turn_id"))):
            return None
        key = turn_key(event["session_id"], event["task_id"], event["turn_id"])
        turn = self.routing_turns.get(key) or self._turn(record, event)
        if (turn is None or record.phase == "manual"
                or (not is_active(record) and event["session_id"] not in record.draining)):
            return None
        if key in self.routing_turns:
            if record.mode != "gradual":
                return None
            request = event.get("request", {})
            original = next((route for route in (record.old, record.new)
                             if route.matches(event) and request.get("model") == route.model), None)
            if original is None:
                return None
            if not turn.sent:
                turn.target = record.assignments.get(turn.session_id, "new") if record.phase == "done" else "old"
            self._guard_sealed_history(record, turn, request, original)
            turn.sent = True
            target = getattr(record, turn.target)
            if target.model == original.model:
                return None
            return {"request": {**request, "model": target.model}, "source": "shadow", "reason": "conversation_assignment"}
        with self.store.transaction() as holder:
            record = holder[0]
            turn = self._turn(record, event)
            if turn.finished or (record.mode == "all_at_once" and record.config_pending):
                return None
            if not turn.sent and record.phase in RESTORE_PHASES:
                turn.target = "old"
            request = event.get("request", {})
            original = next((route for route in (record.old, record.new)
                             if route.matches(event) and request.get("model") == route.model), None)
            if original is None:
                return None
            if record.phase == "running" and original != record.old:
                return None
            if record.mode == "all_at_once":
                if original == record.new:
                    turn.sent = True
                    turn.requests[event["api_request_id"]] = record.new.model
                return None
            if not original.same_wire(record.old):
                return None
            self._guard_sealed_history(record, turn, request, original)
            target = getattr(record, turn.target)
            turn.sent = True
            turn.requests[event["api_request_id"]] = target.model
            if request["model"] == target.model:
                return None
            return {"request": {**request, "model": target.model}, "source": "shadow", "reason": "conversation_assignment"}

    def advance(self):
        with self.store.transaction() as holder:
            if holder is None or holder[0] is None or holder[0].phase != "running":
                raise ValueError("no gradual switch is running")
            if holder[0].step == 3:
                raise ValueError(f"wait for {holder[0].step_turns} new-model turns")
            self._advance(holder[0], manual=True)
        self.reconcile_config()

    def _advance(self, record, *, manual=False):
        if record.step < 3:
            record.step += 1
            observed = record.counts("new").turns
            turns = max(0, observed - record.step_start)
            record.step_start = observed
            self._notice(record, f"step{record.step}", turns=turns, manual=manual)
        else:
            record.config_pending = "new"
            record.phase = "done"
            record.draining = {s: record.assignments.get(s, "new") for s in record.sessions}
            record.completed_on = datetime.now(timezone.utc).date().isoformat()
            self._notice(record, "done")

    @private_event
    def response(self, **event):
        if not self._observable(event):
            return
        with self.store.transaction() as holder:
            record = holder[0]
            turn = self._turn(record, event)
            model = event.get("response_model")
            if not self._request_matches(record, turn, event):
                return
            observation = f"ok:{event['api_request_id']}:{event.get('started_at', '')}"
            if observation in turn.observations:
                return
            turn.observations.append(observation)
            if event["api_request_id"] in turn.request_errors:
                turn.request_errors.remove(event["api_request_id"])
            record.last_response_model = model
            routes = (getattr(record, turn.target), record.old, record.new)
            canonical = next((route.model for route in routes if model == route.model), None)
            if canonical is None and isinstance(model, str):
                canonical = next((route.model for route in routes
                                  if re.fullmatch(re.escape(route.model) + r"-\d{4}-\d{2}-\d{2}", model)), None)
            if canonical is None:
                turn.observations.append(f"unknown_model:{model}")
                return
            if turn.response_model and turn.response_model != canonical:
                self._trip(record, "mixed_models", turn)
            turn.response_model = canonical
            if canonical == record.new.model:
                record.failure_streak = 0
        self.reconcile_config()

    @private_event
    def request_error(self, **event):
        if not self._observable(event):
            return
        with self.store.transaction() as holder:
            record = holder[0]
            turn = self._turn(record, event)
            if not self._request_matches(record, turn, event):
                return
            observation = f"error:{event['api_request_id']}:{event.get('retry_count', '')}:{event.get('started_at', '')}"
            if observation in turn.observations:
                return
            turn.observations.append(observation)
            if event["api_request_id"] not in turn.request_errors:
                turn.request_errors.append(event["api_request_id"])
        self.reconcile_config()

    @private_event
    def tool_result(self, **event):
        if not self._observable(event) or event.get("status") != "error":
            return
        with self.store.transaction() as holder:
            turn = self._turn(holder[0], event)
            if turn.sent and "tool_failure" not in turn.signals:
                turn.signals.append("tool_failure")

    @private_event
    def finish(self, **event):
        if all(event.get(k) for k in ("session_id", "task_id", "turn_id")):
            self.routing_turns.pop(turn_key(event["session_id"], event["task_id"], event["turn_id"]), None)
        if not self._observable(event):
            return
        with self.store.transaction() as holder:
            record = holder[0]
            turn = self._turn(record, event)
            if turn.finished or not turn.sent:
                return
            turn.finished = True
            turn.failed = bool(event.get("failed"))
            if turn.failed:
                turn.signals.append("failed_turn")
                if turn.request_errors:
                    turn.signals.append("failed_requests")
                    if turn.target == "new":
                        record.failure_streak += 1
            if event.get("interrupted"):
                turn.signals.append("stopped_turn")
            record.last_turn[turn.session_id] = turn_key(turn.session_id, turn.task_id, turn.turn_id)
            new = record.counts("new")
            reason = rollback_reason(new, record.counts("old"), record.failure_streak, watch=record.mode == "all_at_once")
            if reason:
                self._trip(record, reason, turn)
            elif record.mode == "all_at_once" and new.turns >= 20:
                record.phase = "done"
                record.draining = {s: "new" for s in record.sessions}
                record.completed_on = datetime.now(timezone.utc).date().isoformat()
                self._notice(record, "watch_done", new=asdict(new))
            elif record.mode == "gradual" and new.turns - record.step_start >= record.step_turns:
                self._advance(record)
        self.reconcile_config()

    @private_event
    def command(self, **event):
        command = event.get("command", "").lstrip("/")
        if command not in {"model", "retry", "undo"}:
            return
        record = self.record
        if record is None or record.phase == "manual":
            return
        with self.store.transaction() as holder:
            record = holder[0]
            if command == "model":
                try:
                    chosen = shlex.split(event.get("args_raw", ""))
                except ValueError:
                    return
                provider = None
                models = []
                while chosen:
                    token = chosen.pop(0)
                    if token in {"--provider", "--reasoning"}:
                        if not chosen:
                            return
                        value = chosen.pop(0)
                        if token == "--provider":
                            provider = value
                    elif token in {"--session", "--global", "--once", "--refresh"}:
                        continue
                    elif token.startswith("-"):
                        return
                    else:
                        models.append(token)
                if not models and provider is None:
                    return
                model = models[0] if models else provider
                session = record.session_keys.get(event.get("session_key"), event.get("session_key"))
                if not is_active(record):
                    record.draining.pop(session, None)
                    for key, turn in list(self.routing_turns.items()):
                        if turn.session_id == session:
                            self.routing_turns.pop(key, None)
                    return
                route = record.new if record.phase == "watch" else record.old
                expected = route.model
                move = f"{record.epoch}:{record.phase}:{expected}"
                instructed = (record.mode == "all_at_once" and not record.config_pending
                              and record.phase in CHAT_MOVE_PHASES
                              and models == [expected] and provider in {None, route.provider} and session
                              and record.chat_moves.get(session) != move)
                if record.phase == "watch" and any(
                        t.session_id == session and t.epoch == record.epoch and t.sent and t.target == "new"
                        for t in record.turns.values()):
                    instructed = False
                if instructed:
                    record.chat_moves[session] = move
                else:
                    self._manual(record, model)
            elif record.watching and command in {"retry", "undo"}:
                session = record.session_keys.get(event.get("session_key"), event.get("session_key"))
                key = record.last_turn.get(session)
                if key is None:
                    return
                turn = record.turns[key]
                if turn.epoch != record.epoch:
                    return
                signal = f"{command}_requested"
                if signal not in turn.signals:
                    turn.signals.append(signal)
                reason = rollback_reason(record.counts("new"), record.counts("old"), record.failure_streak,
                                         watch=record.mode == "all_at_once")
                if reason:
                    self._trip(record, reason, turn)
        self.reconcile_config()

    def _observable(self, event):
        record = self.record
        turn = self._turn(record, event)
        return record is not None and record.watching and turn is not None and turn.epoch == record.epoch

    def _request_matches(self, record, turn, event):
        if not turn.sent or turn.finished or event.get("api_request_id") not in turn.requests:
            return False
        route = getattr(record, turn.target)
        return route.matches(event, model=False)

    def _trip(self, record, reason, current=None):
        if not record.watching:
            return
        new = asdict(record.counts("new"))
        if current is not None and not current.finished and current.target == "new":
            new["turns"] += 1
        trip = {"reason": reason, "new": new, "old": asdict(record.counts("old")), "failures": record.failure_streak}
        record.trips.append(trip)
        record.phase = "paused"
        if record.default_changed:
            record.config_pending = "old"
        self._notice(record, "watch_trip" if record.mode == "all_at_once" else "rollback",
                     session_id=current.session_id if current is not None else None, **trip)

    def resume(self):
        if fcntl is None and self.store.salt_path is not None:
            raise RuntimeError("default config locking is unavailable on this platform")
        with self.store.transaction() as holder:
            if holder is None or holder[0] is None or holder[0].phase != "paused":
                raise ValueError("no paused switch")
            record = holder[0]
            if not self.verified_pair(record.old, record.new) and record.mode == "gradual":
                raise ValueError("exact-pair compatibility probe is required")
            record.phase = "running" if record.mode == "gradual" else "watch"
            record.step = 1
            record.step_start = 0
            record.failure_streak = 0
            record.epoch += 1
            record.resumes += 1
            record.last_turn.clear()
            record.turns.clear()
            if record.mode == "gradual":
                record.assignments.clear()
            if record.mode == "all_at_once":
                record.config_pending = "new"
            self._notice(record, "resume" if record.mode == "gradual" else "watch_start")
        self.reconcile_config()

    def undo(self, *, stop=False):
        with self.store.transaction() as holder:
            if holder is None or holder[0] is None or holder[0].phase == "manual":
                raise ValueError("no switch to undo")
            record = holder[0]
            if record.undo:
                if record.config_pending == "old":
                    return "undo_pending"
                return
            kind = "undo_default" if record.default_changed else "stop" if stop else "undo"
            if record.mode == "all_at_once":
                kind = "watch_undo"
            record.undo = True
            if record.phase != "done":
                record.draining = {s: "old" for s in record.sessions}
            record.phase = "stopped" if stop else "undone"
            if record.default_changed or record.config_pending == "new":
                record.config_pending = "old"
            self._notice(record, kind)
        self.reconcile_config()

    def _manual(self, record, model):
        record.phase = "manual"
        record.notices.clear()
        self._notice(record, "manual", chosen=model)

    def reconcile_config(self, *, force=False):
        for _ in range(3):
            record = self.record
            if record is None or record.config_pending is None or record.phase == "manual":
                return
            if self.set_default is None:
                raise RuntimeError("default writer is required")
            if not self.config_lock.acquire(blocking=False):
                return
            try:
                with ExitStack() as locks:
                    if self.store.salt_path is not None:
                        if fcntl is None:
                            raise RuntimeError("default config locking is unavailable on this platform")
                        lock_file = locks.enter_context(self.store.salt_path.with_name("switch.config.lock").open("a"))
                        try:
                            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            return
                    while True:
                        record = self.record
                        if record.phase == "manual":
                            return
                        if record.config_pending is None:
                            break
                        target = record.config_pending
                        intent = (record.id, record.epoch, target)
                        if (not force and self.config_retry is not None
                                and self.config_retry[0] == intent and monotonic() < self.config_retry[1]):
                            return
                        try:
                            self.set_default(getattr(record, target))
                        except (RuntimeError, OSError):
                            self.config_retry = intent, monotonic() + CONFIG_RETRY_SECONDS
                            raise
                        self.config_retry = None
                        with self.store.transaction() as holder:
                            current = holder[0]
                            if current.id != record.id:
                                continue
                            current.default_changed = target == "new"
                            if current.phase != "manual":
                                if current.config_pending == target:
                                    current.config_pending = None
                                desired = "new" if current.phase in {"done", "watch"} else "old"
                                if current.config_pending is None and desired != target:
                                    current.config_pending = desired
            finally:
                self.config_lock.release()
