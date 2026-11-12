from __future__ import annotations

import argparse
from dataclasses import asdict
import logging
from pathlib import Path
import shlex
from threading import RLock
from time import monotonic

logger = logging.getLogger(__name__)

from ..common.discovery import ModelUnavailable, discover_models
from .copy import copy, fields, render_notice, status
from .defaults import DefaultWriter
from .decision import SwitchBlocked
from .doctor import doctor, pair_doctor
from .pair_probe import run_command
from .domain import is_active
from .engine import Engine
from .store import Store


HISTORY_KINDS = frozenset({"step1", "step2", "step3", "done", "rollback", "resume",
                           "undo", "undo_default", "stop", "manual", "watch_start",
                           "watch_done", "watch_trip", "watch_undo", "override"})
PANEL_HISTORY_FIELDS = ("kind", "at", "turns", "manual", "reason")


def panel_history(record):
    """Public step history. Desktop reads this list, the same one panel() returns."""
    if record is None:
        return []
    rows = []
    for event in record.history:
        if event.get("kind") not in HISTORY_KINDS:
            continue
        row = {key: event.get(key) for key in PANEL_HISTORY_FIELDS}
        if event.get("kind") not in {"rollback", "watch_trip"}:
            row["reason"] = None
        if event.get("kind") == "override":
            row.update(headline=event["headline"], by=event["by"], reason=event["reason"])
        rows.append(row)
    return rows


