from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import json
from pathlib import Path
import textwrap

from .domain import IDLE_PHASES


def copy(key, **fields):
    try:
        from ..common.copy import copy as common_copy
    except ImportError:
        table = json.loads((Path(__file__).parents[1] / "copy" / "switch.json").read_text())
        table.update(json.loads(Path(__file__).with_name("fallback.json").read_text()))
        return table[key].format(**fields)
    return common_copy(key, **fields)


def short_name(model):
    try:
        from ..common.models import short_name as common_short_name
    except ImportError:
        return model
    return common_short_name(model)


def fields(record, notice=None):
    new, old = record.counts("new"), record.counts("old")
    completed = date.fromisoformat(record.completed_on) if record.completed_on else None
    completed_label = (f"{('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')[completed.month - 1]} {completed.day}"
                       if completed else "")
    values = dict(new=short_name(record.new.model), old=short_name(record.old.model), slug=record.new.model,
                  threshold=record.step_turns, new_turns=new.turns, new_trouble=new.trouble,
                  old_turns=old.turns, old_trouble=old.trouble, date=completed_label,
                  last=short_name(record.last_response_model) if record.last_response_model else copy("switch.status.no_answer"),
                  remaining=max(0, record.step_turns - (new.turns - record.step_start)))
    if record.config_pending:
        pending = getattr(record, record.config_pending)
        values.update(pending_provider=pending.provider, pending_model=pending.model)
    if notice:
        for side in ("new", "old"):
            if isinstance(notice.get(side), dict):
                values[f"{side}_turns"] = notice[side]["turns"]
                values[f"{side}_trouble"] = notice[side]["trouble"]
        if "chosen" in notice:
            values["chosen"] = short_name(notice["chosen"])
    signal_counts = Counter(s for t in record.turns.values() if t.epoch == record.epoch and t.target == "new" for s in set(t.signals))
    if signal_counts["failed_requests"]:
        signal_counts.pop("failed_turn", None)
    values["signals"] = ", ".join(copy(f"switch.signals.{signal}", count=count) for signal, count in sorted(signal_counts.items()))
    values["trouble"] = copy("switch.status.no_trouble") if not new.trouble else copy("switch.status.trouble", **values)
    return values


def render_notice(record, notice, *, terminal=False):
    kind = notice["kind"]
    values = fields(record, notice)
    key = f"switch.{kind}"
    if kind in {"rollback", "watch_trip"}:
        key += f".{notice['reason']}"
    lines = [copy(key, **values)]
    if kind == "manual" and record.config_pending:
        lines.append(copy("switch.config_interrupted", **values))
    if kind in {"step1", "step2", "step3", "resume"}:
        lines.append(copy("switch.assignment_unit", **values))
    if kind in {"step1", "watch_start", "rollback", "watch_trip"}:
        lines.append(copy("switch.signals_limit"))
    if record.mode == "all_at_once" and kind in {"watch_trip", "watch_undo"}:
        lines.append(copy("switch.current_chat_restore", slug=record.old.model))
    if record.mode == "all_at_once" and kind in {"watch_start", "watch_trip", "watch_undo"}:
        route = record.new if kind == "watch_start" else record.old
        lines.append(copy("switch.command.model_provider", slug=route.model, provider=route.provider))
    if kind in {"rollback", "watch_trip"}:
        lines.append(copy("switch.command.resume"))
    if kind not in {"undo", "undo_default", "watch_undo", "stop", "manual"}:
        lines.append(copy("switch.command.undo"))
    if terminal:
        return "\n\n".join(textwrap.fill(line, width=72, break_long_words=False, break_on_hyphens=False) for line in lines)
    return "\n".join(lines)


def _status(record):
    if record is not None and record.config_pending == "old" and record.phase in IDLE_PHASES | {"done"}:
        return copy("switch.status.restore_pending", **fields(record))
    if record is None or record.phase in IDLE_PHASES:
        text = copy("switch.status.idle")
        if record is not None and record.phase == "manual" and record.config_pending:
            text += "\n" + copy("switch.config_interrupted", **fields(record))
        return text
    values = fields(record)
    key = f"step{record.step}" if record.phase == "running" else record.phase
    return copy(f"switch.status.{key}", **values)


def status(record):
    text = _status(record)
    if record is not None and record.override:
        when = datetime.fromisoformat(record.override['at']).astimezone()
        hour = when.hour % 12 or 12
        label = f'{when:%b} {when.day}, {hour}:{when:%M} {"AM" if when.hour < 12 else "PM"}'
        text += '\n' + copy('switch.override.status', **{**record.override, 'at': label})
    return text
