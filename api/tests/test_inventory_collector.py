"""The live-baseline collector prints identifiers only, whatever its sources
contain."""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "deploy" / "hopwhistle" / "inventory"))

import collect_inventory as ci

SECRET = "jwt-SECRET-value-123"
PHONE = "+15551234567"

API_ENV = [
    f"OSS_JWT_SECRET={SECRET}",
    "DATABASE_URL=postgresql+asyncpg://dograh:db-PASSWORD@postgres:5432/dograh",
    "ARI_PJSIP_DEFAULT_TRUNK=fractel",
    f"ARI_TRANSFER_DEFAULT_CALLER_ID={PHONE}",
    f"CAMPAIGN_DEFAULT_TRANSFER_DESTINATION={PHONE}",
    "CAMPAIGN_TRANSFER_DESTINATION_POLICY=forced_default",
    "OPENAI_API_KEY=sk-SECRET",
    "FASTAPI_WORKERS=4",
]
HW_ENV = [
    f"AIVOICE_JWT_SECRET={SECRET}",
    "AIVOICE_URL=https://user:pw@aivoice.example",
    "CALL_QUALITY_ENABLED=true",
]


class FakeRunner:
    def __init__(self, hw_secret=SECRET, fail=()):
        self.hw_env = [
            e
            if not e.startswith("AIVOICE_JWT_SECRET=")
            else f"AIVOICE_JWT_SECRET={hw_secret}"
            for e in HW_ENV
        ]
        if hw_secret is None:
            self.hw_env = [e for e in HW_ENV if not e.startswith("AIVOICE_JWT_SECRET=")]
        self.fail = fail
        self.calls = []

    def __call__(self, cmd, input=None, cwd=None):
        self.calls.append((list(cmd), input))
        joined = " ".join(cmd)
        for needle in self.fail:
            if needle in joined:
                raise ci.Unavailable("command_failed")
        if cmd[:2] == ["docker", "inspect"]:
            fmt, target = cmd[3], cmd[4]
            if fmt == "{{json .Config.Env}}":
                return json.dumps(
                    self.hw_env if "hopwhistle" in target else API_ENV
                ).encode()
            if fmt == "{{.Image}}":
                return b"sha256:" + b"ab" * 32
            if fmt == "{{.Config.Image}}":
                return b"hopwhistle/dograh-api:abc12345"
            if fmt == "{{json .Config.Labels}}":
                return json.dumps(
                    {
                        "org.opencontainers.image.revision": "9bf14859a60be276e04e52ac30211432f2a7e073",
                        "maintainer": f"call {PHONE}",
                    }
                ).encode()
            if fmt == "{{json .Mounts}}":
                return json.dumps(
                    [
                        {
                            "Source": "/opt/dograh-patches/provider.py",
                            "Destination": "/app/api/x.py",
                            "RW": False,
                        },
                        {
                            "Source": "/home/someone/secret-dir",
                            "Destination": "/data",
                            "RW": True,
                        },
                    ]
                ).encode()
        if cmd[:3] == ["docker", "ps", "-a"]:
            rows = [
                {
                    "Names": "dograh-api-1",
                    "Image": "img:1",
                    "State": "running",
                    "Command": f"sh -c 'curl {PHONE}'",
                },
                {"Names": "other", "Image": "x", "State": "running"},
            ]
            return "\n".join(json.dumps(r) for r in rows).encode()
        if cmd[:2] == ["docker", "exec"] and "python" in cmd:
            return b"0.0.0-dev\n"
        if cmd[:2] == ["docker", "exec"] and "grep -rhoE" in joined:
            return b"HOPWHISTLE_FISH_TTS_V1\nHOPWHISTLE_FISH_TTS_V1\nnot-a-marker\n"
        if cmd[:2] == ["docker", "exec"] and ">>> HOPWHISTLE" in joined:
            return b">>> HOPWHISTLE DIRECT TRANSFER\n>>> HOPWHISTLE AI CALLBACK\n"
        if "pjsip show endpoints" in joined:
            return (
                b" Endpoint:  <Endpoint/CID.....................................>  <State.....>\n"
                b" Endpoint:  fractel/+15551234567   Not in use    0 of inf\n"
                b" Endpoint:  hopwhistle   Not in use    0 of inf\n"
            )
        if cmd[0] == "nginx":
            return f"server_name aivoice.hopwhistle.com; # owner {PHONE}\nadd_header X-Frame-Options SAMEORIGIN;\n".encode()
        if cmd[:2] == ["crontab", "-l"]:
            return f"*/5 * * * * docker exec dograh-api-1 python /tmp/cq/push_runs.py --secret {SECRET}\n".encode()
        if cmd[0] == "sh":
            return b""
        if cmd[0] == "git":
            raise ci.Unavailable("not_found")
        if "psql" in cmd:
            return self.psql(input.decode())
        raise ci.Unavailable("command_failed")

    def psql(self, sql):
        if "alembic_version" in sql:
            return b"00b0201ad918\n"
        if "organization_configurations" in sql:
            return (
                f"1\tbyok\tpipeline\topenai\tgpt-4.1\tdeepgram\t{PHONE}\televenlabs\t"
                "https://u:p@host/model\t\t\t\t\t\t\n"
            ).encode()
        if "FROM workflows" in sql:
            return b"7\tactive\ttrue\tbyok\trealtime\t\t\t\t\t\t\t\t\t\topenai_live\tgpt-live-1\t\t\n"
        if "workflow_runs WHERE created_at" in sql:
            return b"7\topenai_live\tgpt-live-1\t\t\t\t\topenai\tgpt-4.1\t12\n"
        if "FROM tools" in sql:
            return b"transfer_call\tstatic\ttrue\t2\n"
        if "telephony_phone_numbers" in sql:
            return b"ari\t3\t5\t2\n"
        if "pg_indexes" in sql or "indisvalid" in sql:
            return b"idx_workflow_runs_call_id\n"
        if "external_agent_bindings" in sql:
            return b"DOGRAH\ttrue\t2\n"
        if "count(*)" in sql:
            return b"4\n"
        if "to_char" in sql:
            return b"2026-10-19\n"
        return b""


