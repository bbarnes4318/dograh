# HopSTT hardware: inventory findings and recommendation

Status 2026-11-21. **Every concurrency figure below is an unverified estimate until
`bench/capacity.py` has been run on that hardware.** Prices are from public price
listings and aggregators gathered on 2026-11-21. They exclude VAT and change often,
so confirm each one on the provider's own order page before buying.

## 1. What we have today (read-only inspection)

| Source | Finding |
|---|---|
| Hetzner production host `178.156.223.97` (Ashburn, VA) | **Specs unknown.** This session has no SSH key, and the repo's own audit (`hoppwhistle/docs/migration/SERVERSCOM_PHASE1_AUDIT.md` §1.1) lists OS/CPU/RAM/disk as `PENDING`. Nothing in either repo, its docs or its git history records a GPU. The read-only collector `hoppwhistle/scripts/migration/phase1-readonly-inventory.sh` already exists. Add `nvidia-smi -L; lscpu; free -g` to it, or run those commands once by hand. |
| servers.com account (API, GET only) | One cloud instance, `netenroll`: flavor SSD.180, **6 vCPU / 16 GB RAM / 181 GB**, region WAS1 (Washington). No dedicated servers, no bare-metal servers, and **no GPU**. The dedicated-server catalogue for the US locations checked (WAS1, WAS2, WAS3, DFW1, DFW3) lists 14 models, none with a GPU. Next invoice: $85.66. |
| Hopvoice | `services/hopvoice/docker-compose.gpu.yaml` exists, but no GPU host is recorded anywhere, and the repo has no GPU benchmark results for Hopvoice. |
| This dev container | 4 vCPU, 15 GB RAM, no GPU. |

**Conclusion: no GPU is available today.** Any GPU is a new purchase or rental, which
needs your approval. Nothing has been provisioned.

Network: Dograh and Hoppwhistle both run in Ashburn. A live STT host should be in the
same metro (us-east). A transatlantic host (Hetzner Falkenstein/Nuremberg/Helsinki)
adds roughly 80–100 ms of round-trip time to every finalize→final, which would push us
past the 300 ms target on its own. **For live streaming, rent in US-East. For batch,
location doesn't matter.**

## 2. Model fit (VRAM, approximate)

| Model | Weights (bf16/fp16) | Works on |
|---|---|---|
| Nemotron 3.5 streaming 0.6B | ~1.3 GB + small cache per stream | every card listed below; CPU for smoke tests only |
| Nemotron-en streaming 0.6B | ~1.3 GB | same |
| Voxtral Mini 4B Realtime | ~9 GB + KV; model card says **≥16 GB GPU** | 20 GB+ cards; one model per card in practice |
| Parakeet-TDT 0.6B v3 | ~1.3 GB; long files need local attention | every card; CPU workable |
| faster-whisper large-v3-turbo | ~1.6 GB fp16 / int8 on CPU | every card; CPU workable at low volume |
| Qwen3-ASR 1.7B | ~4.7 GB bf16 | 12 GB+ cards |

Software: the CUDA 12.8 base image (the same one Hopvoice uses) covers Ampere, Ada and
Blackwell. Blackwell cards (RTX 5090, RTX PRO 6000) need a torch build with sm_120
support (torch ≥ 2.7). Confirm this when you build the image.

## 3. GPU comparison

| GPU | VRAM | Power | Typical price (2026-11) | Nemotron 3.5 streams @320 ms (**estimate, unverified**) | Notes |
|---|---|---|---|---|---|
| **NVIDIA L4** | 24 GB | 72 W | GCP ≈ $0.70/h; AWS g6.xlarge ≈ $0.80/h (≈ $580/mo); US dedicated listings from ≈ $290/mo | ~60–150 | Datacenter card, low power, in every hyperscaler's us-east region. **Recommended for live.** |
| NVIDIA A10 | 24 GB | 150 W | AWS g5.xlarge ≈ $1.0/h | ~60–150 | Older Ampere card. No advantage over the L4 for this workload. |
| NVIDIA A100 40/80 GB | 40/80 GB | 250–400 W | RunPod ≈ $1.19–1.79/h; Lambda ≈ $1.99/h | ~300–600 | Overkill until we have hundreds of concurrent calls. |
| RTX 3090 | 24 GB | 350 W | marketplace only | ~60–120 | Consumer card. NVIDIA's GeForce driver licence restricts datacenter use. Marketplace hosts are less reliable. |
| RTX 4090 | 24 GB | 450 W | RunPod ≈ $0.34/h community, $0.74–0.89/h secure | ~100–250 | Very good price/performance. Same consumer-card and hosting-reliability caveats. |
| RTX 5090 | 32 GB | 575 W | RunPod ≈ $0.69–1.19/h | ~150–300 | Needs a Blackwell-ready stack. Same caveats. |
| RTX 4000 SFF Ada (Hetzner GEX44) | 20 GB | 70 W | **€184/mo** + setup fee (about half a month) | ~40–100 | **Falkenstein (FSN1) only.** Cheapest dedicated option. **Recommended for batch.** |
| RTX PRO 6000 Blackwell (Hetzner GEX63/GEX131) | 96 GB | — | €999–1,499/mo + setup | very high | Far more than we need. |
| US budget dedicated (RTX A4000 / L4) | 16–24 GB | — | ≈ $140–300/mo | ~40–120 | Locations are often not stated. Confirm the data centre is in the Ashburn area before buying. |

