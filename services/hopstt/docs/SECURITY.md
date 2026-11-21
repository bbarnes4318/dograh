# HopSTT security

HopSTT processes call audio that can contain health and personal information. It is
private infrastructure.

| Control | Implementation |
|---|---|
| Authentication | Bearer key on every endpoint except `/healthz`, `/readyz` and `/metrics`. Constant-time compare. The server refuses to start with a key shorter than 16 characters unless `HOPSTT_DEV_INSECURE=true`. A WebSocket query-string token is off by default (`HOPSTT_ALLOW_QUERY_TOKEN`). |
| Credential management | The key is supplied by environment variable from the host's secrets store (same pattern as `HOPVOICE_API_KEY`). In Dograh it can be set once by an administrator (`HOPSTT_API_KEY`), so agencies never see it. The configuration API masks it like every other secret field. In Hoppwhistle it is `HOPSTT_API_KEY` in the API container environment. |
| Network | Compose binds to `127.0.0.1` by default. Set `HOPSTT_BIND` to a **private** interface. Never expose 8790/8791 on a public IP. Firewall the ports to the Dograh and Hoppwhistle hosts. Across hosts, use a private network/VPN (WireGuard or the provider's private network), or put a TLS reverse proxy in front and use `wss://` / `https://`. Clients accept both. |
| No public unauthenticated inference | The stream, batch, jobs, models and capacity endpoints all require the key. |
| Input validation | The allowed models list (`HOPSTT_LIVE_MODELS` / `HOPSTT_BATCH_MODELS`); sample rates 8000/16000 only; encodings `pcm_s16le`/`mulaw`/`alaw`; whitelisted chunk sizes; `HOPSTT_MAX_FRAME_BYTES`; upload size enforced while streaming (`HOPSTT_MAX_UPLOAD_MB`); duration cap for sync requests; ffmpeg run without a shell, with an argument list and a timeout. **There is no URL fetch, so no SSRF.** |
| Resource exhaustion | Admission control (`HOPSTT_MAX_STREAMS`); bounded per-session queues with TCP backpressure; lag detection (`overloaded`); config and idle timeouts; bounded batch queue (`429`); batch concurrency cap; live-priority gate. |
| Isolation | Per-session model state, with no cross-session buffers. Sessions are identified only by an opaque client-supplied id. No tenant data is stored. |
| Data retention | Live audio and transcripts are never persisted. Batch uploads exist only as a temp file during decode, deleted in `finally`. Job results stay in memory for `HOPSTT_JOB_TTL_S`, then are deleted. |
| Logging | Never logs audio or transcript text. Session ids are hashed or truncated. Logs carry durations, error codes and bounded reasons. Dograh's shadow-capture directory (`HOPSTT_SHADOW_CAPTURE_DIR`, off by default) **does** contain transcripts: authorised calls only, access-controlled, short retention. |
| Telemetry | None to third parties. Prometheus is scraped internally. Hugging Face is contacted only by `fetch_models`, at build or deploy time, and only for public model weights; set `HOPSTT_ALLOW_DOWNLOAD=false` in production. |
| Supply chain | Model revisions pinned by commit hash (`hopstt/models.py`, overridable with `HOPSTT_MODEL_REVISIONS`), with sha256 checked at fetch. Python dependencies pinned in `pyproject.toml` extras. Image tags are versioned. |
| Licences | `LICENSES.md` / `NOTICE` keep the required notices: OpenMDW-1.1 (Nemotron 3.5); NVIDIA Open Model License incl. "Licensed by NVIDIA Corporation under the NVIDIA Open Model License" (Nemotron-en); CC-BY-4.0 attribution (Parakeet-TDT v3); Apache-2.0 (Voxtral, Qwen3-ASR); MIT (Whisper, faster-whisper). |
| Evaluation data | The benchmark refuses corpus or output paths inside a git working tree, writes anonymised aggregate reports by default, and never calls external APIs. Real recordings are never committed. |
