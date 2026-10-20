#!/usr/bin/env python3
"""Read-only inventory of the AI Voice deployment for the live baseline.

    sudo python3 collect_inventory.py [options] > inventory.json

Run it on the Dograh box (and, with --hoppwhistle-*, where hoppwhistle runs).
It prints one JSON document. What it may print is fixed by this file:

* identifiers only: image refs and ids, git revisions, revision ids, provider
  and model ids, trunk/endpoint names, table/index names, counts, booleans;
* environment variables as set/unset, with values only for a short list of
  non-sensitive settings (trunk names, policies, flags);
* never: complete configuration JSON, environment dumps, compose dumps, cron
  command lines, phone numbers, transcripts, secret values or any hash or
  fingerprint of a secret. Shared secrets are compared in memory and reported
  as match / mismatch / missing.

Database queries run inside BEGIN READ ONLY ... ROLLBACK. Anything that cannot
be read is reported as {"status": "UNKNOWN", "reason": <fixed code>}; raw
error text is never included because it can echo values.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "cutover"))

from cutover_lib import compose
from cutover_lib.io import Psql
from cutover_lib.mounts import SHOWN_ENV

UNKNOWN = "UNKNOWN"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+=-]{0,127}$")
_PHONE = re.compile(r"(?<![0-9A-Za-z])\+?\d{10,}(?![0-9A-Za-z])")
_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_REVISION = re.compile(r"^[0-9a-z_]{1,64}$")

DOGRAH_ENV_NAMES = (
    "ARI_PJSIP_DEFAULT_TRUNK",
    "ARI_PJSIP_TRANSFER_TRUNK",
    "ARI_TRANSFER_DEFAULT_CALLER_ID",
    "ARI_DIAL_ANY_TECHNOLOGY",
    "ARI_DIAL_PREFIX",
    "ARI_DIAL_FORMAT",
    "ARI_CALLER_TRUNKS",
    "CAMPAIGN_DEFAULT_TRANSFER_DESTINATION",
    "CAMPAIGN_TRANSFER_DESTINATION_POLICY",
    "DOGRAH_TRANSFER_DESTINATION",
    "DOGRAH_ARI_TRUNK",
    "DOGRAH_ARI_TRANSFER_TRUNK",
    "DOGRAH_ARI_DIAL_PREFIX",
    "DOGRAH_ARI_DIAL_FORMAT",
    "DOGRAH_ARI_CALLER_TRUNKS",
    "DOGRAH_STATE_CID_POLICY",
    "CALL_HYGIENE_ENABLED",
    "OSS_JWT_SECRET",
    "FASTAPI_WORKERS",
    "ENABLE_TURN_LOGGING",
    "MPS_API_URL",
    "LANGFUSE_HOST",
    "DEPLOYMENT_MODE",
)
HOPPWHISTLE_ENV_NAMES = (
    "AIVOICE_URL",
    "AIVOICE_JWT_SECRET",
    "AIVOICE_COOKIE_DOMAIN",
    "AIVOICE_DEFAULT_USER_ID",
    "DOGRAH_CALLBACK_BRIDGE",
    "DOGRAH_QUALITY_INGEST_SECRET",
    "CALL_QUALITY_ENABLED",
    "CALL_QUALITY_PROVIDER",
    "CALL_QUALITY_MODEL",
)
# Non-sensitive values the inventory may show (in addition to the cutover set).
SHOWN = set(SHOWN_ENV) | {
    "FASTAPI_WORKERS",
    "ENABLE_TURN_LOGGING",
    "DEPLOYMENT_MODE",
    "CALL_QUALITY_ENABLED",
    "CALL_QUALITY_PROVIDER",
    "CALL_QUALITY_MODEL",
}


class Unavailable(Exception):
    """A source could not be read. ``code`` is from a fixed vocabulary."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def unknown(code: str) -> dict:
    return {"status": UNKNOWN, "reason": code}


def ident(value) -> object:
    """An identifier as-is, anything else replaced. Never lets a phone number,
    URL with credentials or free text through."""
    if value is None:
        return None
    if isinstance(value, (bool, int)):
        return value
    text = str(value).strip()
    if not text:
        return ""
    if _PHONE.search(text) or not _IDENTIFIER.match(text) or "://" in text:
        return "<non-identifier>"
    return text


