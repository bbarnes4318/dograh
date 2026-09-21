#!/usr/bin/env python3
"""Layer the Inception (Mercury) BYOK provider onto this box's patched API files.

Why this exists
---------------
`hopwhistle-prod-ash` runs the upstream `dograhai/dograh-api:latest` image with
individual files bind-mounted over it from `/opt/dograh-patches/`. Three of the
five files the Inception provider touches are already patched there with local
customizations (`service_factory.py`, `registry.py`, `check_validity.py`), so
copying the repo's versions over them would silently drop those customizations.

This script instead applies the Inception change as anchored edits to whatever
is on the box right now, in the same style as `apply_fish_tts_patch.py` and
`apply_transfer_duration_hotfix.py`.

Usage
-----
    # 1. seed the two files that are not patched yet, out of the container
    python3 apply_inception_patch.py --seed

    # 2. see what would change, touching nothing
    python3 apply_inception_patch.py --dry-run

    # 3. apply (writes .bak-inception-<timestamp> next to each file)
    python3 apply_inception_patch.py

Nothing is written unless every anchor for every file is found. If an anchor is
missing the script names the file and the edit and exits non-zero, so a
customization that moved one of the anchored lines is reported rather than
half-patched.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

PATCH_DIR = Path("/opt/dograh-patches")
COMPOSE_DIR = Path("/opt/dograh")

# Files already mounted from PATCH_DIR (patch file name -> path inside container)
ALREADY_MOUNTED = {
    "registry.py": "/app/api/services/configuration/registry.py",
    "check_validity.py": "/app/api/services/configuration/check_validity.py",
    "service_factory.py": "/app/api/services/pipecat/service_factory.py",
}

# Files that must be seeded out of the running container before patching, and
# then added to docker-compose.override.yaml.
NEEDS_SEEDING = {
    "realtime_feedback_observer.py": (
        "/app/api/services/pipecat/realtime_feedback_observer.py"
    ),
    "qa_llm_config.py": "/app/api/services/workflow/qa/llm_config.py",
}

# A file is considered already patched if this string is present.
IDEMPOTENCY_MARKERS = {
    "registry.py": "InceptionLLMConfiguration",
    "check_validity.py": "_check_inception_api_key",
    "service_factory.py": "DograhInceptionLLMService",
    "realtime_feedback_observer.py": "LLM_TTFT_LOG_TAG",
    "qa_llm_config.py": "ServiceProviders.INCEPTION.value",
}

# ---------------------------------------------------------------- registry.py

REGISTRY_EDITS = [
    (
        '    OPENROUTER = "openrouter"\n    INWORLD = "inworld"',
        '    OPENROUTER = "openrouter"\n    INCEPTION = "inception"\n'
        '    INWORLD = "inworld"',
    ),
    (
        "        ServiceProviders.OPENROUTER,\n        ServiceProviders.INWORLD,",
        "        ServiceProviders.OPENROUTER,\n        ServiceProviders.INCEPTION,\n"
        "        ServiceProviders.INWORLD,",
    ),
    (
        'OPENROUTER_PROVIDER_MODEL_CONFIG = provider_model_config("Open Router")\n',
        'OPENROUTER_PROVIDER_MODEL_CONFIG = provider_model_config("Open Router")\n'
        "INCEPTION_PROVIDER_MODEL_CONFIG = provider_model_config(\n"
        '    "Inception",\n'
        "    description=(\n"
        '        "Inception Mercury diffusion LLMs. Mercury 2.5 decodes tokens in "\n'
        '        "parallel for very low time-to-first-token, and exposes a "\n'
        '        "reasoning effort knob to trade latency against depth."\n'
        "    ),\n"
        '    provider_docs_url="https://docs.inceptionlabs.ai/",\n'
        ")\n",
    ),
    (
        'DOGRAH_LLM_MODELS = ["default", "accurate", "fast", "lite", "zen"]',
        "INCEPTION_MODELS = [\n"
        '    "mercury-2.5",\n'
        '    "mercury-2",\n'
        "]\n"
        'INCEPTION_DEFAULT_MODEL = "mercury-2.5"\n'
        'INCEPTION_DEFAULT_BASE_URL = "https://api.inceptionlabs.ai/v1"\n'
        "# Inception's diffusion models expose a reasoning-effort knob instead of a\n"
        '# separate "thinking" model tier. "instant" is the lowest latency; "high"\n'
        "# spends the most decoding budget on reasoning.\n"
        'INCEPTION_DEFAULT_REASONING_EFFORT = "low"\n'
        "\n"
        'DOGRAH_LLM_MODELS = ["default", "accurate", "fast", "lite", "zen"]',
    ),
    (
        "@register_llm\nclass AzureLLMService(BaseLLMConfiguration):",
        "@register_llm\n"
        "class InceptionLLMConfiguration(BaseLLMConfiguration):\n"
        "    model_config = INCEPTION_PROVIDER_MODEL_CONFIG\n"
        "    provider: Literal[ServiceProviders.INCEPTION] = ServiceProviders.INCEPTION\n"
        "    model: str = Field(\n"
        "        default=INCEPTION_DEFAULT_MODEL,\n"
        '        description="Inception Mercury model identifier.",\n'
        '        json_schema_extra={"examples": INCEPTION_MODELS, "allow_custom_input": True},\n'
        "    )\n"
        "    base_url: str = Field(\n"
        "        default=INCEPTION_DEFAULT_BASE_URL,\n"
        '        description="Override only if proxying Inception through your own gateway.",\n'
        "    )\n"
        '    reasoning_effort: Literal["instant", "low", "medium", "high"] = Field(\n'
        "        default=INCEPTION_DEFAULT_REASONING_EFFORT,\n"
        "        description=(\n"
        '            "How much of the diffusion budget Mercury spends on reasoning. "\n'
        "            \"'instant' is fastest; raise it for harder turns at the cost of \"\n"
        '            "time-to-first-token."\n'
        "        ),\n"
        "    )\n"
        "\n"
        "\n"
        "@register_llm\n"
        "class AzureLLMService(BaseLLMConfiguration):",
    ),
    (
        "        OpenRouterLLMConfiguration,\n        GoogleLLMService,",
        "        OpenRouterLLMConfiguration,\n        InceptionLLMConfiguration,\n"
        "        GoogleLLMService,",
    ),
]

# ----------------------------------------------------------- check_validity.py

CHECK_VALIDITY_EDITS = [
    (
        "            ServiceProviders.OPENROUTER.value: self._check_openrouter_api_key,",
        "            ServiceProviders.OPENROUTER.value: self._check_openrouter_api_key,\n"
        "            ServiceProviders.INCEPTION.value: self._check_inception_api_key,",
    ),
    (
        "            ServiceProviders.OPENAI.value,\n"
        "            ServiceProviders.OPENAI_REALTIME.value,\n"
        "        ):",
        "            ServiceProviders.OPENAI.value,\n"
        "            ServiceProviders.OPENAI_REALTIME.value,\n"
        "            ServiceProviders.INCEPTION.value,\n"
        "        ):",
    ),
    (
        "    def _check_openrouter_api_key(self, model: str, api_key: str) -> bool:\n"
        "        return True",
        "    def _check_openrouter_api_key(self, model: str, api_key: str) -> bool:\n"
        "        return True\n"
        "\n"
        "    def _check_inception_api_key(\n"
        "        self, model: str, api_key: str, service_config: Optional[ServiceConfig] = None\n"
        "    ) -> bool:\n"
        '        """Best-effort check against Inception\'s OpenAI-compatible model list.\n'
        "\n"
        "        Only a clear auth rejection blocks save; anything else (rate limit,\n"
        "        outage, a gateway that doesn't implement /models) is allowed through so\n"
        "        a transient upstream problem can't lock a user out of their own config.\n"
        '        """\n'
        "        base_url = (\n"
        '            getattr(service_config, "base_url", None) if service_config else None\n'
        '        ) or "https://api.inceptionlabs.ai/v1"\n'
        "        try:\n"
        "            response = httpx.get(\n"
        "                f\"{base_url.rstrip('/')}/models\",\n"
        '                headers={"Authorization": f"Bearer {api_key}"},\n'
        "                timeout=10.0,\n"
        "            )\n"
        "        except httpx.RequestError:\n"
        "            raise ValueError(\n"
        '                f"Could not connect to the Inception API at {base_url}. Please check "\n'
        '                "the base URL and your network connection, and try again."\n'
        "            )\n"
        "        if response.status_code in (401, 403):\n"
        "            raise ValueError(\n"
        '                "Invalid Inception API key. The key was rejected by the Inception API. "\n'
        '                "Please check that your API key is correct and active. You can manage "\n'
        '                "keys at https://platform.inceptionlabs.ai/."\n'
        "            )\n"
        "        return True",
    ),
]

