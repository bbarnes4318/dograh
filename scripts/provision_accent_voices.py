#!/usr/bin/env python3
"""Provision 150 original regional-accent voices (75 accents x male/female) on Fish Audio.

Flow per voice:
  1. POST /v1/voice-design  (model header: voice-design-1) -> candidate WAV (base64)
  2. POST /model            (type=tts, train_mode=fast)    -> persistent voice model _id
Writes:
  accent_voice_registry.json   accent_id -> {male:{voice_id,...}, female:{voice_id,...}}
  accent_routing_map.json      area code -> {accent_id, accent_name, male_voice_id, female_voice_id}
  provision_log.csv            one row per voice, success/failure
  previews/<accent_id>_<gender>.mp3 (wav if ffmpeg is missing)

Idempotent: voices already present in accent_voice_registry.json with a voice_id are skipped.
Usage:
  FISH_API_KEY=... python provision_accent_voices.py --data-dir api/data/accent_voices
  options: --only 1,2,3   --gender male|female|both   --dry-run
"""
import argparse, base64, csv, json, os, random, shutil, subprocess, sys, time
from pathlib import Path

import requests

API = "https://api.fish.audio"
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def call(method, url, *, max_tries=6, **kw):
    for attempt in range(max_tries):
        try:
            r = requests.request(method, url, timeout=180, **kw)
        except requests.RequestException as e:
            err = f"network: {e}"
        else:
            if r.status_code < 300:
                return r
            err = f"HTTP {r.status_code}: {r.text[:400]}"
            if r.status_code not in RETRY_STATUS:
                raise RuntimeError(err)
        wait = min(60, 2 ** attempt) + random.uniform(0, 1)
        print(f"   retry {attempt + 1}/{max_tries} in {wait:.1f}s ({err})", flush=True)
        time.sleep(wait)
    raise RuntimeError(f"gave up after {max_tries} tries: {err}")


def design(key, instruction, preview_text, seed):
    r = call("POST", f"{API}/v1/voice-design",
             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                      "model": "voice-design-1"},
             json={"instruction": instruction, "reference_text": preview_text,
                   "language": "en", "n": 1, "seed": seed})
    return r.json()["candidates"][0]


def create_model(key, title, description, tags, wav_bytes, text, signature):
    def post(with_sig):
        data = [("type", "tts"), ("title", title), ("train_mode", "fast"),
                ("visibility", "private"), ("description", description),
                ("texts", text), ("enhance_audio_quality", "false")]
        data += [("tags", t) for t in tags]
        if with_sig and signature:
            data.append(("voice_design_signatures", signature))
        return call("POST", f"{API}/model", headers={"Authorization": f"Bearer {key}"},
                    data=data, files=[("voices", ("sample.wav", wav_bytes, "audio/wav"))])
    try:
        return post(True).json()
    except RuntimeError as e:
        if "HTTP 4" in str(e) and signature:   # signature rejected -> plain instant clone of the designed audio
            print("   signature rejected, retrying without it", flush=True)
            return post(False).json()
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--only", default="")
    ap.add_argument("--gender", default="both", choices=["male", "female", "both"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    key = os.environ.get("FISH_API_KEY")
    if not key and not a.dry_run:
        sys.exit("FISH_API_KEY not set")
    d = Path(a.data_dir)
    spec = json.loads((d / "accent_voices.json").read_text())
    amap = json.loads((d / "area_code_accent_map.json").read_text())
    reg_path = d / "accent_voice_registry.json"
    reg = json.loads(reg_path.read_text()) if reg_path.exists() else {}
    (d / "previews").mkdir(exist_ok=True)
    only = {int(x) for x in a.only.split(",") if x.strip()}
    genders = ["male", "female"] if a.gender == "both" else [a.gender]
    log_rows = []

    for acc in spec["accents"]:
        aid = acc["accent_id"]
        if only and aid not in only:
            continue
        entry = reg.setdefault(str(aid), {"accent_id": aid, "name": acc["name"], "states": acc["states"],
                                          "area_codes": acc["area_codes"]})
        for g in genders:
            if entry.get(g, {}).get("voice_id"):
                print(f"[{aid:>2} {g:6}] exists {entry[g]['voice_id']}")
                continue
            title = f"US Accent {aid:02d} - {acc['name']} ({g.title()})"
            print(f"[{aid:>2} {g:6}] {title}", flush=True)
            if a.dry_run:
                continue
            p = acc[g]
            seed = aid * 10 + (1 if g == "male" else 2)
            try:
                try:
                    cand = design(key, p["instruction"], spec["preview_text"], seed)
                    used = "detailed"
                except RuntimeError as e:
                    print(f"   detailed prompt failed ({e}); using fallback prompt", flush=True)
                    cand = design(key, p["fallback_instruction"], spec["preview_text"], seed)
                    used = "fallback"
                wav = base64.b64decode(cand["audio_base64"])
                wav_path = d / "previews" / f"{aid:02d}_{g}.wav"
                wav_path.write_bytes(wav)
                if shutil.which("ffmpeg"):  # small mp3 for the UI preview player
                    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-ac", "1",
                                    "-b:a", "64k", str(wav_path.with_suffix(".mp3"))], check=False)
                    if wav_path.with_suffix(".mp3").exists():
                        wav_path.unlink()
                model = create_model(key, title, f"{acc['name']} | {acc['states']} | area codes "
                                     f"{', '.join(acc['area_codes'])}", ["us-accent", f"accent-{aid}", g],
                                     wav, cand.get("text") or spec["preview_text"], cand.get("id"))
                entry[g] = {"voice_id": model["_id"], "title": title, "state": model.get("state"),
                            "prompt_used": used, "preview_ms": cand.get("duration_ms"),
                            "source": model.get("source")}
                log_rows.append([aid, acc["name"], g, "OK", model["_id"], ",".join(acc["area_codes"]), ""])
                print(f"   OK voice_id={model['_id']} state={model.get('state')}", flush=True)
            except Exception as e:
                log_rows.append([aid, acc["name"], g, "FAILED", "", ",".join(acc["area_codes"]), str(e)[:300]])
                print(f"   FAILED {e}", flush=True)
            reg_path.write_text(json.dumps(reg, indent=2, ensure_ascii=False))  # checkpoint after every voice
            time.sleep(1.0)

    # routing map: every area code -> voice ids
    routing = {"default_accent_id": amap["default_accent_id"], "area_codes": {}}
    for code, aid in amap["area_code_to_accent_id"].items():
        e = reg.get(str(aid), {})
        routing["area_codes"][code] = {"accent_id": aid, "accent_name": e.get("name"),
                                       "male_voice_id": e.get("male", {}).get("voice_id"),
                                       "female_voice_id": e.get("female", {}).get("voice_id")}
    (d / "accent_routing_map.json").write_text(json.dumps(routing, indent=2, ensure_ascii=False))

    log_path = d / "provision_log.csv"
    new = not log_path.exists()
    with log_path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["accent_id", "accent_name", "gender", "status", "voice_id", "area_codes", "error"])
        w.writerows(log_rows)
    total = sum(1 for e in reg.values() for g in ("male", "female") if e.get(g, {}).get("voice_id"))
    fails = [r for r in log_rows if r[3] == "FAILED"]
    print(f"\nDone. {total}/150 voices registered. {len(fails)} failures this run.")
    if fails:
        print("Re-run the same command to retry failures (existing voices are skipped).")


if __name__ == "__main__":
    main()