def run_cmd(
    cmd: Sequence[str], *, input: bytes | None = None, cwd: str | None = None
) -> bytes:
    try:
        proc = subprocess.run(
            list(cmd),
            input=input,
            cwd=cwd,
            capture_output=True,
            check=False,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise Unavailable("command_not_found") from exc
    except subprocess.TimeoutExpired as exc:
        raise Unavailable("timeout") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").lower()
        if "no such container" in err or "no such object" in err or "not found" in err:
            raise Unavailable("not_found")
        if "permission denied" in err:
            raise Unavailable("permission_denied")
        raise Unavailable("command_failed")
    return proc.stdout


def section(fn: Callable[[], object]) -> object:
    try:
        return fn()
    except Unavailable as exc:
        return unknown(exc.code)
    except Exception:  # noqa: BLE001 - any failure is reported, never its text
        return unknown("error")


class Collector:
    def __init__(self, args, run: Callable[..., bytes] = run_cmd):
        self.args = args
        self.run = run

    # ---- docker helpers ----------------------------------------------------
    def _inspect(self, container: str, fmt: str) -> str:
        return self.run(["docker", "inspect", "-f", fmt, container]).decode().strip()

    def _env(self, container: str, names: Sequence[str]) -> dict[str, str]:
        raw = self._inspect(container, "{{json .Config.Env}}")
        wanted = set(names)
        out = {}
        for item in json.loads(raw or "[]") or []:
            key, _, value = item.partition("=")
            if key in wanted:
                out[key] = value
        return out

    def env_presence(self, container: str, names: Sequence[str]) -> dict:
        env = self._env(container, names)
        out = {}
        for name in names:
            if name not in env:
                out[name] = "unset"
            elif name in SHOWN:
                out[name] = {"set": True, "value": ident(env[name])}
            else:
                out[name] = "set"
        return out

    def containers(self, pattern: str) -> list:
        raw = self.run(["docker", "ps", "-a", "--format", "{{json .}}"]).decode()
        out = []
        for line in raw.splitlines():
            row = json.loads(line)
            if re.search(pattern, row.get("Names", "")):
                out.append(
                    {
                        "name": ident(row.get("Names")),
                        "image": ident(row.get("Image")),
                        "state": ident(row.get("State")),
                    }
                )
        return out

    def image(self, container: str) -> dict:
        image_id = self._inspect(container, "{{.Image}}")
        ref = self._inspect(container, "{{.Config.Image}}")
        labels = (
            json.loads(self._inspect(container, "{{json .Config.Labels}}") or "{}")
            or {}
        )
        revision = labels.get("org.opencontainers.image.revision", "")
        return {
            "ref": ident(ref),
            "id": ident(image_id),
            "revision_label": revision
            if _GIT_SHA.match(revision or "")
            else (None if not revision else "<non-identifier>"),
            "created_label": ident(labels.get("org.opencontainers.image.created")),
        }

    def pipecat(self, container: str) -> dict:
        out = (
            self.run(
                [
                    "docker",
                    "exec",
                    container,
                    "python",
                    "-c",
                    "import importlib.metadata as m; print(m.version('pipecat-ai'))",
                ]
            )
            .decode()
            .strip()
        )
        return {"version": ident(out)}

    def mounts(self, container: str) -> list:
        raw = self._inspect(container, "{{json .Mounts}}")
        out = []
        for m in json.loads(raw or "[]") or []:
            source = m.get("Source", "")
            out.append(
                {
                    "destination": ident(m.get("Destination")),
                    "source": ident(source)
                    if source.startswith("/opt/")
                    else "<other>",
                    "read_only": not m.get("RW", True),
                }
            )
        return sorted(out, key=lambda m: str(m["destination"]))

    def markers(self, container: str) -> list:
        raw = self.run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-c",
                "grep -rhoE 'HOPWHISTLE_[A-Z0-9_]+_V[0-9]+' /app/api 2>/dev/null | sort -u || true",
            ]
        )
        return sorted(
            {
                m
                for m in raw.decode().split()
                if re.fullmatch(r"HOPWHISTLE_[A-Z0-9_]+_V[0-9]+", m)
            }
        )

    def git(self, path: str) -> dict:
        if not os.path.isdir(os.path.join(path, ".git")):
            raise Unavailable("not_found")
        sha = self.run(["git", "-C", path, "rev-parse", "HEAD"]).decode().strip()
        branch = (
            self.run(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"])
            .decode()
            .strip()
        )
        dirty = (
            self.run(["git", "-C", path, "status", "--porcelain"]).decode().splitlines()
        )
        return {
            "commit": sha if _GIT_SHA.match(sha) else "<non-identifier>",
            "branch": ident(branch),
            "dirty_files": len(dirty),
        }

    def override(self, compose_dir: str) -> dict:
        path = os.path.join(compose_dir, "docker-compose.override.yaml")
        if not os.path.isfile(path):
            raise Unavailable("not_found")
        with open(path, encoding="utf-8") as handle:
            parsed = compose.parse(handle.read())
        out = {}
        for name, svc in parsed.services.items():
            out[name] = {
                "image": ident(svc.image),
                "volume_targets": sorted(ident(v.target) for v in svc.volumes),
                "env_names": sorted(ident(k) for k in svc.env),
                "env_file": svc.has_env_file,
                "unsupported_syntax": bool(svc.unsupported),
            }
        return out

    # ---- database --------------------------------------------------------------
    def dograh_psql(self) -> Psql:
        cwd = self.args.compose_dir if os.path.isdir(self.args.compose_dir) else None
        if self.args.dograh_psql:
            return Psql(json.loads(self.args.dograh_psql), cwd=cwd, run=self.run)
        user, name = "postgres", "postgres"
        try:
            env = self._env(self.args.api_container, ["DATABASE_URL"])
            match = re.match(
                r".*://([^:/@]+)(?::[^@]*)?@[^/]+/([^?]+)", env.get("DATABASE_URL", "")
            )
            if match:
                user, name = match.group(1), match.group(2)
        except Unavailable:
            pass
        return Psql(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                user,
                "-d",
                name,
            ],
            cwd=cwd,
            run=self.run,
        )

    def dograh_db(self) -> dict:
        psql = self.dograh_psql()

        def rows(sql: str) -> list[list[str]]:
            try:
                return psql.read(sql)
            except RuntimeError as exc:
                raise Unavailable("query_failed") from exc

        def q(sql: str, fields: Sequence[str], counts: Sequence[str] = ()) -> object:
            try:
                out = []
                for row in rows(sql):
                    item = {}
                    for f, v in zip(fields, row):
                        item[f] = (
                            int(v)
                            if f in counts and v.isdigit()
                            else ident(v if v != "" else None)
                        )
                    out.append(item)
                return out
            except Unavailable as exc:
                return unknown(exc.code)

        revisions = section(
            lambda: [
                r[0] if _REVISION.match(r[0]) else "<non-identifier>"
                for r in rows("SELECT version_num FROM alembic_version ORDER BY 1")
            ]
        )
        provider_paths = {
            "llm": "{byok,pipeline,llm}",
            "stt": "{byok,pipeline,stt}",
            "tts": "{byok,pipeline,tts}",
            "realtime": "{byok,realtime,realtime}",
            "realtime_llm": "{byok,realtime,llm}",
        }
        cols = ", ".join(
            f"value::jsonb #>> '{p[:-1]},provider}}', value::jsonb #>> '{p[:-1]},model}}'"
            for p in provider_paths.values()
        )
        org_fields = ["organization_id", "mode", "byok_mode"]
        for k in provider_paths:
            org_fields += [f"{k}_provider", f"{k}_model"]
        org = q(
            "SELECT organization_id, value::jsonb ->> 'mode', value::jsonb #>> '{byok,mode}', "
            f"{cols} FROM organization_configurations WHERE key = 'MODEL_CONFIGURATION_V2' ORDER BY 1",
            org_fields,
        )
        wf_fields = [
            "workflow_id",
            "status",
            "has_v2_override",
            "v2_mode",
            "v2_byok_mode",
            "legacy_is_realtime",
            "legacy_llm_provider",
            "legacy_llm_model",
            "legacy_stt_provider",
            "legacy_stt_model",
            "legacy_tts_provider",
            "legacy_tts_model",
            "legacy_realtime_provider",
            "legacy_realtime_model",
            "v2_realtime_provider",
            "v2_realtime_model",
            "turn_stop_strategy",
            "buffer_muted_speech",
        ]
        workflows = q(
            "SELECT w.id, w.status, "
            "(c ? 'model_configuration_v2_override')::text, "
            "c #>> '{model_configuration_v2_override,mode}', "
            "c #>> '{model_configuration_v2_override,byok,mode}', "
            "c #>> '{model_overrides,is_realtime}', "
            "c #>> '{model_overrides,llm,provider}', c #>> '{model_overrides,llm,model}', "
            "c #>> '{model_overrides,stt,provider}', c #>> '{model_overrides,stt,model}', "
            "c #>> '{model_overrides,tts,provider}', c #>> '{model_overrides,tts,model}', "
            "c #>> '{model_overrides,realtime,provider}', c #>> '{model_overrides,realtime,model}', "
            "c #>> '{model_configuration_v2_override,byok,realtime,realtime,provider}', "
            "c #>> '{model_configuration_v2_override,byok,realtime,realtime,model}', "
            "c ->> 'turn_stop_strategy', c ->> 'buffer_muted_speech' "
            "FROM workflows w LEFT JOIN workflow_definitions d ON d.id = w.released_definition_id "
            "CROSS JOIN LATERAL (SELECT COALESCE(d.workflow_configurations, w.workflow_configurations)::jsonb AS c) x "
            "ORDER BY w.id",
            wf_fields,
        )
        rc = "initial_context::jsonb #>> '{runtime_configuration,%s}'"
        keys = [
            "realtime_provider",
            "realtime_model",
            "stt_provider",
            "stt_model",
            "tts_provider",
            "tts_model",
            "llm_provider",
            "llm_model",
        ]
        runs = q(
            "SELECT workflow_id, " + ", ".join(rc % k for k in keys) + ", count(*) "
            "FROM workflow_runs WHERE created_at > now() - interval '7 days' "
            "GROUP BY 1, "
            + ", ".join(str(i) for i in range(2, len(keys) + 2))
            + " ORDER BY 1",
            ["workflow_id", *keys, "runs"],
            counts=["runs"],
        )
        tools = q(
            "SELECT category::text, COALESCE(definition::jsonb #>> '{config,destination_source}', ''), "
            "(definition::text LIKE '%PJSIP/hopwhistle%')::text, count(*) FROM tools GROUP BY 1, 2, 3 ORDER BY 1, 2",
            ["category", "destination_source", "direct_transfer_rewritten", "tools"],
            counts=["tools"],
        )
        numbers = q(
            "SELECT tc.provider, count(*) FILTER (WHERE p.inbound_workflow_id IS NOT NULL), count(*), "
            "count(*) FILTER (WHERE p.extra_metadata::jsonb ->> 'pool' = 'state_cid') "
            "FROM telephony_phone_numbers p JOIN telephony_configurations tc ON tc.id = p.telephony_configuration_id "
            "GROUP BY 1 ORDER BY 1",
            ["provider", "with_inbound_workflow", "numbers", "state_cid_pool"],
            counts=["with_inbound_workflow", "numbers", "state_cid_pool"],
        )
        indexes = q(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'workflow_runs' ORDER BY 1",
            ["index"],
        )
        invalid = q(
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE NOT i.indisvalid ORDER BY 1",
            ["index"],
        )
        return {
            "alembic_version": revisions,
            "org_model_configuration": org,
            "workflows": workflows,
            "runs_last_7_days_by_model": runs,
            "tools": tools,
            "phone_numbers": numbers,
            "workflow_runs_indexes": indexes,
            "invalid_indexes": invalid,
        }

    # ---- host ------------------------------------------------------------------
    def asterisk(self) -> dict:
        c = self.args.asterisk_container
        blocks = self.run(
            [
                "docker",
                "exec",
                c,
                "sh",
                "-c",
                "grep -hoE '>>> HOPWHISTLE [A-Z ]+' /etc/asterisk/pjsip.conf /etc/asterisk/extensions.conf 2>/dev/null || true",
            ]
        )
        endpoints = self.run(
            ["docker", "exec", c, "asterisk", "-rx", "pjsip show endpoints"]
        ).decode()
        names = sorted(
            {
                m
                for m in re.findall(
                    r"^\s*Endpoint:\s+(\S+?)(?:/\S*)?\s", endpoints, flags=re.MULTILINE
                )
                if m != "<Endpoint/CID.....................................>"
            }
        )
        return {
            "hopwhistle_blocks": sorted(
                {
                    b.replace(">>> ", "").strip()
                    for b in blocks.decode().splitlines()
                    if b.strip()
                }
            ),
            "pjsip_endpoints": [ident(n) for n in names],
        }

    def nginx(self) -> dict:
        text = self.run(["nginx", "-T"]).decode(errors="replace")
        names = sorted(
            {
                n
                for line in re.findall(r"server_name\s+([^;]+);", text)
                for n in line.split()
                if "aivoice" in n
            }
        )
        return {
            "aivoice_server_names": [ident(n) for n in names],
            "x_frame_options_present": "X-Frame-Options" in text,
            "frame_ancestors_present": "frame-ancestors" in text,
        }

    def schedules(self) -> dict:
        def contains(cmd: Sequence[str]) -> object:
            try:
                return "push_runs" in self.run(cmd).decode(errors="replace")
            except Unavailable as exc:
                return unknown(exc.code)

        return {
            "push_runs_in_root_crontab": contains(["crontab", "-l"]),
            "push_runs_in_cron_d": contains(
                ["sh", "-c", "cat /etc/cron.d/* 2>/dev/null || true"]
            ),
            "push_runs_systemd_unit": contains(
                [
                    "sh",
                    "-c",
                    "systemctl cat '*' 2>/dev/null | grep -c push_runs || true",
                ]
            ),
        }

    # ---- hoppwhistle -------------------------------------------------------------
    def hoppwhistle_db(self) -> dict:
        psql = Psql(json.loads(self.args.hoppwhistle_psql), run=self.run)

        def count(sql):
            try:
                return psql.count(sql)
            except RuntimeError as exc:
                raise Unavailable("query_failed") from exc

        def rows(sql):
            try:
                return psql.read(sql)
            except RuntimeError as exc:
                raise Unavailable("query_failed") from exc

        return {
            "external_agent_bindings": section(
                lambda: [
                    {
                        "source_type": ident(r[0]),
                        "active": ident(r[1]),
                        "bindings": int(r[2]),
                    }
                    for r in rows(
                        'SELECT "sourceType"::text, active::text, count(*) FROM external_agent_bindings GROUP BY 1, 2 ORDER BY 1, 2'
                    )
                ]
            ),
            "open_quarantined_runs": section(
                lambda: count(
                    'SELECT count(*) FROM call_quality_quarantine WHERE "resolvedAt" IS NULL'
                )
            ),
            "latest_external_call_record_date": section(
                lambda: ident(
                    (
                        rows(
                            "SELECT to_char(max(\"receivedAt\"), 'YYYY-MM-DD') FROM external_call_records"
                        )
                        or [[""]]
                    )[0][0]
                    or None
                )
            ),
            "callback_numbers": section(
                lambda: count(
                    "SELECT count(*) FROM phone_numbers WHERE metadata::jsonb ->> 'dograhCallback' = 'true'"
                )
            ),
        }

    # ---- secret comparison ---------------------------------------------------------
    def compare_secret(self) -> str:
        """match / mismatch / missing. Values stay in this process."""
        try:
            ours = self._env(self.args.api_container, ["OSS_JWT_SECRET"]).get(
                "OSS_JWT_SECRET"
            )
        except Unavailable as exc:
            return f"{UNKNOWN}:{exc.code}"
        if self.args.peer_secret_stdin:
            theirs = sys.stdin.readline().rstrip("\n") or None
        elif self.args.hoppwhistle_api_container:
            try:
                theirs = self._env(
                    self.args.hoppwhistle_api_container, ["AIVOICE_JWT_SECRET"]
                ).get("AIVOICE_JWT_SECRET")
            except Unavailable as exc:
                return f"{UNKNOWN}:{exc.code}"
        else:
            return f"{UNKNOWN}:hoppwhistle_not_on_this_host"
        if not ours or not theirs:
            return "missing"
        return (
            "match"
            if hmac.compare_digest(ours.encode(), theirs.encode())
            else "mismatch"
        )

    # ---- report ----------------------------------------------------------------------
    def collect(self) -> dict:
        a = self.args
        report: dict = {"schema": 1, "tool": "collect_inventory"}
        report["dograh"] = {
            "source_checkout": section(lambda: self.git(a.dograh_src)),
            "containers": section(lambda: self.containers(r"dograh")),
            "api_image": section(lambda: self.image(a.api_container)),
            "ui_image": section(lambda: self.image(a.ui_container)),
            "pipecat": section(lambda: self.pipecat(a.api_container)),
            "api_mounts": section(lambda: self.mounts(a.api_container)),
            "api_patch_markers": section(lambda: self.markers(a.api_container)),
            "api_environment": section(
                lambda: self.env_presence(a.api_container, DOGRAH_ENV_NAMES)
            ),
            "compose_override": section(lambda: self.override(a.compose_dir)),
            "database": section(self.dograh_db)
            if not a.skip_db
            else unknown("skipped"),
            "asterisk": section(self.asterisk),
            "nginx": section(self.nginx),
            "schedules": section(self.schedules),
        }
        hw: dict = {"source_checkout": section(lambda: self.git(a.hoppwhistle_dir))}
        if a.hoppwhistle_api_container:
            hw["containers"] = section(lambda: self.containers(r"hopwhistle"))
            hw["api_environment"] = section(
                lambda: self.env_presence(
                    a.hoppwhistle_api_container, HOPPWHISTLE_ENV_NAMES
                )
            )
        else:
            hw["containers"] = unknown("not_on_this_host")
            hw["api_environment"] = unknown("not_on_this_host")
        hw["database"] = (
            section(self.hoppwhistle_db)
            if a.hoppwhistle_psql
            else unknown("not_configured")
        )
        report["hoppwhistle"] = hw
        report["jwt_secret_parity"] = self.compare_secret()
        return enforce_safe(report)