# ------------------------------------------------------------ service_factory.py

SERVICE_FACTORY_EDITS = [
    (
        "from api.services.configuration.registry import ServiceProviders\n",
        "from api.services.configuration.registry import (\n"
        "    INCEPTION_DEFAULT_BASE_URL,\n"
        "    INCEPTION_DEFAULT_REASONING_EFFORT,\n"
        "    ServiceProviders,\n"
        ")\n",
    ),
    (
        "from pipecat.services.inworld.tts import InworldTTSService, InworldTTSSettings\n",
        "from pipecat.services.inception.llm import InceptionLLMService\n"
        "from pipecat.services.inworld.tts import InworldTTSService, InworldTTSSettings\n",
    ),
    (
        "class DograhGoogleVertexLLMService(GoogleVertexLLMService):\n"
        "    adapter_class = DograhGeminiJSONSchemaAdapter\n",
        "class DograhGoogleVertexLLMService(GoogleVertexLLMService):\n"
        "    adapter_class = DograhGeminiJSONSchemaAdapter\n"
        "\n"
        "\n"
        "class DograhInceptionLLMService(InceptionLLMService):\n"
        '    """Inception Mercury with a structured-output guard.\n'
        "\n"
        "    Inception's OpenAI-compatible endpoint does not advertise native\n"
        "    ``json_schema`` structured outputs and rejects a strict ``json_schema``\n"
        "    ``response_format``. Dograh's structured-output callers (variable\n"
        "    extraction, gathered-context extraction, QA analysis, node summaries) ask\n"
        "    for JSON in the prompt and parse leniently, so they work as-is — but if a\n"
        "    caller ever does set a ``json_schema`` response format, downgrade it to\n"
        "    JSON mode here rather than letting the request fail.\n"
        "\n"
        "    This overrides the single method every request path funnels through\n"
        "    (streaming ``_process_context`` and one-shot ``run_inference`` both build\n"
        "    their params here), and only ever runs for Inception.\n"
        '    """\n'
        "\n"
        "    def build_chat_completion_params(self, params_from_context) -> dict:\n"
        "        params = super().build_chat_completion_params(params_from_context)\n"
        '        response_format = params.get("response_format")\n'
        "        is_json_schema = (\n"
        "            isinstance(response_format, dict)\n"
        '            and response_format.get("type") == "json_schema"\n'
        "        )\n"
        "        if is_json_schema:\n"
        "            logger.warning(\n"
        '                "Inception does not support json_schema response_format; "\n'
        '                "falling back to JSON mode for this request."\n'
        "            )\n"
        '            params["response_format"] = {"type": "json_object"}\n'
        "        return params\n",
    ),
    (
        "    temperature: float | None = None,\n    bill_to: str | None = None,\n):",
        "    temperature: float | None = None,\n    bill_to: str | None = None,\n"
        "    reasoning_effort: str | None = None,\n):",
    ),
    (
        "        return OpenRouterLLMService(\n"
        "            api_key=api_key,\n"
        "            settings=OpenRouterLLMSettings(model=model, temperature=0.1),\n"
        "            **kwargs,\n"
        "        )\n",
        "        return OpenRouterLLMService(\n"
        "            api_key=api_key,\n"
        "            settings=OpenRouterLLMSettings(model=model, temperature=0.1),\n"
        "            **kwargs,\n"
        "        )\n"
        "    elif provider == ServiceProviders.INCEPTION.value:\n"
        "        base_url = base_url or INCEPTION_DEFAULT_BASE_URL\n"
        '        _validate_runtime_service_url(base_url, "base_url")\n'
        "        return DograhInceptionLLMService(\n"
        "            api_key=api_key,\n"
        "            base_url=base_url,\n"
        "            settings=DograhInceptionLLMService.Settings(\n"
        "                model=model,\n"
        "                temperature=0.1,\n"
        "                reasoning_effort=reasoning_effort or INCEPTION_DEFAULT_REASONING_EFFORT,\n"
        "            ),\n"
        "        )\n",
    ),
    (
        "    elif provider == ServiceProviders.OPENROUTER.value:\n"
        '        kwargs["base_url"] = user_config.llm.base_url\n'
        "    elif provider == ServiceProviders.AZURE.value:",
        "    elif provider == ServiceProviders.OPENROUTER.value:\n"
        '        kwargs["base_url"] = user_config.llm.base_url\n'
        "    elif provider == ServiceProviders.INCEPTION.value:\n"
        '        kwargs["base_url"] = user_config.llm.base_url\n'
        '        kwargs["reasoning_effort"] = user_config.llm.reasoning_effort\n'
        "    elif provider == ServiceProviders.AZURE.value:",
    ),
]