class SwitchPlugin:
    def __init__(self, ctx, engine, *, config_reader, proposal_lookup=None, availability=None, bound_session=None, catalog_reader=None):
        self.ctx = ctx
        self.catalog_reader = catalog_reader
        self.engine = engine
        self.config_reader = config_reader
        self.proposal_lookup = proposal_lookup or (lambda test_id: None)
        self.availability = availability or (lambda: (False, False))
        self.notice_lock = RLock()
        self.pending_delivery = None
        self.bound_session = bound_session or (lambda: ("", ""))
        self.delivery_attempts = {}

    def model_catalog(self):
        if self.catalog_reader:
            return self.catalog_reader()
        trial = getattr(self.ctx, 'shadow_trial', None)
        return trial.model_catalog() if trial is not None else discover_models()

    def require_target_change(self, route):
        from ..common.models import Model
        from .decision import require_model_change

        if isinstance(route, dict):
            route = Model(**route)
        catalog = self.model_catalog()
        current = catalog.require(catalog.current.provider, catalog.current.model)
        target = catalog.require(route.provider, route.model)
        require_model_change(current, target)

    def validate_routes(self, old, new):
        catalog = self.model_catalog()
        for route in (old, new):
            catalog.require(route.provider, route.model)

    def notice_text(self, *, terminal=False):
        with self.notice_lock:
            if not self.settle_delivery():
                return ""
            record = self.engine.record
            if record is None or (record.config_pending and record.phase != "manual") or not record.notices:
                return ""
            text = "\n\n".join(render_notice(record, notice, terminal=terminal) for notice in record.notices)
            self.engine.acknowledge_notices(record.id, record.notices)
            return text

    def settle_delivery(self):
        if self.pending_delivery is not None:
            switch_id, notices, handle = self.pending_delivery
            if handle.pending:
                return False
            if handle.ok:
                self.engine.acknowledge_notices(switch_id, notices)
            self.pending_delivery = None
        return True

    def observe(self, method, **event):
        getattr(self.engine, method)(**event)
        with self.notice_lock:
            if not self.settle_delivery():
                return
            record = self.engine.record
            if record is None or (record.config_pending and record.phase != "manual") or not record.notices:
                return
            session = self.engine.store.identity(event.get("session_id"))
            if not session and event.get("session_key"):
                key = self.engine.store.identity(event["session_key"])
                session = record.session_keys.get(key, key)
            if session not in record.sessions or session in record.excluded:
                return
            notices = [notice for notice in record.notices if notice.get("session_id") in {None, session}]
            if not notices:
                return
            terminal = event.get("platform") == "cli"
            if terminal:
                if method != "finish":
                    return
            else:
                bound_id, bound_key = self.bound_session()
                if (not bound_key or self.engine.store.identity(bound_id) != session
                        or record.session_keys.get(self.engine.store.identity(bound_key)) != session):
                    return
            now = monotonic()
            notices = [notice for notice in notices if now - self.delivery_attempts.get((notice["id"], session), -60) >= 30]
            if not notices:
                return
            for notice in notices:
                self.delivery_attempts[notice["id"], session] = now
            text = "\n\n".join(render_notice(record, notice, terminal=terminal) for notice in notices)
            if terminal:
                print(text)
            else:
                handle = self.ctx.post_message(text)
                if getattr(handle, "pending", False):
                    self.pending_delivery = record.id, notices, handle
                    return
                if not getattr(handle, "ok", False):
                    return
            self.engine.acknowledge_notices(record.id, notices)

    def command(self, raw_args, *, terminal=False, session_id=None, session_key=None, platform="", **_):
        try:
            args = shlex.split(raw_args)
        except ValueError:
            return copy("switch.error.invalid")
        verb = args[0] if args else ""
        if verb not in {"", "doctor", "status", "switch", "undo"}:
            return copy("switch.help")
        try:
            if verb in {"", "doctor"}:
                setup = self.diagnostics()
                return "\n\n".join((copy("switch.help"), setup)) if not verb else setup
            trial = getattr(self.ctx, "shadow_trial", None)
            if verb == "switch" and trial is not None:
                active = trial.active_record()
                if active:
                    return copy("trial.still_running", id=active["id"])
            restoring = self._restore_pending() if verb == "undo" else False
            try:
                self.engine.reconcile_config(force=True)
            except (RuntimeError, OSError):
                if restoring and self._restore_pending():
                    return copy("switch.undo_pending", **fields(self.engine.record))
                raise
            if verb == "status":
                self.engine.session_started(session_id=session_id, session_key=session_key, platform=platform)
                pending = self.notice_text(terminal=terminal)
                return "\n\n".join(filter(None, (pending, status(self.engine.record))))
            if verb == "undo":
                if self.engine.undo() == "undo_pending" and self._restore_pending():
                    return copy("switch.undo_pending", **fields(self.engine.record))
            else:
                anyway = "--anyway" in args
                args = [arg for arg in args if arg != "--anyway"]
                if len(args) > 2 or any(arg.startswith("--") for arg in args[1:]):
                    return copy("switch.error.invalid")
                record = self.engine.record
                if record and record.phase == "paused" and len(args) == 1:
                    self.validate_routes(record.old, record.new)
                    self.engine.resume()
                else:
                    target = args[1] if len(args) > 1 else None
                    proposal = self.lookup_proposal(target)
                    if proposal is None:
                        return copy("switch.error.no_result")
                    self.require_target_change(proposal.new)
                    self.validate_routes(proposal.old, proposal.new)
                    self.engine.validate_routes(proposal)
                    from .decision import switch_warning
                    if proposal.override_reason and not anyway:
                        command = 'hermes shadow' if terminal else '/shadow'
                        target = proposal.test_id or target
                        return switch_warning(proposal) + '\n' + copy('switch.override.confirm_command', command=command, target=shlex.quote(target))
                    self.engine.start(proposal, confirm_override=anyway)
            self.engine.session_started(session_id=session_id, session_key=session_key, platform=platform)
            return self.notice_text(terminal=terminal) or status(self.engine.record)
        except (ValueError, TypeError) as exc:
            if isinstance(exc, (ModelUnavailable, SwitchBlocked)):
                return str(exc)
            reason = str(exc)
            if "compatibility" in reason:
                return copy("switch.error.compatibility")
            if "progress" in reason:
                return copy("switch.error.busy")
            if self.engine.record and self.engine.record.phase == "manual":
                return copy("switch.error.manual")
            return copy("switch.error.invalid")
        except (RuntimeError, OSError):
            return copy("switch.error.config")

    def lookup_proposal(self, target=None):
        from dataclasses import replace
        from .decision import untested_proposal

        trial = getattr(self.ctx, 'shadow_trial', None)
        store = getattr(trial, 'store', None)

        def check_saved(record):
            from ..trial.cards import switch_limit

            if record and record.get('totals'):
                self.require_target_change(record['trial'])
                limit = switch_limit(record)
                if limit:
                    raise SwitchBlocked(limit)

        if store and target:
            check_saved(store.get(target))
        proposal = self.proposal_lookup(target)
        if proposal is None and target:
            catalog = self.model_catalog()
            model = catalog.resolve(target)
            if store:
                for saved in reversed(store.list()):
                    if saved.get('totals') and (saved['trial']['provider'], saved['trial']['model']) == (model.provider, model.model):
                        if (saved['current']['provider'], saved['current']['model']) != (catalog.current.provider, catalog.current.model):
                            continue
                        check_saved(saved)
                        candidate = self.proposal_lookup(saved['id'])
                        if candidate and (candidate.old.provider, candidate.old.model) == (catalog.current.provider, catalog.current.model):
                            proposal = candidate
                            break
            if proposal is None:
                proposal = untested_proposal(catalog, self.config_reader(), target)
        if proposal and proposal.test_id and store:
            record = store.get(proposal.test_id)
            if record and record.get('totals'):
                check_saved(record)
                from ..trial.presentation import verdict_presentation
                proposal = replace(proposal, headline=verdict_presentation(record)['headline'])
        return proposal

    def verify_pair(self, *, terminal=False, yes=False, **pair):
        checker = getattr(self, "pair_check", None)
        if checker is not None:
            return checker(terminal=terminal, yes=yes, **pair)
        return run_command(self, terminal=terminal, yes=yes, **pair)

    def diagnostics(self, *, pair_only=False):
        proposal = self.proposal_lookup(None)
        routes = dict(old=proposal.old if proposal else None, new=proposal.new if proposal else None)
        if pair_only:
            return pair_doctor(self.config_reader(), profile_name=getattr(self.ctx, "profile_name", "default"), **routes)
        return doctor(self.config_reader(), catalog=self.model_catalog(),
                      profile_name=getattr(self.ctx, "profile_name", "default"), **routes)

    def panel(self):
        record = self.engine.record
        if record is None:
            return None
        summary = {name: getattr(record, name) for name in
                   ("id", "test_id", "mode", "phase", "step", "step_turns", "trips", "resumes", "undo", "completed_on")}
        summary.update({side: {"provider": getattr(record, side).provider, "model": getattr(record, side).model}
                        for side in ("old", "new")})
        return dict(summary, counts={side: asdict(record.counts(side)) for side in ("old", "new")},
                    assignment_unit="conversation", status=status(record),
                    history=panel_history(record),
                    notices=[{"id": notice["id"], "text": render_notice(record, notice)}
                             for notice in record.notices
                             if not record.config_pending or record.phase == "manual"])

    def action(self, action, *, switch_id=None, notice_ids=()):
        if action == "acknowledge":
            record = self.engine.record
            if record is not None:
                self.engine.acknowledge_notices(switch_id, [notice for notice in record.notices
                                                          if notice["id"] in notice_ids])
            return ""
        restoring = False
        if action in {"advance", "resume"} and self.engine.record is not None:
            self.engine.reconcile_config(force=True)
            self.validate_routes(self.engine.record.old, self.engine.record.new)
        if action == "advance":
            self.engine.advance()
        elif action == "resume":
            self.engine.resume()
        elif action in {"undo", "stop"}:
            restoring = self.engine.undo(stop=action == "stop") == "undo_pending"
        else:
            raise ValueError("unknown switch action")
        try:
            self.engine.reconcile_config(force=True)
        except (RuntimeError, OSError):
            if restoring and self._restore_pending():
                return copy("switch.undo_pending", **fields(self.engine.record))
            raise
        if restoring and self._restore_pending():
            return copy("switch.undo_pending", **fields(self.engine.record))
        return self.notice_text() or status(self.engine.record)

    def _restore_pending(self):
        record = self.engine.record
        return record is not None and record.undo and record.config_pending == "old"


