# Model and dependency licences

HopSTT itself is first-party code. It does not ship model weights; the
`hopstt.tools.fetch_models` step downloads them at the pinned revisions in
`hopstt/models.py`. Each model's licence governs its use. Verified against the
model cards on 2026-11-21 (see INVESTIGATION.md §3). The required attribution
lines are collected in `NOTICE`; keep `NOTICE` in every image and distribution.

| Model id | Weights | Licence | Commercial use | What we must do |
|---|---|---|---|---|
| `nemotron-3.5-streaming` | nvidia/nemotron-3.5-asr-streaming-0.6b @ `ea30d66` | **OpenMDW-1.1** | yes, no use restrictions | Keep the licence notice / copyright notice with redistributed weights. |
| `nemotron-en-streaming` | nvidia/nemotron-speech-streaming-en-0.6b @ `ebe59e5` | **NVIDIA Open Model License** | yes | Include the licence and the notice "Licensed by NVIDIA Corporation under the NVIDIA Open Model License" when distributing the model or derivatives. The licence has guardrail and termination clauses: bypassing the model's safety guardrails, or suing NVIDIA over the model, terminates the licence. Review before production use. |
| `parakeet-tdt-0.6b-v3` | nvidia/parakeet-tdt-0.6b-v3 @ `541d1f9` | **CC-BY-4.0** | yes, with attribution | Attribute NVIDIA, link the licence, state that the weights are unmodified (see `NOTICE`). Surface the attribution wherever transcripts or the service are described to third parties (e.g. product docs). |
| `voxtral-realtime` | mistralai/Voxtral-Mini-4B-Realtime-2602 @ `2769294` (served by vLLM) | Apache-2.0 | yes | Keep the licence and notices. |
| `qwen3-asr-1.7b` | Qwen/Qwen3-ASR-1.7B @ `7278e1e` | Apache-2.0 | yes | Keep the licence and notices. |
| `whisper-large-v3-turbo` | dropbox-dash/faster-whisper-large-v3-turbo @ `0a363e9` | MIT | yes | Keep the copyright and permission notice. |
| `whisper-base` (dev only) | Systran/faster-whisper-base @ `ebe41f7` | MIT | yes | Keep the copyright and permission notice. |

Full texts:
- OpenMDW-1.1: https://openmdw.ai/license/1-1/
- NVIDIA Open Model License: https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/
- CC BY 4.0: https://creativecommons.org/licenses/by/4.0/legalcode
- Apache-2.0: https://www.apache.org/licenses/LICENSE-2.0
- MIT: https://opensource.org/license/mit

## Software

| Package | Licence | Notes |
|---|---|---|
| NVIDIA NeMo (`nemo_toolkit[asr]==3.0.0`) | Apache-2.0 | `nemo` extra |
| PyTorch | BSD-3-Clause | |
| faster-whisper, CTranslate2 | MIT | `whisper` extra |
| qwen-asr | Apache-2.0 | `qwen` extra (own image: pins transformers 4.57.6) |
| vLLM | Apache-2.0 | separate server for Voxtral; HopSTT only speaks its WebSocket API |
| FastAPI, Starlette, uvicorn, numpy, scipy, prometheus-client | MIT / BSD | base |
| FFmpeg (Ubuntu / Debian package) | LGPL-2.1+ with GPL parts in distro builds | run as a subprocess, not linked |

Not used: kyutai/stt-1b-en_fr (no Spanish) and parakeet_realtime_eou_120m
(no punctuation) — see INVESTIGATION.md.