# ------------------------------------------------ realtime_feedback_observer.py

OBSERVER_EDITS = [
    (
        "if TYPE_CHECKING:\n"
        "    from api.services.pipecat.in_memory_buffers import InMemoryLogsBuffer",
        "#: Prefix on the per-turn LLM time-to-first-token log line. Stable so call-log\n"
        "#: pipelines can filter these timing lines out (``grep -v llm_ttft``).\n"
        'LLM_TTFT_LOG_TAG = "[llm_ttft]"\n'
        "\n"
        "if TYPE_CHECKING:\n"
        "    from api.services.pipecat.in_memory_buffers import InMemoryLogsBuffer",
    ),
    (
        "                if isinstance(metric_data, TTFBMetricsData):\n"
        "                    # Only send TTFB if it's from an LLM processor\n"
        '                    if metric_data.processor and "LLM" in metric_data.processor:\n'
        "                        await self._send_message(",
        "                if isinstance(metric_data, TTFBMetricsData):\n"
        "                    # Only send TTFB if it's from an LLM processor\n"
        '                    if metric_data.processor and "LLM" in metric_data.processor:\n'
        "                        # Per-turn time-to-first-token for the LLM leg. This is\n"
        "                        # measured on our side by the LLM service itself:\n"
        "                        # the clock starts just before the chat-completion\n"
        "                        # request goes out and stops on the first streamed chunk\n"
        "                        # that carries a choice — never a provider-reported\n"
        "                        # number. The LLM_TTFT_LOG_TAG prefix is there so these\n"
        "                        # lines can be grepped out of call logs.\n"
        "                        logger.info(\n"
        '                            f"{LLM_TTFT_LOG_TAG} ttft_ms="\n'
        '                            f"{metric_data.value * 1000:.1f} "\n'
        '                            f"processor={metric_data.processor} "\n'
        '                            f"model={metric_data.model}"\n'
        "                        )\n"
        "                        await self._send_message(",
    ),
]