def collect(runner, *argv):
    args = ci.parse_args(
        [
            "--compose-dir",
            "/nonexistent",
            "--hoppwhistle-api-container",
            "hopwhistle-api",
            "--hoppwhistle-psql",
            json.dumps(["docker", "exec", "-i", "hw-pg", "psql"]),
            *argv,
        ]
    )
    return ci.Collector(args, run=runner).collect()


def test_report_contains_identifiers_and_no_secrets_or_numbers():
    runner = FakeRunner()
    report = collect(runner)
    text = json.dumps(report)
    for leaked in (
        SECRET,
        "db-PASSWORD",
        "sk-SECRET",
        "user:pw",
        "u:p@",
        "15551234567",
        "push_runs.py --secret",
        "curl",
    ):
        assert leaked not in text, leaked
    d = report["dograh"]
    assert (
        d["api_image"]["revision_label"] == "9bf14859a60be276e04e52ac30211432f2a7e073"
    )
    assert d["api_environment"]["OSS_JWT_SECRET"] == "set"
    assert d["api_environment"]["ARI_TRANSFER_DEFAULT_CALLER_ID"] == "set"
    assert d["api_environment"]["ARI_PJSIP_DEFAULT_TRUNK"] == {
        "set": True,
        "value": "fractel",
    }
    assert d["api_environment"]["CAMPAIGN_TRANSFER_DESTINATION_POLICY"] == {
        "set": True,
        "value": "forced_default",
    }
    assert d["api_environment"]["DOGRAH_ARI_TRUNK"] == "unset"
    assert d["api_mounts"][1]["source"] == "<other>"
    assert d["api_patch_markers"] == ["HOPWHISTLE_FISH_TTS_V1"]
    org = d["database"]["org_model_configuration"][0]
    assert org["llm_model"] == "gpt-4.1"
    assert (
        org["stt_model"] == "<non-identifier>"
        and org["tts_model"] == "<non-identifier>"
    )
    assert d["database"]["workflows"][0]["v2_realtime_model"] == "gpt-live-1"
    assert d["database"]["runs_last_7_days_by_model"][0]["runs"] == 12
    assert d["asterisk"]["hopwhistle_blocks"] == [
        "HOPWHISTLE AI CALLBACK",
        "HOPWHISTLE DIRECT TRANSFER",
    ]
    assert "<non-identifier>" in d["asterisk"]["pjsip_endpoints"] or all(
        "1555" not in e for e in d["asterisk"]["pjsip_endpoints"]
    )
    assert d["nginx"] == {
        "aivoice_server_names": ["aivoice.hopwhistle.com"],
        "x_frame_options_present": True,
        "frame_ancestors_present": False,
    }
    assert d["schedules"]["push_runs_in_root_crontab"] is True
    assert report["hoppwhistle"]["api_environment"]["AIVOICE_JWT_SECRET"] == "set"
    assert report["hoppwhistle"]["database"]["external_agent_bindings"] == [
        {"source_type": "DOGRAH", "active": "true", "bindings": 2}
    ]