How the stream estimates were derived: NVIDIA's model card claims ~240 streams at
80 ms and ~2,400 at 1,120 ms **on an H100**. I scaled that by the cards' approximate
inference throughput relative to an H100. That says nothing about 8 kHz telephone
audio, our chunk size, or our latency limits. **Treat these numbers only as a reason
to measure.** `bench/capacity.py` ramps 1→5→10→20→50→100→200 streams and stops at
the first step that breaks p95 latency, error rate or accuracy.

## 3b. Measured on CPU (this container: 4 shared vCPU, no GPU, other jobs running)
| Model | Result |
|---|---|
| Nemotron 3.5 streaming (real WebSocket server, 8 kHz PCM16 / μ-law) | 320 ms chunks: step ≈ 1.15× real time, **cannot keep up**; the server correctly sends `overloaded`. 560 ms chunks: keeps up, finalize→final p50 0.84 s. 1120 ms chunks: p50 1.2–1.4 s. **CPU is not viable for live calls.** |
| Parakeet-TDT 0.6B v3 (batch) | RTF 0.13–0.16 (≈ 7× faster than real time on 4 shared cores) |
| faster-whisper `base` int8 (batch) | RTF 0.07–0.10 (the smoke test used `base`, not turbo) |

These are CPU smoke numbers, not GPU projections. They show that **batch on CPU is
viable at low volume**: a dedicated 8–16-core CPU host would process several
recording-hours per hour. Live needs a GPU.

## 4. Recommendation (lowest cost that can meet the requirements)

| Stage | Live | Batch | Approx. $/mo |
|---|---|---|---|
| Benchmark (Stages 2–4) | rented hourly: 1× L4 (or 4090) in us-east, only for benchmark runs | same box, `role=both` | ≈ $20–60 total |
| Pilot (Stages 5–7) | 1× L4 us-east (`role=live`) | CPU batch on an existing or cheap CPU host, or the same L4 with `role=both` limits | ≈ $300–600 |
| Production (Stage 8+) | **2× L4 us-east** (N+1, client failover), sized by measured capacity | **1× GEX44** (Falkenstein) or CPU workers, `role=batch` | ≈ $800–1,400 |

Why two live GPUs: a single live STT host is a single point of failure for every AI
call. HopSTT's client failover only helps if a second healthy instance exists.

**Reuse:** if a GPU host is acquired for Hopvoice TTS, a HopSTT live worker (0.6B,
a few GB of VRAM) can share it. Run it as a separate container with its own
`HOPSTT_MAX_STREAMS`. Its capacity has to be measured with Hopvoice under load at
the same time.

## Sources
- [Hetzner GEX44 product page](https://www.hetzner.com/dedicated-rootserver/gex44/), [Hetzner GPU server docs (locations)](https://docs.hetzner.com/robot/dedicated-server/server-lines/gpu-server/), [gpuhosted Hetzner GEX review](https://gpuhosted.com/en/hetzner-gpu-review/), [bex.co GEX44 break-even](https://bex.co/blog/2026/07/13/hetzner-gex44-gpu-pricing-break-even)
- [RunPod pricing summary](https://hackceleration.com/labs/runpod-pricing), [RunPod vs Lambda vs Vast.ai 2026](https://tech-insider.org/runpod-vs-lambda-vs-vast-ai-2026/), [Cloud GPU pricing comparison](https://www.cloudzero.com/blog/cloud-gpu-pricing-comparison/), [CloudRift pricing comparison](https://www.cloudrift.ai/cloud-gpu-pricing-comparison)
- [GPU Mart pricing](https://www.gpu-mart.com/pricing), [Server Room GPU](https://serverroom.net/gpu), [INXY GPU hosting guide](https://inxy.hosting/blog-posts/gpu-server-hosting-guide)
- Model cards (licences, VRAM notes): see `../INVESTIGATION.md` §3.
