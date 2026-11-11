from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import shlex

from .copy import copy
from .reasoning import matching_proof


@dataclass(frozen=True)
class Permission:
    setting: str
    capability: str | None = None


PERMISSIONS = (
    Permission("plugins.entries.shadow.llm.allow_provider_override", "llm.provider_override"),
    Permission("plugins.entries.shadow.llm.allow_model_override", "llm.model_override"),
)
REQUIRED_SETTINGS = tuple(permission.setting for permission in PERMISSIONS)


@dataclass(frozen=True)
class Missing:
    setting: str
    fix: str


def hermes_command(profile_name=None):
    if profile_name and profile_name not in {"default", "custom"}:
        return copy("switch.doctor.profile_command", profile=shlex.quote(profile_name))
    return copy("switch.doctor.command")


def permission_granted(config, permission):
    if permission.capability:
        try:
            from hermes_cli.plugin_capabilities import plugin_capability_granted
        except ImportError:
            pass
        else:
            return plugin_capability_granted("shadow", permission.capability, config=config)
    value = config
    for segment in permission.setting.split("."):
        value = value.get(segment) if isinstance(value, dict) else None
    return bool(value) if permission.capability else value is True


def inspect_setup(config: dict, *, other_model: bool, judge: bool, profile_name=None) -> list[Missing]:
    command = hermes_command(profile_name)
    missing = []
    for permission in PERMISSIONS:
        if not permission_granted(config, permission):
            fix = (copy("switch.doctor.capability_fix", command=command) if permission.capability
                   else copy("switch.doctor.setting_fix", command=command, setting=permission.setting))
            missing.append(Missing(permission.setting, fix))
    if not other_model:
        missing.append(Missing(copy("switch.doctor.other_model"), copy("switch.doctor.model_fix", command=command)))
    if not judge:
        missing.append(Missing(copy("switch.doctor.judge"), copy("switch.doctor.judge_fix", command=command)))
    return missing


def host_version(command):
    requirement = ">=0.23.1"
    try:
        manifest = Path(__file__).resolve().parents[2] / "plugin.yaml"
        requirement = next((line.partition(":")[2].strip().strip("\"'")
                            for line in manifest.read_text().splitlines() if line.startswith("requires_hermes:")), requirement)
    except (OSError, ValueError):
        pass
    try:
        from hermes_cli.plugins_manifest import running_hermes_version, version_satisfies
        version = running_hermes_version()
        if not version or version == "unknown":
            raise ValueError("unknown Hermes version")
        compatible = version_satisfies(requirement, version)
    except (ImportError, OSError, ValueError):
        return copy("switch.doctor.version_unknown", requirement=requirement, command=command)
    key = "switch.doctor.version_ok" if compatible else "switch.doctor.version_old"
    return copy(key, version=version, requirement=requirement, command=command)


def setup_doctor(config: dict, *, catalog, profile_name="default") -> str:
    from ..common.scrub import Scrubber

    command = hermes_command(profile_name)
    scrubber = Scrubber()
    lines = [host_version(command), copy("switch.doctor.profile", profile=scrubber.text(profile_name))]
    candidates = [] if catalog.failure else catalog.candidates
    lines.append(copy("switch.doctor.models"))
    lines.extend(copy("switch.doctor.model", provider=scrubber.text(model.provider), model=scrubber.text(model.model))
                 for model in candidates)
    if not candidates:
        lines.append(copy("switch.doctor.no_models", command=command))
    if catalog.failure:
        lines.append(copy("switch.doctor.discovery_unavailable", command=command))
    if any(provider and model for provider, model in catalog.reasons):
        lines.append(copy("switch.doctor.credentials_unknown", command=command))
    current = catalog.current
    current_usable = any((model.provider, model.model) == (current.provider, current.model) for model in candidates)
    others = [model for model in candidates if (model.provider, model.model) != (current.provider, current.model)]
    judge = current_usable and bool(current.maker) and any(
        other.maker and any(model.maker and model.maker not in {current.maker, other.maker} for model in candidates)
        for other in others)
    lines.append(copy("switch.doctor.judge_available" if judge else "doctor.judge"))
    for permission in PERMISSIONS:
        state = copy("switch.doctor.granted" if permission_granted(config, permission) else "switch.doctor.not_granted")
        lines.append(copy("switch.doctor.permission", permission=permission.capability or permission.setting, state=state))
    missing = inspect_setup(config, other_model=current_usable and bool(others), judge=judge, profile_name=profile_name)
    lines.extend(copy("switch.doctor.missing", setting=item.setting, fix=item.fix) for item in missing)
    lines.append(copy("switch.doctor.unchanged"))
    return "\n\n".join(lines)


def doctor(config: dict, *, old=None, new=None, catalog=None, profile_name="default") -> str:
    if catalog is None:
        from ..common.discovery import discover_models
        catalog = discover_models()
    setup = setup_doctor(config, catalog=catalog, profile_name=profile_name)
    return "\n\n".join(filter(None, (setup, pair_doctor(config, old=old, new=new, profile_name=profile_name))))


def pair_doctor(config: dict, *, old=None, new=None, profile_name="default") -> str:
    command = hermes_command(profile_name)
    lines = []
    settings = config.get("plugins", {}).get("entries", {}).get("shadow", {}).get("settings", {})
    proofs = settings.get("verified_gradual_pairs", [])
    if old is not None and new is not None and old.same_wire(new):
        matched = matching_proof(config, old, new)
        if matched:
            lines.append(copy("switch.doctor.pair_ready"))
        else:
            template = {"old": asdict(old), "new": asdict(new), "probe_passed": False, "evidence": ""}
            lines.append(copy("switch.doctor.pair_missing", entry=json.dumps(template), command=command))
    elif not proofs:
        lines.append(copy("switch.doctor.pair_registry", command=command))
    return "\n\n".join(lines)
