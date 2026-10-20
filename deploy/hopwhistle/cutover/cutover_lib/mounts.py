"""Decide which bind mounts the cutover may drop, and what replaces them.

A mount over the api image is only removed when the behavior it carries is
known (mount_manifest.json), present in the target image, and, where it was
patch-only, reproduced by explicit environment settings derived here. Anything
else blocks the plan with a reason an operator can act on.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

# Environment the planner may read from the running api container. Nothing
# else is ever read, and values are never printed (see SHOWN_ENV).
READ_ENV = (
    "ARI_PJSIP_DEFAULT_TRUNK",
    "ARI_PJSIP_TRANSFER_TRUNK",
    "ARI_TRANSFER_DEFAULT_CALLER_ID",
    "ARI_DIAL_ANY_TECHNOLOGY",
    "ARI_DIAL_PREFIX",
    "ARI_DIAL_FORMAT",
    "ARI_CALLER_TRUNKS",
    "CAMPAIGN_DEFAULT_TRANSFER_DESTINATION",
    "CAMPAIGN_TRANSFER_DESTINATION_POLICY",
    "CALL_HYGIENE_ENABLED",
    "DOGRAH_STATE_CID_POLICY",
    "DOGRAH_ARI_TRUNK",
    "DOGRAH_ARI_TRANSFER_TRUNK",
    "DOGRAH_ARI_DIAL_PREFIX",
    "DOGRAH_ARI_DIAL_FORMAT",
    "DOGRAH_ARI_CALLER_TRUNKS",
    "DOGRAH_TRANSFER_DESTINATION",
)

# Values safe to show in a plan: trunk names, policies, flags. Everything
# else (caller IDs, destinations, dial prefixes, caller-trunk lists) is shown
# as <set>.
SHOWN_ENV = {
    "ARI_PJSIP_DEFAULT_TRUNK",
    "ARI_PJSIP_TRANSFER_TRUNK",
    "ARI_DIAL_ANY_TECHNOLOGY",
    "ARI_DIAL_FORMAT",
    "CAMPAIGN_TRANSFER_DESTINATION_POLICY",
    "CALL_HYGIENE_ENABLED",
    "DOGRAH_STATE_CID_POLICY",
}

# Patch-era names the target image no longer reads.
PATCH_ONLY_ENV = (
    "DOGRAH_ARI_TRUNK",
    "DOGRAH_ARI_TRANSFER_TRUNK",
    "DOGRAH_ARI_DIAL_PREFIX",
    "DOGRAH_ARI_DIAL_FORMAT",
    "DOGRAH_ARI_CALLER_TRUNKS",
    "DOGRAH_TRANSFER_DESTINATION",
)


def shown(name: str, value: str | None) -> str:
    if value is None:
        return "<unset>"
    if value == "":
        return "<empty>"
    return value if name in SHOWN_ENV else "<set>"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DerivationError(ValueError):
    pass


@dataclass
class Context:
    mounted_text: str
    container_env: dict[str, str]


def _env(ctx: Context, name: str) -> str:
    return ctx.container_env.get(name, "").strip()


def _one(pattern: str, text: str, what: str) -> str:
    found = sorted(set(re.findall(pattern, text)))
    if len(found) != 1:
        raise DerivationError(
            f"expected one {what} in the mounted file, found {len(found)}"
        )
    return found[0]


def _trunk_from_patch_env(ctx: Context) -> dict[str, str]:
    out = {
        "ARI_PJSIP_DEFAULT_TRUNK": _env(ctx, "DOGRAH_ARI_TRUNK") or "fractel",
    }
    transfer = _env(ctx, "DOGRAH_ARI_TRANSFER_TRUNK")
    if transfer and transfer != out["ARI_PJSIP_DEFAULT_TRUNK"]:
        out["ARI_PJSIP_TRANSFER_TRUNK"] = transfer
    return out


def _literal_trunk(ctx: Context) -> dict[str, str]:
    outbound = _one(
        r'f"PJSIP/\{to_number\}@([A-Za-z0-9_.-]+)"',
        ctx.mounted_text,
        "outbound trunk literal",
    )
    out = {"ARI_PJSIP_DEFAULT_TRUNK": outbound}
    transfers = re.findall(
        r'f"PJSIP/\{destination\}@([A-Za-z0-9_.-]+)"', ctx.mounted_text
    )
    if len(set(transfers)) > 1:
        raise DerivationError(
            "more than one transfer trunk literal in the mounted file"
        )
    if transfers and transfers[0] != outbound:
        out["ARI_PJSIP_TRANSFER_TRUNK"] = transfers[0]
    return out


def _copy(source: str, target: str) -> Callable[[Context], dict[str, str]]:
    def derive(ctx: Context) -> dict[str, str]:
        value = _env(ctx, source)
        return {target: value} if value else {}

    return derive


def _transfer_caller_id_literal(ctx: Context) -> dict[str, str]:
    value = _one(
        r'kwargs\.get\("caller_id"\)\s*or\s*"(\+?\d+)"',
        ctx.mounted_text,
        "transfer caller ID literal",
    )
    return {"ARI_TRANSFER_DEFAULT_CALLER_ID": value}


def _forced_shared_did(ctx: Context) -> dict[str, str]:
    value = _env(ctx, "DOGRAH_TRANSFER_DESTINATION")
    if not value:
        literals = re.findall(
            r'SHARED_TRANSFER_DID\s*=\s*os\.environ\.get\("DOGRAH_TRANSFER_DESTINATION"\)\s*or\s*"(\+?\d+)"',
            ctx.mounted_text,
        )
        if len(set(literals)) != 1:
            raise DerivationError(
                "the shared transfer DID is neither in DOGRAH_TRANSFER_DESTINATION nor a single "
                "literal in the mounted file; pass --set CAMPAIGN_DEFAULT_TRANSFER_DESTINATION=..."
            )
        value = literals[0]
    return {
        "CAMPAIGN_TRANSFER_DESTINATION_POLICY": "forced_default",
        "CAMPAIGN_DEFAULT_TRANSFER_DESTINATION": value,
    }


def _context_then_literal(ctx: Context) -> dict[str, str]:
    block = re.search(
        r'"transfer_destination":\s*\(((?:[^\n]*\n){0,4}?[^\n]*"\+?\d{7,}")',
        ctx.mounted_text,
    )
    if not block:
        raise DerivationError("transfer destination fallback literal not found")
    literal = _one(r'"(\+?\d{7,})"', block.group(1), "transfer destination literal")
    return {
        "CAMPAIGN_TRANSFER_DESTINATION_POLICY": "context_first",
        "CAMPAIGN_DEFAULT_TRANSFER_DESTINATION": literal,
    }


def _context_first(ctx: Context) -> dict[str, str]:
    # A mounted copy of the source resolver: keep its (default) policy unless
    # the running environment already chose one.
    return {
        "CAMPAIGN_TRANSFER_DESTINATION_POLICY": _env(
            ctx, "CAMPAIGN_TRANSFER_DESTINATION_POLICY"
        )
        or "context_first"
    }


DERIVATIONS: dict[str, Callable[[Context], dict[str, str]]] = {
    "ari_trunk_from_patch_env": _trunk_from_patch_env,
    "ari_any_technology": lambda ctx: {"ARI_DIAL_ANY_TECHNOLOGY": "true"},
    "ari_dial_prefix": _copy("DOGRAH_ARI_DIAL_PREFIX", "ARI_DIAL_PREFIX"),
    "ari_dial_format": _copy("DOGRAH_ARI_DIAL_FORMAT", "ARI_DIAL_FORMAT"),
    "ari_caller_trunks": _copy("DOGRAH_ARI_CALLER_TRUNKS", "ARI_CALLER_TRUNKS"),
    "ari_literal_trunk": _literal_trunk,
    "ari_transfer_caller_id_literal": _transfer_caller_id_literal,
    "transfer_forced_shared_did": _forced_shared_did,
    "transfer_context_then_literal": _context_then_literal,
    "transfer_context_first": _context_first,
}


@dataclass
class MountResult:
    target: str
    source: str
    action: str  # remove | keep | block
    sha256: str | None = None
    target_sha256: str | None = None
    variants: list[str] = field(default_factory=list)
    behaviors: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class MountEvaluation:
    results: list[MountResult]
    env: dict[str, str]
    env_origin: dict[str, str]
    blockers: list[str]
    warnings: list[str]


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    for target, entry in manifest["mounts"].items():
        for variant in entry["variants"]:
            for behavior in variant["behaviors"]:
                if behavior not in manifest["behaviors"]:
                    raise ValueError(f"{target}: unknown behavior {behavior}")
            for pattern in variant["detect"]:
                re.compile(pattern)
    for name, behavior in manifest["behaviors"].items():
        if behavior["status"] == "config" and name not in DERIVATIONS:
            raise ValueError(f"behavior {name} has status config but no derivation")
    return manifest


def evaluate(
    manifest: dict,
    mounts: list,
    read_host: Callable[[str], str | None],
    target_files: dict[str, str | None],
    container_env: dict[str, str],
    acks: dict[str, str],
    operator_env: dict[str, str],
) -> MountEvaluation:
    """``mounts``: compose.Volume list of the api service. ``target_files``:
    every path the manifest may need, read from the target image (None when
    absent). ``acks``: target path -> sha256 the operator reviewed."""
    results: list[MountResult] = []
    blockers: list[str] = []
    warnings: list[str] = []
    derived: dict[str, tuple[str, str]] = {}  # name -> (value, origin)

    for mount in mounts:
        res = MountResult(target=mount.target, source=mount.source, action="block")
        results.append(res)
        entry = manifest["mounts"].get(mount.target)
        if not mount.is_bind:
            res.reasons.append(
                "named volume or non-path source; not handled by the manifest"
            )
        elif entry is None:
            res.reasons.append(
                "unknown mount: no entry in mount_manifest.json. Diff the mounted file against "
                "the target image (cutover_plan.py diff), then add an entry describing what it carries"
            )
        elif entry["action"] == "keep":
            res.action = "keep"
            continue
        if res.reasons:
            blockers.append(f"{mount.target}: {res.reasons[-1]}")
            continue

        text = read_host(mount.source)
        if text is None:
            res.reasons.append(f"cannot read mounted file {mount.source} on the host")
            blockers.append(f"{mount.target}: {res.reasons[-1]}")
            continue
        res.sha256 = sha256(text)
        target_text = target_files.get(mount.target)
        res.target_sha256 = sha256(target_text) if target_text is not None else None
        if target_text is not None and target_text == text:
            res.action = "remove"
            res.reasons.append("identical to the file in the target image")
            continue

        matched = [
            v
            for v in entry["variants"]
            if all(re.search(p, text, flags=re.MULTILINE) for p in v["detect"])
        ]
        res.variants = [v["id"] for v in matched]
        acked = acks.get(mount.target) == res.sha256
        problems: list[str] = []
        if not matched:
            if acked:
                res.action = "remove"
                res.reasons.append(
                    "unrecognised content removed on operator acknowledgement"
                )
                warnings.append(
                    f"{mount.target}: removed on acknowledgement only; no behavior carried over"
                )
                continue
            problems.append(
                "content not recognised by any manifest variant; review it with "
                f"`cutover_plan.py diff {mount.target}` and add a variant, or acknowledge with "
                f"--ack-mount {mount.target}={res.sha256}"
            )
        ids = set(res.variants)
        required = entry.get("require_one_of")
        if matched and required and not ids & set(required):
            problems.append(f"none of the expected base variants {required} matched")
        for pair in entry.get("exclusive", []):
            if set(pair) <= ids:
                problems.append(
                    f"variants {pair} cannot both apply; the file is not a known combination"
                )
        for v in matched:
            if v.get("requires_ack") and not acked:
                problems.append(
                    f"variant {v['id']} is recognised heuristically; review the diff and acknowledge with "
                    f"--ack-mount {mount.target}={res.sha256}"
                )
        for v in matched:
            for behavior_id in v["behaviors"]:
                if behavior_id in res.behaviors:
                    continue
                res.behaviors.append(behavior_id)
                behavior = manifest["behaviors"][behavior_id]
                if behavior["status"] == "unported":
                    problems.append(
                        f"behavior {behavior_id} is not in source: {behavior['description']}"
                    )
                    continue
                for ev in behavior.get("evidence", []):
                    body = target_files.get(ev["path"])
                    if body is None or not re.search(
                        ev["pattern"], body, flags=re.MULTILINE
                    ):
                        problems.append(
                            f"target image lacks {behavior_id} (no /{ev['pattern']}/ in {ev['path']})"
                        )
                if behavior["status"] == "config":
                    try:
                        values = DERIVATIONS[behavior_id](Context(text, container_env))
                    except DerivationError as exc:
                        if all(n in operator_env for n in behavior["env"]):
                            values = {}
                        else:
                            problems.append(f"{behavior_id}: {exc}")
                            continue
                    for name, value in values.items():
                        prior = derived.get(name)
                        if prior and prior[0] != value:
                            problems.append(
                                f"{name} derived twice with different values ({prior[1]} and {behavior_id})"
                            )
                        derived[name] = (value, behavior_id)
        if problems:
            res.reasons.extend(problems)
            blockers.extend(f"{mount.target}: {p}" for p in problems)
        else:
            res.action = "remove"

    env = {name: value for name, (value, _) in derived.items()}
    origin = {name: f"derived:{o}" for name, (_, o) in derived.items()}
    for name, value in operator_env.items():
        if name in env and env[name] != value:
            warnings.append(
                f"{name}: operator value replaces the derived one ({origin[name]})"
            )
        env[name] = value
        origin[name] = "operator"
    for name, value in env.items():
        current = container_env.get(name)
        if current is not None and current != value:
            warnings.append(
                f"{name}: changes from the running value ({shown(name, current)} -> {shown(name, value)})"
            )
    for name in PATCH_ONLY_ENV:
        if name in container_env:
            warnings.append(
                f"{name} is set but the target image ignores it (patch-era name)"
            )
    return MountEvaluation(results, env, origin, blockers, warnings)