def verified_pair_from_config(config_reader, old, new, *, sealed_required=False):
    from .reasoning import matching_proof
    return matching_proof(config_reader(), old, new, sealed_required=sealed_required)


def register(ctx):
    from gateway.session_context import get_session_env
    from hermes_constants import get_hermes_home
    from hermes_cli.config import read_raw_config_readonly
    from plugins.plugin_storage import plugin_db

    profile_home = Path(get_hermes_home()).resolve()
    config_reader = lambda: read_raw_config_readonly() or {}
    data_dir = profile_home / "plugin-data" / "shadow"

    def open_database():
        if Path(get_hermes_home()).resolve() != profile_home:
            raise RuntimeError("profile changed before opening switch storage")
        return plugin_db("shadow", "switch.db")

    engine = Engine(Store(path=data_dir / "switch.db", salt_path=data_dir / "switch.salt",
                          connection_factory=open_database),
                    verified_pair=lambda old, new: verified_pair_from_config(config_reader, old, new),
                    verified_sealed_pair=lambda old, new: verified_pair_from_config(
                        config_reader, old, new, sealed_required=True),
                    set_default=DefaultWriter(profile_home, config_reader=config_reader))
    service = SwitchPlugin(ctx, engine, config_reader=config_reader,
                           proposal_lookup=lambda test_id: getattr(ctx, "shadow_proposal_lookup", lambda _: None)(test_id),
                           availability=lambda: getattr(ctx, "shadow_model_availability", lambda: (False, False))(),
                           bound_session=lambda: (get_session_env("HERMES_SESSION_ID", ""), get_session_env("HERMES_SESSION_KEY", "")))
    ctx.shadow_switch = service
    ctx.shadow_verify_pair = service.verify_pair

    def on_session_start(**event):
        service.observe("session_started", **event)

    def pre_llm_call(**event):
        # Recovery writes the default model and can raise. Admit first would miss
        # enrollment, so enroll, isolate that writer error, then admit.
        record = engine.record
        session_id = event.get("session_id")
        if (record is not None and record.watching and session_id and not event.get("parent_session_id")
                and get_session_env("HERMES_SESSION_ID", "") == session_id):
            try:
                engine.session_started(session_id=session_id, platform=event.get("platform"),
                                       session_key=get_session_env("HERMES_SESSION_KEY", ""))
            except Exception:
                logger.exception("shadow switch session recovery failed closed")
        try:
            service.observe("admit", **event)
        except Exception:
            logger.exception("shadow switch admit failed closed")

    def post_api_request(**event):
        service.observe("response", **event)

    def api_request_error(**event):
        service.observe("request_error", **event)

    def post_tool_call(**event):
        service.observe("tool_result", **event)

    def on_session_end(**event):
        service.observe("finish", **event)

    def post_compaction(**event):
        engine.compacted(**event)

    def pre_command(**event):
        service.observe("command", **event)

    def subagent_start(child_session_id="", **_):
        if engine.record is not None:
            engine.exclude_session(child_session_id)

    for hook, callback in (("on_session_start", on_session_start), ("pre_llm_call", pre_llm_call),
                           ("post_api_request", post_api_request), ("api_request_error", api_request_error),
                           ("post_tool_call", post_tool_call), ("on_session_end", on_session_end),
                           ("pre_command", pre_command), ("subagent_start", subagent_start),
                           ("post_compaction", post_compaction)):
        ctx.register_hook(hook, callback)
    ctx.register_middleware("llm_request", engine.rewrite)

    routes = getattr(ctx, "shadow_command_routes", None)
    if routes is None:
        routes = ctx.shadow_command_routes = {}
    previous = {verb: routes[verb] for verb in ("undo", "status", "doctor") if verb in routes}

    def shared_command(raw_args, **context):
        verb = raw_args.strip().split(maxsplit=1)[0] if raw_args.strip() else ""
        handler = previous.get(verb)
        if handler is not None:
            if verb == "doctor":
                body = handler(raw_args, **context)
                try:
                    tokens = shlex.split(raw_args)
                except ValueError:
                    tokens = []
                if "--verify" in tokens or "--verify-pair" in tokens:
                    return body
                return "\n\n".join(filter(None, (body, service.diagnostics(pair_only=True))))
            record = engine.record
            trial = getattr(ctx, "shadow_trial", None)
            if not is_active(record) and (record is None or (trial and trial.active_record())):
                return handler(raw_args, **context)
        return service.command(raw_args, **context)

    routes["switch"] = service.command
    routes.update({verb: shared_command for verb in ("undo", "status", "doctor")})
    if not getattr(ctx, "shadow_router_registered", False):
        def dispatch(raw_args, **context):
            verb = raw_args.strip().split(maxsplit=1)[0] if raw_args.strip() else ""
            handler = ctx.shadow_command_routes.get(verb, service.command)
            return handler(raw_args, **context)

        def cli_setup(parser):
            parser.add_argument("shadow_args", nargs=argparse.REMAINDER)

        def cli_handler(args):
            print(dispatch(shlex.join(args.shadow_args), terminal=True))

        ctx.register_command("shadow", dispatch, description=copy("switch.cli.help"), args_hint="<command>")
        ctx.register_cli_command("shadow", copy("switch.cli.help"), cli_setup, cli_handler)
        ctx.shadow_router_registered = True
    return service