# ----------------------------------------------------------- qa/llm_config.py

QA_LLM_CONFIG_EDITS = [
    (
        "from api.db.models import WorkflowRunModel\n"
        "from api.services.workflow.dto import QANodeData",
        "from api.db.models import WorkflowRunModel\n"
        "from api.services.configuration.registry import ServiceProviders\n"
        "from api.services.workflow.dto import QANodeData",
    ),
    (
        '    elif provider == "openrouter" and llm_config.get("base_url"):\n'
        '        kwargs["base_url"] = llm_config["base_url"]\n'
        "\n"
        "    return provider, model, api_key, kwargs",
        '    elif provider == "openrouter" and llm_config.get("base_url"):\n'
        '        kwargs["base_url"] = llm_config["base_url"]\n'
        "    elif provider == ServiceProviders.INCEPTION.value:\n"
        "        # reasoning_effort is what makes Mercury worth using, so it has to\n"
        "        # survive the hop into QA/node-summary inference too.\n"
        '        if llm_config.get("base_url"):\n'
        '            kwargs["base_url"] = llm_config["base_url"]\n'
        '        if llm_config.get("reasoning_effort"):\n'
        '            kwargs["reasoning_effort"] = llm_config["reasoning_effort"]\n'
        "\n"
        "    return provider, model, api_key, kwargs",
    ),
]

