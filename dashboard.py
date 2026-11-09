"""FieldMind operator dashboard (Streamlit). Talks only to the local API.
Run: .venv/bin/streamlit run dashboard.py --server.port 8501"""
from __future__ import annotations

import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path

import httpx
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from app import config as C
from app.mapview import SEVERITY_COLORS, make_map

API = C.API_URL
ROOT = Path(__file__).resolve().parent
RADIO = C.SAMPLE_DIR / "radio"
SEV_WORD = {5: ("Critical", "#d7191c"), 4: ("High", "#f46d43"), 3: ("Medium", "#fdae61"), 2: ("Low", "#74add1"), 1: ("Info", "#2b83ba")}
st.set_page_config(page_title="FieldMind", layout="wide", page_icon="🔥")
st_autorefresh(interval=C.DASHBOARD_REFRESH_MS, key="tick")
ss = st.session_state
ss.setdefault("selected", None)
ss.setdefault("seen_alert", 0)
ss.setdefault("skipped", False)
ss.setdefault("big_agent", None)


def api(method: str, path: str, timeout: float = 5, **kw):
    try:
        r = httpx.request(method, f"{API}{path}", timeout=timeout, **kw)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001
        ss["api_error"] = f"{method} {path}: {type(e).__name__}: {str(e)[:120]}"
        return None


