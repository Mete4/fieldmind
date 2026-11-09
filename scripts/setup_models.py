#!/usr/bin/env python3
"""Phase 2 model setup. Inventories the pulled Ollama models, checks memory
headroom, measures tokens/s for every LLM candidate that is pulled, applies
the rule "live extraction model must do >= LLM_MIN_TOKENS_PER_S", and writes
data/models.json (read by app/extract.py). Also keeps the chosen models
loaded (keep_alive -1) so both LLMs and Whisper are resident at the same time.

  .venv/bin/python scripts/setup_models.py [--no-bench] [--num-predict 128]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config as C  # noqa: E402
from app import db  # noqa: E402

import ollama  # noqa: E402
import psutil  # noqa: E402

BENCH_PROMPT = ("You are a disaster-response assistant. In about 120 words, describe the standard radio "
                "report format for a structure fire with trapped occupants, then list five resource needs.")


def bench(client: ollama.Client, model: str, num_predict: int) -> dict:
    r = client.chat(model=model, messages=[{"role": "user", "content": BENCH_PROMPT}], keep_alive=C.OLLAMA_KEEP_ALIVE,
                    options={"temperature": 0.1, "num_ctx": C.LLM_NUM_CTX, "num_predict": num_predict})
    ev, ed = int(r.eval_count or 0), int(r.eval_duration or 0)
    pe, pd = int(r.prompt_eval_count or 0), int(r.prompt_eval_duration or 0)
    total, load = int(r.total_duration or 0), int(r.load_duration or 0)
    return {"tokens_per_s": round(ev / (ed / 1e9), 1) if ed else None, "prompt_tokens_per_s": round(pe / (pd / 1e9), 1) if pd else None,
            "eval_tokens": ev, "load_s": round(load / 1e9, 1), "total_s": round(total / 1e9, 1)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-bench", action="store_true")
    ap.add_argument("--num-predict", type=int, default=128)
    ap.add_argument("--preload", action="store_true", help="load live LLM and summary LLM (keep_alive -1) and exit")
    a = ap.parse_args()
    db.init_db()
    client = ollama.Client(host=C.OLLAMA_HOST, timeout=600)
    try:
        version = client._request_raw("GET", "/api/version").json().get("version")
        pulled = {m.model: m for m in client.list().models}
    except Exception as e:  # noqa: BLE001
        print(f"Ollama not reachable at {C.OLLAMA_HOST}: {e}")
        return 1
    vm = psutil.virtual_memory()
    total_gb, avail_gb = vm.total / 2**30, vm.available / 2**30
    budget_gb = total_gb * 0.85
    print(f"Ollama {version} at {C.OLLAMA_HOST} | unified memory {total_gb:.0f} GiB, available {avail_gb:.0f} GiB, "
          f"budget with 15% headroom {budget_gb:.0f} GiB | num_parallel {C.OLLAMA_NUM_PARALLEL}")
    print("pulled models:")
    for name, m in pulled.items():
        print(f"  {name:<16} {m.size / 2**30:6.1f} GiB  {getattr(m.details, 'family', '') or ''} {getattr(m.details, 'parameter_size', '') or ''} {getattr(m.details, 'quantization_level', '') or ''}")
    cands = [c for c in C.LLM_CANDIDATES if c in pulled]
    if a.preload:
        prev = json.loads(C.MODELS_JSON.read_text()) if C.MODELS_JSON.exists() else {}
        todo = [(prev.get("live_llm") or (cands[-1] if cands else None), C.LLM_NUM_CTX),
                (prev.get("summary_llm") or (cands[0] if cands else None), C.LLM_NUM_CTX)]
        for model, ctx in todo:
            if not model or model not in pulled:
                continue
            t0 = time.time()
            client.chat(model=model, messages=[{"role": "user", "content": "Reply READY."}], keep_alive=C.OLLAMA_KEEP_ALIVE,
                        options={"num_ctx": ctx, "num_predict": 3})
            print(f"  loaded {model} (num_ctx {ctx}) in {time.time() - t0:.1f}s")
        print("resident:", [(m.model, f"{m.size / 2**30:.1f} GiB") for m in client.ps().models])
        return 0
    results: dict[str, dict] = {}
    if not a.no_bench:
        for cand in cands:
            print(f"benchmarking {cand} ...", end=" ", flush=True)
            try:
                results[cand] = bench(client, cand, a.num_predict)
                print(f"{results[cand]['tokens_per_s']} tok/s generation, {results[cand]['prompt_tokens_per_s']} tok/s prompt, load {results[cand]['load_s']}s")
            except Exception as e:  # noqa: BLE001
                results[cand] = {"error": str(e)[:160]}
                print("FAILED", results[cand]["error"])
    live = next((c for c in cands if (results.get(c, {}).get("tokens_per_s") or 0) >= C.LLM_MIN_TOKENS_PER_S), None)
    if live is None and cands:
        live = cands[-1]
    summary = cands[0] if cands else None
    resident = [m for m in (live, summary) if m]
    resident_gb = sum(pulled[m].size / 2**30 for m in set(resident))
    choice = {"live_llm": live, "summary_llm": summary, "candidates_pulled": cands,
              "benchmarks": results, "memory_total_gib": round(total_gb, 1), "memory_budget_gib": round(budget_gb, 1),
              "resident_models_gib": round(resident_gb, 1), "num_parallel": C.OLLAMA_NUM_PARALLEL,
              "min_tokens_per_s": C.LLM_MIN_TOKENS_PER_S, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    C.MODELS_JSON.write_text(json.dumps(choice, indent=1))
    db.set_metric("models", choice)
    print(f"\nlive extraction LLM : {live}  (largest pulled model with >= {C.LLM_MIN_TOKENS_PER_S} tok/s)")
    print(f"edge summary LLM    : {summary}  (largest pulled)")
    print(f"resident model weights: {resident_gb:.1f} GiB of {budget_gb:.0f} GiB budget -> {'OK' if resident_gb < budget_gb else 'TOO BIG'}")
    stepped = cands and live != cands[0]
    if stepped:
        print(f"NOTE: {cands[0]} is below {C.LLM_MIN_TOKENS_PER_S} tok/s, stepped down to {live} for live extraction; {cands[0]} is still used for the edge summary.")
    print(f"wrote {C.MODELS_JSON.relative_to(ROOT)}")
    try:
        loaded = client.ps().models
        print("loaded now (ollama ps):", [(m.model, f"{m.size / 2**30:.1f} GiB") for m in loaded])
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