def test_database_reads_are_read_only_transactions():
    runner = FakeRunner()
    collect(runner)
    scripts = [inp.decode() for cmd, inp in runner.calls if "psql" in cmd]
    assert scripts
    for script in scripts:
        assert script.startswith("BEGIN READ ONLY;") and script.rstrip().endswith(
            "ROLLBACK;"
        )
        body = script.upper()
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "ALTER ", "DROP ", "TRUNCATE "):
            assert verb not in body


@pytest.mark.parametrize(
    "hw_secret,expected", [(SECRET, "match"), ("other", "mismatch"), (None, "missing")]
)
def test_secret_parity_is_reported_without_values(hw_secret, expected):
    report = collect(FakeRunner(hw_secret=hw_secret))
    assert report["jwt_secret_parity"] == expected


def test_secret_parity_from_stdin(monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(SECRET + "\n"))
    args = ci.parse_args(["--peer-secret-stdin"])
    assert ci.Collector(args, run=FakeRunner()).compare_secret() == "match"


def test_unavailable_sources_are_unknown_with_fixed_reasons():
    report = collect(FakeRunner(fail=("nginx", "asterisk", "psql")))
    assert report["dograh"]["nginx"] == {
        "status": "UNKNOWN",
        "reason": "command_failed",
    }
    assert report["dograh"]["asterisk"]["status"] == "UNKNOWN"
    assert report["dograh"]["database"]["alembic_version"]["status"] == "UNKNOWN"
    assert report["dograh"]["source_checkout"] == {
        "status": "UNKNOWN",
        "reason": "not_found",
    }
    no_hw = ci.Collector(
        ci.parse_args(["--compose-dir", "/nonexistent"]), run=FakeRunner()
    ).collect()
    assert no_hw["hoppwhistle"]["api_environment"] == {
        "status": "UNKNOWN",
        "reason": "not_on_this_host",
    }
    assert no_hw["jwt_secret_parity"] == "UNKNOWN:hoppwhistle_not_on_this_host"


def test_final_guard_refuses_unsafe_values():
    with pytest.raises(ValueError):
        ci.enforce_safe({"x": [f"call {PHONE}"]})
    with pytest.raises(ValueError):
        ci.enforce_safe({"x": "y" * 500})
    assert ci.enforce_safe({"id": "sha256:" + "ab" * 32}) == {
        "id": "sha256:" + "ab" * 32
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        ("gpt-4.1", "gpt-4.1"),
        ("us.amazon.nova-pro-v1:0", "us.amazon.nova-pro-v1:0"),
        (PHONE, "<non-identifier>"),
        ("https://user:pw@host", "<non-identifier>"),
        ("free text with spaces", "<non-identifier>"),
        (None, None),
    ],
)
def test_ident(value, expected):
    assert ci.ident(value) == expected