def launch(args: list[str]) -> None:
    subprocess.Popen([sys.executable, *args], cwd=ROOT, stdout=open(ROOT / "logs" / "demo_play.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)


def age(ts: str | None) -> str:
    if not ts:
        return "no data yet"
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        t = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        secs = max(0, (dt.datetime.now(dt.timezone.utc) - t).total_seconds())
        return f"based on data up to {t.astimezone().strftime('%H:%M:%S')}, {int(secs // 60)} min {int(secs % 60)} s old"
    except Exception:  # noqa: BLE001
        return ts


def sev_badge(sev: int) -> str:
    word, col = SEV_WORD.get(int(sev or 3), ("Medium", "#fdae61"))
    return f"<span style='background:{col};color:#fff;border-radius:4px;padding:0 6px;font-weight:700'>{word}</span>"


def fields_line(i: dict) -> str:
    parts = []
    if i.get("people_count") is not None:
        parts.append(f"{i['people_count']} people")
    if i.get("structure_part"):
        parts.append(f"part: {i['structure_part']}")
    if i.get("fire_direction"):
        parts.append(f"fire from {i['fire_direction']}")
    if i.get("fire_size"):
        parts.append(f"{i['fire_size']} fire")
    if i.get("department"):
        parts.append("/".join(i["department"]) + (": " + ", ".join(i.get("unit") or []) if i.get("unit") else ""))
    if i.get("hazard_zones"):
        parts.append("zone: " + ", ".join(z.replace("_", " ") for z in i["hazard_zones"]))
    return " · ".join(parts)


def audio_for(report: dict):
    sf = report.get("source_file")
    if sf and sf != "live-stream":
        p = RADIO / sf
        if p.exists():
            st.audio(str(p))


def card(i: dict, show_transcript: bool = True) -> None:
    where = i.get("geocode_match") or i.get("location_text") or "location unknown"
    st.markdown(
        f"<div style='border-left:6px solid {SEV_WORD.get(int(i.get('severity') or 3), ('', '#999'))[1]};padding:4px 8px;margin:4px 0;background:#f7f7f7;color:#111'>"
        f"<b>{i.get('label') or i.get('id', '')[:6]}</b> {sev_badge(i.get('severity', 3))} <b>{i.get('type', '')}</b>"
        f"{' (' + ', '.join(t for t in i.get('types', []) if t != i.get('type')) + ')' if len(i.get('types', [])) > 1 else ''}"
        f" · {i.get('report_count', 1)} report{'s' if i.get('report_count', 1) != 1 else ''}<br>"
        f"<small><b>{where}</b> · {fields_line(i)}</small><br>"
        f"<small><i>{(i.get('summary') or '')[:160]}</i>"
        + (f"<br><span style='color:#444'>“{(i.get('transcript') or '')[:220]}”</span>" if show_transcript else "") + "</small></div>",
        unsafe_allow_html=True)


health = api("GET", "/health") or {}
net = health.get("network", {})
online = bool(net.get("online"))

# ------------------------------------------------------------ header (small)
h1, h2 = st.columns([6, 3])
with h1:
    st.markdown(f"### 🔥 FieldMind - {C.TOWN_NAME} &nbsp; <small style='color:#888'>{C.SCENARIO_BANNER} · {C.SCENARIO_NAME}</small>", unsafe_allow_html=True)
with h2:
    st.markdown(f"<div style='text-align:right;font-size:13px'>network: <b style='color:{'#1a9850' if online else '#d73027'}'>{'online' if online else 'offline'}</b> · "
                f"Cloud agent: <b>recorded response</b> · organizer: <b>{health.get('organizer', '?')}</b> · STT: {health.get('stt') or 'Whisper large-v3'}<br>"
                f"<small>Running on Lenovo ThinkStation PGX · every model on the edge</small></div>", unsafe_allow_html=True)
if ss.get("api_error"):
    st.warning(ss.pop("api_error"))

tab_cc, tab_ops, tab_hw, tab_src = st.tabs(["Command Center", "Operator", "Hardware", "Data sources"])
incidents = api("GET", "/incidents") or []
by_id = {i["id"]: i for i in incidents}
by_label = {i.get("label"): i["id"] for i in incidents if i.get("label")}

with tab_cc:
    # ---- demo buttons
    b = st.columns([1.1, 1.4, 1.6, 1.4, 1.4, 2])
    if b[0].button("Reset demo", type="secondary"):
        api("POST", "/reset")
        ss.update(selected=None, skipped=False, big_agent=None, seen_alert=0)
        st.rerun()
    if b[1].button("▶ Play radio (part 1)", type="primary"):
        launch(["scripts/play_radio.py", str(RADIO / "camp_live.json"), "--stream"])
        st.toast("Playing camp_live.json: audio streamed through Whisper", icon="📻")
    if b[2].button("⏩ Skip ahead 30 minutes"):
        launch(["scripts/play_radio.py", str(RADIO / "camp_batch.json"), "--text", "--speed", "20"])
        ss["skipped"] = True
        st.toast("Sending the 30-minute batch as text at 20x", icon="⏩")
    if b[3].button("☁ Ask the big agent"):
        with st.spinner("..."):
            if ss["skipped"] or len(incidents) >= 12:
                ss["big_agent"] = api("POST", "/recorded_cloud", timeout=60)
            else:
                ss["big_agent"] = api("POST", "/situation", timeout=300)
    b[4].button("🎙 Live mic: not today", disabled=True, help="Pinned: live microphone input is not part of today's build.")
    b[5].caption(f"{len(incidents)} incidents · {sum(i.get('report_count', 1) for i in incidents)} reports")

    # ---- alerts
    alerts = api("GET", "/alerts", params={"limit": 20}) or []
    if alerts and alerts[0]["id"] > ss["seen_alert"]:
        for a in reversed([x for x in alerts if x["id"] > ss["seen_alert"]][:3]):
            st.toast(a["headline"], icon="🚨")
        ss["seen_alert"] = alerts[0]["id"]
    st.markdown("#### Alerts")
    if not alerts:
        st.caption("No alerts. The orchestrator raises one when an incident is severe, involves many people, trapped people, a school or hospital, a burnover or a blocked evacuation route, and again when it gets worse.")
    for a in alerts[:6]:
        c1, c2 = st.columns([8, 1])
        col = "#d7191c" if a["level"] == "critical" else "#f46d43"
        c1.markdown(f"<div style='border-left:6px solid {col};padding:3px 8px;background:#fff3f0;color:#111'><b>{a['headline']}</b>"
                    f"<br><small>{a['why']} <span style='color:#888'>· {a['trigger']} · {a['ts'][11:19]}</span></small></div>", unsafe_allow_html=True)
        if c2.button("Open", key=f"open{a['id']}"):
            ss["selected"] = a["incident_id"]
            st.rerun()

    # ---- map + right panel
    left, right = st.columns([3, 2])
    with left:
        sel = by_id.get(ss["selected"])
        center = (sel["lat"], sel["lon"]) if sel else None
        try:
            from streamlit_folium import st_folium

            m = make_map(incidents, enabled_layers={"fire", "camp_fire"}, show_pois=False, center=center, zoom=15 if sel else None)
            out = st_folium(m, height=560, use_container_width=True, returned_objects=["last_object_clicked_tooltip"], key="map")
            tip = (out or {}).get("last_object_clicked_tooltip")
            if tip:
                lab = tip.split(" | ")[0].strip()
                if lab in by_label and by_label[lab] != ss["selected"]:
                    ss["selected"] = by_label[lab]
                    st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"map: {e}")
        pick = st.selectbox("Incident", ["-"] + [f"{i.get('label')} · {i['type']} · {i.get('geocode_match') or ''}" for i in incidents],
                            index=0 if not sel else 1 + [i["id"] for i in incidents].index(sel["id"]), label_visibility="collapsed")
        if pick != "-" and by_label.get(pick.split(" · ")[0]) != ss["selected"]:
            ss["selected"] = by_label.get(pick.split(" · ")[0])
            st.rerun()
        st.caption("Pins: colour = severity, badge = linked reports. Map (c) OpenStreetMap contributors (ODbL), CAL FIRE FHSZ, Camp Fire perimeter CAL FIRE FRAP.")
    with right:
        if sel:
            d = api("GET", f"/incidents/{sel['id']}") or {}
            i, reps = d.get("incident", sel), d.get("reports", [])
            hdr = st.columns([6, 1])
            hdr[0].markdown(f"### {i.get('label')} {sev_badge(i.get('severity'))} {i.get('type')}", unsafe_allow_html=True)
            if hdr[1].button("✕", key="close_panel"):
                ss["selected"] = None
                st.rerun()
            rows = [("Where", i.get("geocode_match") or i.get("location_text") or "unknown"), ("People", i.get("people_count")),
                    ("Part of building", i.get("structure_part")), ("Fire from", i.get("fire_direction")), ("Fire size", i.get("fire_size")),
                    ("Departments", "/".join(i.get("department") or [])), ("Units", ", ".join(i.get("unit") or [])),
                    ("Types reported", ", ".join(i.get("types") or [])), ("Hazard zones", ", ".join(i.get("hazard_zones") or [])),
                    ("Needs", ", ".join(i.get("needs") or [])), ("Status", i.get("status"))]
            st.markdown("<table style='font-size:13px'>" + "".join(f"<tr><td style='color:#888;padding:1px 10px 1px 0'>{k}</td><td><b>{v}</b></td></tr>" for k, v in rows if v not in (None, "", [])) + "</table>", unsafe_allow_html=True)
            if i.get("details"):
                st.markdown("**Details**")
                st.caption(i["details"].replace("\n", "  \n"))
            st.markdown(f"**Reports ({len(reps)})**, newest first")
            for r in sorted(reps, key=lambda x: x["ts"], reverse=True):
                with st.container(border=True):
                    st.markdown(f"<small>{r['ts'][11:19]} · {r.get('channel')} · <b>{r.get('unit') or '-'}</b> ({r.get('department')}) · {r.get('input_type')}"
                                + (f" · Whisper {r.get('stt_latency_ms')} ms, RTF {r.get('stt_rtf')}" if r.get("stt_rtf") else "") + "</small>", unsafe_allow_html=True)
                    audio_for(r)
                    st.write(r.get("text", ""))
            near = [o for o in incidents if o["id"] != i["id"] and not o.get("location_unknown") and
                    2 * 6371000 * math.asin(math.sqrt(math.sin(math.radians(o["lat"] - i["lat"]) / 2) ** 2 + math.cos(math.radians(i["lat"])) * math.cos(math.radians(o["lat"])) * math.sin(math.radians(o["lon"] - i["lon"]) / 2) ** 2)) <= 500]
            if near:
                st.markdown("**Within 500 m**")
                for o in near:
                    card(o, show_transcript=False)
        else:
            st.markdown("#### Radio feed")
            reps = api("GET", "/reports", params={"limit": 10}) or []
            if not reps:
                st.caption("Waiting for radio traffic. Press ▶ Play radio (part 1).")
            for r in reps:
                with st.container(border=True):
                    st.markdown(f"<small>{r['ts'][11:19]} · {r.get('channel')} · <b>{r.get('unit') or '-'}</b> ({r.get('department')})"
                                + (f" · Whisper {r.get('stt_latency_ms')} ms" if r.get("stt_rtf") else "") + "</small>", unsafe_allow_html=True)
                    audio_for(r)
                    st.write(r.get("text", ""))
                    inc = by_id.get(r.get("incident_id") or "")
                    if inc:
                        card(inc, show_transcript=False)
                    elif r.get("status") == "received":
                        st.caption("extracting...")

    # ---- big agent
    ba = ss.get("big_agent")
    if ba:
        st.markdown("#### " + ("☁ Recorded cloud response" if ba.get("recorded") else f"☁ {ba.get('label', 'Situation')}"))
        if ba.get("recorded"):
            st.caption(f"Replayed from a response written in advance by {ba.get('model')}; NOT a live API call · {age(ba.get('data_as_of'))}")
        else:
            st.caption(f"{ba.get('model')} · {ba.get('latency_ms')} ms · {age(ba.get('data_as_of'))}")
        st.write(ba.get("situation", ""))
        cA, cB = st.columns(2)
        with cA:
            if ba.get("priorities"):
                st.markdown("**Priorities**")
                for p_ in ba["priorities"]:
                    ref = p_.get("ref") or {}
                    st.markdown(f"{p_.get('rank', '')}. **{ref.get('label') or ref.get('place') or ''}** {p_['text']}")
            st.markdown("**Suggestions**")
            for s_ in [x for x in (api("GET", "/suggestions") or []) if x["status"] == "open"][:8]:
                with st.container(border=True):
                    st.markdown(f"**{s_['text']}**  \n<small>{s_['reason']}</small>", unsafe_allow_html=True)
                    a1, a2, _ = st.columns([1, 1, 3])
                    if a1.button("Accept", key=f"acc{s_['id']}"):
                        api("POST", f"/suggestions/{s_['id']}", json={"action": "accept"})
                        st.rerun()
                    if a2.button("Reject", key=f"rej{s_['id']}"):
                        api("POST", f"/suggestions/{s_['id']}", json={"action": "reject"})
                        st.rerun()
        with cB:
            for key, title in (("contradictions", "Contradictions"), ("open_questions", "Open questions"), ("resolved", "Resolved")):
                items = ba.get(key) or []
                if items:
                    st.markdown(f"**{title}**")
                    for it in items:
                        ref = it.get("ref") or {}
                        st.markdown(f"- **{ref.get('label') or ref.get('place') or ''}** {it.get('text', '')}" if isinstance(it, dict) else f"- {it}")

    # ---- priority list, query, inputs
    st.markdown(f"#### Priority list ({len(incidents)})")
    for i in incidents[:30]:
        card(i)
    st.markdown("#### Ask the data")
    with st.form("query_form"):
        q = st.text_input("Question", placeholder="Which incidents have people trapped inside?")
        if st.form_submit_button("Ask") and q.strip():
            with st.spinner("local model writing SQL, DuckDB + Flock running it..."):
                ss["query_result"] = api("POST", "/query", timeout=180, json={"question": q})
    qr = ss.get("query_result")
    if qr:
        st.markdown(f"**Answer:** {qr.get('answer') or qr.get('error')}")
        st.caption(f"{qr.get('model')} · {qr.get('latency_ms')} ms · {len(qr.get('rows', []))} rows")
        st.code(qr.get("sql") or "", language="sql")
        if qr.get("rows"):
            st.dataframe(qr["rows"], use_container_width=True, height=min(300, 40 + 35 * len(qr["rows"])))
        st.caption(f"incident ids: {', '.join(qr.get('incident_ids', [])) or '-'} · report ids: {', '.join(qr.get('report_ids', [])) or '-'}")
    with st.form("text_form", clear_on_submit=True):
        t1, t2 = st.columns([5, 1])
        text = t1.text_input("New report (text)")
        chan = t2.text_input("Channel", value="FD Tact 1")
        if st.form_submit_button("Ingest") and text.strip():
            res = api("POST", "/ingest/text", timeout=120, json={"text": text, "channel": chan})
            if res and res.get("incident"):
                st.success(f"{res['link']} {res['incident'].get('label')}: {res['incident']['type']} in {res['incident']['edge_latency_ms']} ms")
    au = st.file_uploader("Radio clip", type=["wav", "mp3", "m4a", "flac", "ogg"], key="audio_up")
    if au is not None and st.button("Transcribe + ingest clip"):
        res = api("POST", "/ingest/audio", timeout=300, files={"file": (au.name, au.getvalue())}, data={"channel": "upload"})
        if res and res.get("incident"):
            st.success(f"{res['stt']['backend']} {res['stt']['latency_ms']} ms (rtf {res['stt']['rtf']}) -> {res['incident'].get('label')} {res['incident']['type']}")

with tab_ops:
    units = api("GET", "/units") or []
    c1, c2 = st.columns([3, 2])
    with c1:
        st.markdown("#### Situation (edge or recorded)")
        if st.button("Update edge situation"):
            with st.spinner("local 32B..."):
                api("POST", "/situation", timeout=300)
        sit = api("GET", "/situation") or {}
        payload = sit.get("payload") or {}
        if sit:
            label = "Recorded cloud response" if payload.get("recorded") else ("Cloud situation" if sit.get("source") == "cloud" else "Edge summary (local 32B)")
            st.caption(f"**{label}** · {sit.get('model')} · {age(sit.get('data_as_of'))}")
            st.write(payload.get("situation", ""))
        st.markdown("#### Open questions")
        for q_ in (api("GET", "/questions", params={"status": "open"}) or [])[:8]:
            with st.form(f"q{q_['id']}"):
                st.markdown(f"**{q_['text']}** <small>{by_id.get(q_['incident_id'], {}).get('label', '')}</small>", unsafe_allow_html=True)
                ans = st.text_input("Answer", key=f"ans{q_['id']}")
                if st.form_submit_button("Answer") and ans.strip():
                    api("POST", f"/questions/{q_['id']}/answer", json={"answer": ans})
                    st.rerun()
    with c2:
        st.markdown("#### Units")
        inc_opts = {f"{i.get('label')} · {i['type']} · {i.get('geocode_match') or 'unknown'}": i["id"] for i in incidents}
        for u in units:
            with st.container(border=True):
                colr = {"available": "#1a9850", "assigned": "#fdae61", "out_of_service": "#999"}.get(u["status"], "#999")
                st.markdown(f"<span style='color:{colr};font-weight:700'>●</span> **{u['name']}** ({u['department']}) {u['status']}"
                            f"{' → ' + by_id.get(u['incident_id'], {}).get('label', u['incident_id'][:6]) if u.get('incident_id') else ''}", unsafe_allow_html=True)
                if inc_opts:
                    with st.form(f"as{u['id']}"):
                        s_ = st.selectbox("Assign to", list(inc_opts), key=f"sel{u['id']}", label_visibility="collapsed")
                        x1, x2 = st.columns(2)
                        if x1.form_submit_button("Assign"):
                            api("POST", f"/units/{u['id']}/assign", json={"incident_id": inc_opts[s_], "status": "assigned"})
                            st.rerun()
                        if x2.form_submit_button("Release"):
                            api("POST", f"/units/{u['id']}/assign", json={"incident_id": None, "status": "available"})
                            st.rerun()
        st.markdown("#### Info request")
        with st.form("info_req"):
            unit_opts = {f"{u['name']} ({u['department']})": u["id"] for u in units}
            iu = st.selectbox("Unit", list(unit_opts)) if unit_opts else None
            ii = st.selectbox("Incident", list(inc_opts)) if inc_opts else None
            what = st.text_input("What to find out")
            if st.form_submit_button("Send") and iu and ii and what.strip():
                api("POST", "/info_requests", json={"incident_id": inc_opts[ii], "unit_id": unit_opts[iu], "what_to_find": what})
                st.rerun()
        for r_ in (api("GET", "/info_requests") or [])[:6]:
            st.caption(f"{r_['status']}: {r_['unit_id']} → {by_id.get(r_['incident_id'], {}).get('label', r_['incident_id'][:6])}: {r_['what_to_find'][:80]}" + (f" · answer: {r_['answer']}" if r_.get("answer") else ""))
        st.markdown("#### Decisions log")
        for d_ in (api("GET", "/decisions", params={"limit": 8}) or []):
            st.caption(f"{d_['ts'][11:19]} {d_['kind']} {d_['target_id']}: {d_['detail'][:70]}")

with tab_hw:
    m = api("GET", "/metrics") or {}
    if m:
        dev, gpu, mem, pipe = m.get("device", {}), m.get("gpu", {}), m.get("memory", {}), m.get("pipeline", {})
        k = st.columns(6)
        k[0].metric("Device", dev.get("product", "?"), dev.get("arch"))
        k[1].metric("GPU", gpu.get("name", "?"), f"{gpu.get('utilization_pct')} % util")
        k[2].metric("Unified memory used", f"{mem.get('used_gib')} / {mem.get('total_gib')} GiB", f"{mem.get('percent')} %")
        k[3].metric("Reports / min", pipe.get("reports_per_min"), f"{pipe.get('reports_total')} total")
        k[4].metric("LLM tokens/s", pipe.get("llm_tokens_per_s_avg"), f"active {sum((pipe.get('active_streams') or {}).values())}")
        k[5].metric("Whisper RTF", pipe.get("whisper_rtf_avg"), f"{pipe.get('audio_reports')} clips")
        st.markdown("**Models resident (Ollama):** " + ", ".join(f"{x.get('model')} {x.get('size_gib')} GiB" for x in m.get("models_loaded", []) if x.get("model"))
                    + f" · organizer **{health.get('organizer')}**, fallback {health.get('fallback')}")
        st.json(pipe.get("routes", {}), expanded=False)
        st.caption(f"{C.DEVICE_LABEL} · all numbers measured live (psutil, nvidia-smi, Ollama, request log)")
    else:
        st.info("API not reachable")

with tab_src:
    hz = api("GET", "/hazards") or {}
    st.markdown("**Base map:** OpenStreetMap raster tiles, cached once for z10-16, (c) OpenStreetMap contributors, ODbL. "
                "**Place names:** OpenStreetMap via Overpass (ODbL). **Incidents:** simulated (radio traffic written for the exercise).")
    for L in hz.get("layers", []):
        st.markdown(f"- **{L['name']}** ({L.get('features')} features): {L.get('source')} - [{L.get('source_url')}]({L.get('source_url')}) - {L.get('license')}")
    st.caption("Details and download links: data/hazards/README.md")