EDITS = {
    "registry.py": REGISTRY_EDITS,
    "check_validity.py": CHECK_VALIDITY_EDITS,
    "service_factory.py": SERVICE_FACTORY_EDITS,
    "realtime_feedback_observer.py": OBSERVER_EDITS,
    "qa_llm_config.py": QA_LLM_CONFIG_EDITS,
}


def seed_from_container(patch_dir: Path, compose_dir: Path) -> int:
    """Copy the two unpatched files out of the running api container."""
    failures = 0
    for name, container_path in NEEDS_SEEDING.items():
        target = patch_dir / name
        if target.exists():
            print(f"  {name}: already present, leaving it alone")
            continue
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "api", "cat", container_path],
            cwd=compose_dir,
            capture_output=True,
        )
        if result.returncode != 0:
            print(
                f"  {name}: FAILED to read {container_path} from the api container\n"
                f"    {result.stderr.decode(errors='replace').strip()}"
            )
            failures += 1
            continue
        target.write_bytes(result.stdout)
        print(f"  {name}: seeded {len(result.stdout)} bytes from {container_path}")
    return failures


def check_file(path: Path, name: str) -> tuple[str | None, list[int]]:
    """Return (text, indices of edits whose anchor is missing)."""
    text = path.read_text()
    missing = [
        i for i, (find, _) in enumerate(EDITS[name], start=1) if find not in text
    ]
    return text, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--patch-dir", type=Path, default=PATCH_DIR, help="default /opt/dograh-patches"
    )
    parser.add_argument(
        "--compose-dir", type=Path, default=COMPOSE_DIR, help="default /opt/dograh"
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="copy the two not-yet-patched files out of the api container, then exit",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    args = parser.parse_args()

    if args.seed:
        print(f"Seeding unpatched files into {args.patch_dir} ...")
        return 1 if seed_from_container(args.patch_dir, args.compose_dir) else 0

    # ---- pass 1: read everything and verify every anchor before writing ----
    plans: dict[str, tuple[Path, str]] = {}
    problems: list[str] = []

    for name in EDITS:
        path = args.patch_dir / name
        if not path.exists():
            problems.append(
                f"{name}: missing at {path}"
                + ("  (run with --seed first)" if name in NEEDS_SEEDING else "")
            )
            continue

        text = path.read_text()
        if IDEMPOTENCY_MARKERS[name] in text:
            print(f"  {name}: already patched, skipping")
            continue

        text, missing = check_file(path, name)
        if missing:
            problems.append(
                f"{name}: anchor not found for edit(s) {missing} of "
                f"{len(EDITS[name])} — a local customization probably moved those "
                f"lines; send me this file and I'll re-anchor it"
            )
            continue

        for find, replace in EDITS[name]:
            if text.count(find) != 1:
                problems.append(
                    f"{name}: anchor appears {text.count(find)} times, expected once"
                )
                break
            text = text.replace(find, replace, 1)
        else:
            plans[name] = (path, text)

    if problems:
        print("\nNOT APPLYING ANYTHING. Problems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    if not plans:
        print("\nNothing to do — every file is already patched.")
        return 0

    # ---- pass 2: write ----
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for name, (path, text) in sorted(plans.items()):
        if args.dry_run:
            print(f"  {name}: would apply {len(EDITS[name])} edit(s)")
            continue
        backup = path.with_name(f"{path.name}.bak-inception-{stamp}")
        shutil.copy2(path, backup)
        path.write_text(text)
        print(f"  {name}: applied {len(EDITS[name])} edit(s)  (backup: {backup.name})")

    if args.dry_run:
        print("\nDry run — nothing written.")
        return 0

    print("\nPatched. Remaining manual steps:")
    print("  1. Add these two mounts under the `api:` service's volumes in")
    print(f"     {args.compose_dir}/docker-compose.override.yaml :")
    for name, container_path in NEEDS_SEEDING.items():
        print(f"       - {args.patch_dir}/{name}:{container_path}:ro")
    print("  2. docker compose up -d --force-recreate --no-deps api")
    print(
        "  3. curl -s localhost:8000/api/v1/user/configurations/defaults | grep -o inception"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
