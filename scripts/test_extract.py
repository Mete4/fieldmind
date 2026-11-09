#!/usr/bin/env python3
"""Phase 1 CLI test: every sample report through the edge pipeline
(report saved -> local LLM extraction -> geocode -> hazard zones -> priority
-> link rule -> incident), printed as JSON with timing, then a summary table,
then a merge demo: a follow-up transmission about report 1 joins its incident.

  .venv/bin/python scripts/test_extract.py --reset            # sequential
  .venv/bin/python scripts/test_extract.py --parallel 4       # 4 radio channels at once
  .venv/bin/python scripts/test_extract.py --model qwen2.5:32b --limit 3
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import config as C  # noqa: E402
from app import db, hazards  # noqa: E402
from app.extract import available_models, llm_extract, resolve_model, set_model  # noqa: E402
from app.pipeline import ingest_text  # noqa: E402
from app.samples import load_reports  # noqa: E402

XYZ = "Fire at XYZ High School. The east wing burns. The fire comes from the north. It is large. 123 people are inside."
FOLLOW_UP = ("Engine 82 to Command, on scene with Engine 81 at Skyway and Bille Road. Fire has extended into the "
             "exposure on the north side, we now have three residents unaccounted for. Requesting a third alarm.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parallel", type=int, default=1, help="number of reports processed concurrently")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--model", default=None, help="override the Ollama model")
    ap.add_argument("--quiet", action="store_true", help="table only, no per-report JSON")
    ap.add_argument("--reset", action="store_true", help="wipe the database first")
    ap.add_argument("--no-merge-demo", action="store_true")
    a = ap.parse_args()

    db.init_db()
    if a.reset:
        db.reset_db()
    if a.model:
        set_model(a.model)
    model = resolve_model()
    reports = load_reports()[: a.limit]
    if not reports:
        print("no reports found in data/samples/reports.txt")
        return 1
    print(f"FieldMind edge pipeline test | {C.TOWN_NAME} | model {model} (pulled: {available_models()})")
    print(f"hazard polygons: {hazards.get_index().polygon_count} | reports: {len(reports)} | parallel: {a.parallel} | "
          f"link rule: same type within {C.LINK_RADIUS_M} m and {C.LINK_WINDOW_MIN} min")
    t_warm = time.perf_counter()
    _, meta = llm_extract("Test to Command, radio check.", model=model)
    print(f"warm-up / model load: {time.perf_counter() - t_warm:.1f}s ({meta.get('tokens_per_s')} tok/s)\n")

    def run(r: dict):
        rep, inc, action = ingest_text(r["text"], channel=r["channel"], model=model)
        if not a.quiet and a.parallel == 1:
            show(r, rep, inc, action)
        return r, rep, inc, action

    t0 = time.perf_counter()
    if a.parallel > 1:
        with ThreadPoolExecutor(max_workers=a.parallel) as ex:
            results = list(ex.map(run, reports))
        if not a.quiet:
            for row in results:
                show(*row)
    else:
        results = [run(r) for r in reports]
    wall = time.perf_counter() - t0

    print("\n#  channel          type          sev ppl conf  loc   location_text -> match (score)                      hazard zones            link      ms   tok/s")
    print("   dept unit                 part            dir  size    details")
    for r, rep, inc, action in results:
        if inc is None:
            print(f"{r['id']:<2} {r['channel'][:16]:<16} FAILED {rep.error}")
            continue
        loc = "pin?" if inc.location_unknown else "ok  "
        match = f"{(inc.location_text or '-')[:24]} -> {(inc.geocode_match or 'NONE')[:26]} ({inc.geocode_score:.0f})"
        link = "new" if action == "new" else f"->{inc.id[:6]}"
        flag = " FALLBACK" if inc.fallback else ""
        print(f"{r['id']:<2} {r['channel'][:16]:<16} {inc.type:<13} {inc.severity}   {str(inc.people_count or '-'):<3} {('R' if inc.needs_review else ' '):<4}  {loc}  "
              f"{match:<58} {','.join(inc.hazard_zones) or '-':<23} {link:<8} {inc.edge_latency_ms:>5} {inc.tokens_per_s or 0:>6.1f}{flag}")
        print(f"   {'/'.join(inc.department):<4} {'/'.join(inc.unit)[:20]:<20} {str(inc.structure_part or '-')[:15]:<15} {str(inc.fire_direction or '-'):<4} "
              f"{str(inc.fire_size or '-'):<7} {inc.details[:95]}")
    ok = [(r, rep, inc) for r, rep, inc, _ in results if inc is not None]
    lat = sorted(inc.edge_latency_ms for _, _, inc in ok)
    tps = [inc.tokens_per_s for _, _, inc in ok if inc.tokens_per_s]
    low = sum(1 for _, _, inc in ok if inc.needs_review)
    if lat:
        print(f"\nwall {wall:.1f}s for {len(results)} reports = {len(results) / wall * 60:.1f} reports/min | "
              f"latency p50 {lat[len(lat) // 2]} ms, p95 {lat[min(len(lat) - 1, int(len(lat) * 0.95))]} ms, mean {statistics.mean(lat):.0f} ms"
              + (f" | tokens/s mean {statistics.mean(tps):.1f}" if tps else ""))
    print(f"needs review: {low} | in hazard zone: {sum(1 for _, _, i in ok if i.in_hazard_zone)} | "
          f"location unknown: {sum(1 for _, _, i in ok if i.location_unknown)} | fallbacks: {sum(1 for _, _, i in ok if i.fallback)}")
    types: dict[str, int] = {}
    for _, _, inc in ok:
        types[inc.type] = types.get(inc.type, 0) + 1
    print(f"types: {types} | reports in DB: {db.report_count()} | incidents in DB: {db.incident_count()} | "
          f"LLM requests logged: {db.request_stats().get('edge_llm', {}).get('count', 0)}")
    top = sorted(ok, key=lambda t: -t[2].priority_score)[:5]
    print("top priorities:", [(r["id"], inc.type, inc.priority_score) for r, _, inc in top])

    print("\n--- section 3 check: XYZ High School example")
    rep, inc, action = ingest_text(XYZ, channel="Dispatch", model=model)
    if inc is not None:
        got = {"structure_part": inc.structure_part, "fire_direction": inc.fire_direction, "fire_size": inc.fire_size,
               "people_count": inc.people_count, "department": inc.department, "unit": inc.unit, "type": inc.type}
        want = {"structure_part": "east wing", "fire_direction": "N", "fire_size": "large", "people_count": 123}
        okk = all(str(got[k]).lower() == str(v).lower() for k, v in want.items())
        print(f"{'PASS' if okk else 'CHECK'}: {got} | details: {inc.details[:120]}")

    if not a.no_merge_demo and ok:
        print("\n--- merge demo: follow-up transmission about report 1 (same fire, same intersection)")
        rep, inc, action = ingest_text(FOLLOW_UP, channel="Engine 82", model=model)
        if inc is not None:
            print(f"action: {action} | {rep.link_reason}")
            print(f"incident {inc.id}: reports={inc.report_count} {inc.channels} severity={inc.severity} people={inc.people_count} "
                  f"needs={inc.needs} review={inc.review_reasons} priority={inc.priority_score}")
            print(f"linked reports: {[ (x.channel, x.text[:50] + '...') for x in db.list_reports(incident_id=inc.id)]}")
        print(f"reports in DB: {db.report_count()} | incidents in DB: {db.incident_count()}")
    return 0


def show(r: dict, rep, inc, action: str) -> None:
    if inc is None:
        print(f"--- report {r['id']} [{r['channel']}] FAILED: {rep.error}")
        return
    print(f"--- report {r['id']} [{r['channel']}] -> {action} incident {inc.id}  {inc.edge_latency_ms} ms, {inc.tokens_per_s} tok/s, "
          f"attempts {inc.extraction_attempts}{' FALLBACK' if inc.fallback else ''}  zones={inc.hazard_zones}")
    print(json.dumps(inc.model_dump(), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