def enforce_safe(value):
    """Final pass: every string must be short and contain no phone-number-like
    run. Raises rather than emit something unsafe."""
    if isinstance(value, dict):
        return {str(k): enforce_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [enforce_safe(v) for v in value]
    if isinstance(value, str):
        if _PHONE.search(value):
            raise ValueError("refusing to emit a phone-number-like value")
        if len(value) > 200:
            raise ValueError("refusing to emit a long free-text value")
    return value


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--compose-dir", default="/opt/dograh")
    p.add_argument("--dograh-src", default="/opt/dograh-src")
    p.add_argument("--api-container", default="dograh-api-1")
    p.add_argument("--ui-container", default="dograh-ui-1")
    p.add_argument("--asterisk-container", default="dograh-asterisk")
    p.add_argument(
        "--dograh-psql",
        help="JSON argv for psql against the dograh DB (default: compose postgres)",
    )
    p.add_argument("--skip-db", action="store_true")
    p.add_argument("--hoppwhistle-dir", default="/opt/hopwhistle")
    p.add_argument(
        "--hoppwhistle-api-container",
        default="",
        help="e.g. hopwhistle-api, when on this host",
    )
    p.add_argument(
        "--hoppwhistle-psql",
        default="",
        help='JSON argv, e.g. ["docker","exec","-i","hopwhistle-postgres-dev","psql","-U","callfabric","-d","callfabric"]',
    )
    p.add_argument(
        "--peer-secret-stdin",
        action="store_true",
        help="read hoppwhistle's AIVOICE_JWT_SECRET from stdin (piped over ssh) for comparison",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    report = Collector(parse_args(argv)).collect()
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
