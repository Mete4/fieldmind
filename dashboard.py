"""FieldMind operator console (Streamlit): map first, CAD-style incident queue,
incident drawer with the radio timeline. Talks only to the local API."""
from __future__ import annotations

import datetime as dt
import math
import subprocess
import sys
from pathlib import Path

import httpx
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from app import config as C
import html as _html

import folium

from app.mapview import add_incident, make_map

API = C.API_URL
ROOT = Path(__file__).resolve().parent
RADIO = C.SAMPLE_DIR / "radio"
SEV = {5: ("Critical", "#e53935"), 4: ("High", "#fb8c00"), 3: ("Medium", "#fdd835"), 2: ("Low", "#42a5f5"), 1: ("Info", "#90a4ae")}
TYPE_SHORT = {"fire": "FIRE", "trapped": "TRAPPED", "medical": "MEDICAL", "road_blocked": "ROAD", "collapse": "COLLAPSE", "other": "OTHER"}
st.set_page_config(page_title="FieldMind", layout="wide", initial_sidebar_state="collapsed")
st_autorefresh(interval=C.DASHBOARD_REFRESH_MS, key="tick")
st.markdown("""<style>
.block-container{padding-top:0.6rem;padding-bottom:0;max-width:100%}
header[data-testid="stHeader"]{height:0}
div[data-testid="stVerticalBlockBorderWrapper"]{padding:0}
.fm-top{display:flex;align-items:center;gap:14px;font-size:14px;color:#ddd;padding:2px 0 6px 0;border-bottom:1px solid #333}
.fm-top b{font-size:17px;color:#fff}
.fm-alert{border-radius:6px;padding:8px 14px;margin:6px 0;color:#fff;font-size:20px;font-weight:700}
.fm-alert small{font-size:13px;font-weight:400;opacity:.9}
.fm-row{display:flex;gap:8px;align-items:stretch;padding:4px 6px;margin:3px 0;background:#1e1e1e;border-radius:4px;color:#eee;font-size:13px}
.fm-bar{width:6px;border-radius:3px;flex:none}
.fm-id{font-weight:700;min-width:70px}
.fm-dim{color:#999;font-size:12px}
.fm-kf{font-size:22px;font-weight:700;color:#fff;line-height:1.2}
.fm-sub{font-size:14px;color:#ccc}
.fm-call{border-left:3px solid #555;padding:4px 8px;margin:6px 0;font-size:13px;color:#ddd}
.stButton>button{padding:2px 10px;font-size:13px}
.stButton>button p{white-space:nowrap}
.fm-q-head{border-left:4px solid #ffb300;background:#2a2410;color:#ffd54f;font-weight:700;font-size:15px;padding:6px 12px;margin:6px 0 2px 0;border-radius:4px}
.fm-q{color:#ffe082;font-size:14px;padding-top:6px}
.fm-demo{color:#999;font-size:12px;text-transform:uppercase;letter-spacing:.06em;margin-right:4px}
</style>""", unsafe_allow_html=True)
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


def replay_running() -> bool:
    """True while a demo radio replay is still sending calls."""
    return subprocess.run(["pgrep", "-f", "scripts/play_radio.py"], capture_output=True).returncode == 0


def launch(args: list[str]) -> None:
    subprocess.Popen([sys.executable, *args], cwd=ROOT, stdout=open(ROOT / "logs" / "demo_play.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)


def ago(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        t = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        s = max(0, int((dt.datetime.now(dt.timezone.utc) - t).total_seconds()))
        return f"{s} s ago" if s < 60 else f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago"
    except Exception:  # noqa: BLE001
        return ""


def sev_of(i: dict) -> tuple[str, str]:
    return SEV.get(int(i.get("severity") or 3), ("Medium", "#fdd835"))


def what(i: dict) -> str:
    size = (i.get("fire_size") or "").capitalize()
    base = f"{size} fire".strip().capitalize() if i.get("type") == "fire" else TYPE_SHORT.get(i.get("type", ""), "").capitalize()
    if i.get("structure_part"):
        base += f", {i['structure_part']}"
    return base


def where(i: dict) -> str:
    return i.get("geocode_match") or i.get("location_text") or "location unknown"


def audio_for(r: dict) -> None:
    sf = r.get("source_file")
    if sf and sf != "live-stream" and (RADIO / sf).exists():
        st.audio(str(RADIO / sf))


health = api("GET", "/health") or {}
online = bool((health.get("network") or {}).get("online"))
incidents = api("GET", "/incidents") or []
by_id = {i["id"]: i for i in incidents}
by_label = {i.get("label"): i["id"] for i in incidents if i.get("label")}
units = api("GET", "/units") or []
units_by_inc: dict[str, list[str]] = {}
for u in units:
    if u.get("incident_id"):
        units_by_inc.setdefault(u["incident_id"], []).append(u["name"])

# ---------------------------------------------------------------- top bar
counts = f"{len(incidents)} incidents · {sum(i.get('report_count', 1) for i in incidents)} radio calls"
st.markdown(f"<div class='fm-top'><b>FieldMind&nbsp;·&nbsp;Paradise,&nbsp;CA</b> <span>{dt.datetime.now().strftime('%H:%M:%S')}</span>"
            f"<span style='color:{'#43a047' if online else '#fb8c00'}'>●</span><span class='fm-dim'>{'Hybrid · cloud linked' if online else 'Edge only · cloud will catch up'} · {C.SCENARIO_BANNER}</span>"
            f"<span class='fm-dim' style='margin-left:auto'>{counts}</span></div>", unsafe_allow_html=True)
next_step = "start" if not incidents else ("skip" if not ss["skipped"] else "cloud")   # the button to press next is the primary one
kind = lambda step: "primary" if step == next_step else "secondary"
with st.container(horizontal=True, gap="small", vertical_alignment="center"):
    st.markdown("<span class='fm-demo'>Demo</span>", width="content", unsafe_allow_html=True)
    if st.button("Reset demo", width="content"):
        subprocess.run(["pkill", "-f", "scripts/play_radio.py"], check=False)   # stop a replay that is still sending calls
        api("POST", "/reset")
        ss.update(selected=None, skipped=False, big_agent=None, seen_alert=0)
        st.rerun()
    if st.button("Start demo", type=kind("start"), width="content"):
        if replay_running():
            st.toast("The demo radio is already playing")
        else:
            launch(["scripts/play_radio.py", str(RADIO / "camp_live.json"), "--stream"])
            st.toast("Radio part 1: audio streamed through Whisper on the GPU")
    if st.button("Skip ahead 30 min", type=kind("skip"), width="content"):
        if replay_running():
            st.toast("Wait until the current radio calls are in")
        else:
            launch(["scripts/play_radio.py", str(RADIO / "camp_batch.json"), "--text", "--speed", "20"])
            ss["skipped"] = True
    if st.button("Ask cloud agent", type=kind("cloud"), width="content"):
        ss["big_agent"] = api("POST", "/recorded_cloud", timeout=60) if (ss["skipped"] or len(incidents) >= 12) else api("POST", "/situation", timeout=300)
if ss.get("api_error"):
    st.warning(ss.pop("api_error"))

# ---------------------------------------------------------------- alert banner
alerts = api("GET", "/alerts", params={"limit": 12}) or []
if alerts and alerts[0]["id"] > ss["seen_alert"]:
    for a in reversed([x for x in alerts if x["id"] > ss["seen_alert"]][:3]):
        st.toast(a["headline"])
    ss["seen_alert"] = alerts[0]["id"]
if alerts:
    a = alerts[0]
    inc_a = by_id.get(a["incident_id"], {})
    col = "#c62828" if a["level"] == "critical" else "#ef6c00"
    c1, c2 = st.columns([9, 1])
    c1.markdown(f"<div class='fm-alert' style='background:{col}'>{a['label']} &nbsp; {what(inc_a) or a['headline']} · {where(inc_a)}"
                f"{' · ' + str(inc_a.get('people_count')) + ' people' if inc_a.get('people_count') else ''}"
                f"<br><small>{a['why']} · {ago(a['ts'])}</small></div>", unsafe_allow_html=True)
    if c2.button("Open", key="open_top"):
        ss["selected"] = a["incident_id"]
        st.rerun()
    if len(alerts) > 1:
        with st.expander(f"{len(alerts) - 1} earlier alerts"):
            for a2 in alerts[1:]:
                e1, e2 = st.columns([9, 1])
                e1.markdown(f"<span style='color:{'#e53935' if a2['level'] == 'critical' else '#fb8c00'}'>●</span> <b>{a2['label']}</b> {a2['headline'].split(' · ', 1)[-1]} <span class='fm-dim'>{ago(a2['ts'])}</span>", unsafe_allow_html=True)
                if e2.button("Open", key=f"open{a2['id']}"):
                    ss["selected"] = a2["incident_id"]
                    st.rerun()

# ---------------------------------------------------------------- open questions and info requests (always on the main view)
unit_name = {u["id"]: u["name"] for u in units}
open_qs = api("GET", "/questions", params={"status": "open"}) or []
waiting = [r_ for r_ in (api("GET", "/info_requests") or []) if r_.get("status") != "answered"]
def render_questions() -> None:
    """One line with the open questions; it opens to answer or dismiss each one."""
    if open_qs or waiting:
        n_q = len(open_qs) + len(waiting)
        newest = open_qs[0]["text"] if open_qs else f"Waiting on {unit_name.get(waiting[0]['unit_id'], waiting[0]['unit_id'])}: {waiting[0]['what_to_find']}"
        head = f":orange[**{n_q} open question{'s' if n_q != 1 else ''} need{'s' if n_q == 1 else ''} an answer**] · newest: {newest[:90]}"

        def question_row(key: str, incident_id: str, text: str, ts: str, endpoint: str) -> None:
            with st.form(key, clear_on_submit=True, border=False):
                q1, q2, q3, q4, q5 = st.columns([1.1, 5, 3, 1, 1])
                if q1.form_submit_button(by_id.get(incident_id, {}).get("label") or "Incident"):
                    ss["selected"] = incident_id
                    st.rerun()
                q2.markdown(f"<div class='fm-q'>{_html.escape(text)} <span class='fm-dim'>{ago(ts)}</span></div>", unsafe_allow_html=True)
                ans = q3.text_input("Answer", key=f"{key}_text", label_visibility="collapsed", placeholder="Answer")
                if q4.form_submit_button("Answer") and ans.strip():
                    api("POST", endpoint, json={"answer": ans})
                    st.rerun()
                if q5.form_submit_button("Dismiss"):
                    api("POST", endpoint, json={"answer": "Dismissed by operator"})
                    st.rerun()

        with st.expander(head):
            with st.container(height=220, border=False):
                for q_ in open_qs:
                    question_row(f"q{q_['id']}", q_["incident_id"], q_["text"], q_["ts"], f"/questions/{q_['id']}/answer")
                for r_ in waiting:
                    question_row(f"r{r_['id']}", r_["incident_id"], f"Waiting on {unit_name.get(r_['unit_id'], r_['unit_id'])}: {r_['what_to_find']}",
                                 r_["ts"], f"/info_requests/{r_['id']}/answer")

tab_main, tab_ask, tab_units, tab_q, tab_hw, tab_src = st.tabs(["Console", "Ask the data", "Units", "Log", "Hardware", "Data sources"])

with tab_main:
    left, mid, right = st.columns([1, 2, 1])
    # -------- incident queue (CAD style)
    with left:
        depts = ["FD", "PD", "EMS"]
        pick = st.pills("Queue", depts, selection_mode="multi", default=depts, label_visibility="collapsed") if hasattr(st, "pills") else depts
        q = [i for i in incidents if not pick or any(d in pick for d in (i.get("department") or [])) or not i.get("department")]
        with st.container(height=560):
            if not q:
                st.caption("No incidents yet.")
            for i in q:
                word, col = sev_of(i)
                sel = i["id"] == ss["selected"]
                r1, r2 = st.columns([5, 1])
                r1.markdown(f"<div class='fm-row' style='{'outline:1px solid #888' if sel else ''}'><div class='fm-bar' style='background:{col}'></div>"
                            f"<div><span class='fm-id'>{i.get('label')}</span> {TYPE_SHORT.get(i.get('type'), '')}"
                            f"{' · ' + str(i.get('people_count')) + ' ppl' if i.get('people_count') else ''}<br>"
                            f"<span class='fm-dim'>{where(i)} · {ago(i.get('last_ts'))}"
                            f"{' · ' + ', '.join(units_by_inc.get(i['id'], [])) if units_by_inc.get(i['id']) else ''}</span>"
                            f"{'<br><span style=color:#ffb74d>Needs review: ' + ', '.join(i.get('review_reasons') or []) + '</span>' if i.get('needs_review') else ''}</div></div>", unsafe_allow_html=True)
                if r2.button("›", key=f"sel{i['id']}"):
                    ss["selected"] = i["id"]
                    st.rerun()
    # -------- map
    with mid:
        sel = by_id.get(ss["selected"])
        try:
            from streamlit_folium import st_folium

            m = make_map([], enabled_layers=set(), show_pois=False)   # fixed base map; pins go in a layer that updates in place
            pins = folium.FeatureGroup(name="Incidents")
            for inc in incidents:
                try:
                    add_incident(pins, inc)
                except Exception:  # noqa: BLE001  one bad record must not stop the map
                    continue
            out = st_folium(m, height=600, use_container_width=True, returned_objects=["last_object_clicked_tooltip"], key="map",
                            feature_group_to_add=pins,
                            center=(sel["lat"], sel["lon"]) if sel else None, zoom=15 if sel else None)
            tip = (out or {}).get("last_object_clicked_tooltip")
            if tip and tip != ss.get("map_tip_handled"):   # the component repeats the last click on every rerun
                ss["map_tip_handled"] = tip
                lab = tip.split(" | ")[0].strip()
                if lab in by_label and by_label[lab] != ss["selected"]:
                    ss["selected"] = by_label[lab]
                    st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"map: {e}")
        render_questions()
    # -------- drawer / radio feed
    with right:
        with st.container(height=600):
            if sel:
                d = api("GET", f"/incidents/{sel['id']}") or {}
                i, reps = d.get("incident", sel), d.get("reports", [])
                word, col = sev_of(i)
                h1, h2 = st.columns([5, 1])
                h1.markdown(f"<span class='fm-kf'>{i.get('label')}</span> &nbsp;<span style='background:{col};color:#000;border-radius:4px;padding:1px 8px;font-weight:700'>{word}</span>", unsafe_allow_html=True)
                if h2.button("Close"):
                    ss["selected"] = None
                    st.rerun()
                st.markdown(f"<div class='fm-kf'>{what(i)}</div><div class='fm-sub'>{where(i)}</div>"
                            + (f"<div style='color:#ffb74d'>Needs review: {', '.join(i.get('review_reasons') or [])}</div>" if i.get("needs_review") else ""), unsafe_allow_html=True)
                for q_ in [q for q in open_qs if q["incident_id"] == i["id"]]:
                    st.markdown(f"<div class='fm-q'>Open question: {_html.escape(q_['text'])}</div>", unsafe_allow_html=True)
                with st.expander("Ask a unit"):
                    with st.form(f"ask_{i['id']}", clear_on_submit=True, border=False):
                        names = {u["name"]: u["id"] for u in units}
                        who = st.selectbox("Unit", list(names)) if names else None
                        what_q = st.text_input("What to find out")
                        if st.form_submit_button("Send") and who and what_q.strip():
                            api("POST", "/info_requests", json={"incident_id": i["id"], "unit_id": names[who], "what_to_find": what_q})
                            st.rerun()
                facts = []
                if i.get("people_count") is not None:
                    facts.append(f"<b>{i['people_count']}</b> people")
                if i.get("fire_direction"):
                    facts.append(f"fire from <b>{i['fire_direction']}</b>")
                if i.get("fire_size"):
                    facts.append(f"<b>{i['fire_size']}</b> fire")
                if i.get("unit"):
                    facts.append("units: " + ", ".join(i["unit"]))
                if len(i.get("types") or []) > 1:
                    facts.append("also: " + ", ".join(t_ for t_ in i["types"] if t_ != i["type"]))
                if i.get("needs"):
                    facts.append("needs: " + ", ".join(i["needs"]))
                st.markdown("<div class='fm-sub'>" + " · ".join(facts) + "</div>", unsafe_allow_html=True)
                if i.get("details"):
                    st.caption(i["details"].replace("\n", "  \n"))
                st.markdown(f"**Radio timeline** · {len(reps)} calls")
                for r in sorted(reps, key=lambda x: x["ts"], reverse=True):
                    st.markdown(f"<div class='fm-call'><b>{r.get('unit') or r.get('channel')}</b> <span class='fm-dim'>{r.get('department')} · {ago(r['ts'])}</span><br>{r.get('text', '')}</div>", unsafe_allow_html=True)
                    audio_for(r)
                near = [o for o in incidents if o["id"] != i["id"] and not o.get("location_unknown") and
                        2 * 6371000 * math.asin(math.sqrt(math.sin(math.radians(o["lat"] - i["lat"]) / 2) ** 2 + math.cos(math.radians(i["lat"])) * math.cos(math.radians(o["lat"])) * math.sin(math.radians(o["lon"] - i["lon"]) / 2) ** 2)) <= 500]
                if near:
                    st.markdown("**Nearby (500 m)**")
                    for o in near:
                        st.markdown(f"<span style='color:{sev_of(o)[1]}'>●</span> <b>{o.get('label')}</b> {what(o)} · {where(o)}", unsafe_allow_html=True)
                avail = [u for u in units if u["status"] == "available"]
                if avail:
                    a1, a2 = st.columns([3, 1])
                    pick_u = a1.selectbox("Assign unit", [u["name"] for u in avail], label_visibility="collapsed")
                    if a2.button("Assign"):
                        uid = next(u["id"] for u in avail if u["name"] == pick_u)
                        api("POST", f"/units/{uid}/assign", json={"incident_id": i["id"], "status": "assigned"})
                        st.rerun()
            else:
                st.markdown("**Radio feed**")
                reps = api("GET", "/reports", params={"limit": 10}) or []
                if not reps:
                    st.caption("Waiting for radio traffic.")
                for r in reps:
                    inc = by_id.get(r.get("incident_id") or "")
                    tag = (f" → <b>{inc.get('label')}</b> {what(inc)}" + (f" <span class='fm-dim'>· pin in {r.get('pin_s')} s</span>" if r.get("pin_s") else "")) if inc else \
                        (" · extracting..." if r.get("status") == "received" else (" · <i>unclear: open question</i>" if (r.get("link_reason") or "").startswith("unclear") else ""))
                    st.markdown(f"<div class='fm-call'><b>{r.get('unit') or r.get('channel')}</b> <span class='fm-dim'>{r.get('department')} · {ago(r['ts'])}</span>{tag}<br>{r.get('text', '')}</div>", unsafe_allow_html=True)
                    audio_for(r)
    # -------- cloud agent output (below the main view)
    ba = ss.get("big_agent")
    if ba:
        st.markdown("#### " + ("Recorded cloud response" if ba.get("recorded") else ba.get('label', 'Situation')))
        sent = ba.get("sent") or {}
        upto = ""
        try:
            upto = dt.datetime.fromisoformat(ba.get("data_as_of").replace("Z", "+00:00")).astimezone().strftime("%H:%M")
        except Exception:  # noqa: BLE001
            pass
        st.caption((f"Replayed from a response written in advance by {ba.get('model')}; not a live API call" if ba.get("recorded") else f"{ba.get('model')} · {ba.get('latency_ms')} ms")
                   + (f" · Sent: {sent.get('incidents')} incidents, {sent.get('kb')} KB, no audio" if sent else "") + (f" · Based on data up to {upto}" if upto else ""))
        st.write(ba.get("situation", ""))
        cA, cB = st.columns(2)
        with cA:
            for p_ in ba.get("priorities") or []:
                ref = p_.get("ref") or {}
                st.markdown(f"**{p_.get('rank', '')}. {ref.get('label') or ref.get('place') or ''}** {p_['text']}")
            st.markdown("**Suggestions**")
            for s_ in [x for x in (api("GET", "/suggestions") or []) if x["status"] == "open"][:8]:
                with st.container(border=True):
                    st.markdown(f"**{s_['text']}**  \n<small>{s_['reason']}</small>", unsafe_allow_html=True)
                    b1, b2, _ = st.columns([1, 1, 3])
                    if b1.button("Accept", key=f"acc{s_['id']}"):
                        api("POST", f"/suggestions/{s_['id']}", json={"action": "accept"})
                        st.rerun()
                    if b2.button("Reject", key=f"rej{s_['id']}"):
                        api("POST", f"/suggestions/{s_['id']}", json={"action": "reject"})
                        st.rerun()
        with cB:
            for key, title in (("contradictions", "Contradictions"), ("open_questions", "Open questions"), ("resolved", "Resolved")):
                items = ba.get(key) or []
                if items:
                    st.markdown(f"**{title}**")
                    for it in items:
                        ref = it.get("ref") or {} if isinstance(it, dict) else {}
                        st.markdown(f"- **{ref.get('label') or ref.get('place') or ''}** {it.get('text', '') if isinstance(it, dict) else it}")

with tab_ask:
    with st.form("query_form"):
        qq = st.text_input("Question", placeholder="Which incidents have people trapped and no unit assigned?")
        if st.form_submit_button("Ask") and qq.strip():
            api("POST", "/query/start", json={"question": qq})
    qr = api("GET", "/query/last") or {}
    if qr.get("status") == "running":
        st.info(f"Working on: {qr.get('question')} (local model writes the SQL, DuckDB + Flock run it)")
    elif qr:
        st.markdown(f"**Q:** {qr.get('question')}  \n**Answer:** {qr.get('answer') or qr.get('error')}")
        st.caption(f"{qr.get('model')} · {qr.get('latency_ms')} ms · {len(qr.get('rows', []))} rows")
        st.code(qr.get("sql") or "", language="sql")
        if qr.get("rows"):
            st.dataframe(qr["rows"], use_container_width=True, height=min(300, 40 + 35 * len(qr["rows"])))
    with st.form("text_form", clear_on_submit=True):
        t1, t2 = st.columns([5, 1])
        text = t1.text_input("New report (text)")
        chan = t2.text_input("Unit / channel", value="FD Tact 1")
        if st.form_submit_button("Ingest") and text.strip():
            res = api("POST", "/ingest/text", timeout=120, json={"text": text, "channel": chan})
            if res and res.get("incident"):
                st.success(f"{res['link']} {res['incident'].get('label')} in {res['incident']['edge_latency_ms']} ms")
    au = st.file_uploader("Radio clip", type=["wav", "mp3", "m4a", "flac", "ogg"], key="audio_up")
    if au is not None and st.button("Transcribe + ingest clip"):
        res = api("POST", "/ingest/audio", timeout=300, files={"file": (au.name, au.getvalue())}, data={"channel": "upload"})
        if res and res.get("incident"):
            st.success(f"Whisper {res['stt']['latency_ms']} ms (RTF {res['stt']['rtf']}) → {res['incident'].get('label')}")

with tab_units:
    inc_opts = {f"{i.get('label')} · {what(i)} · {where(i)}": i["id"] for i in incidents}
    for u in units:
        c1, c2, c3 = st.columns([2, 2, 2])
        colr = {"available": "#43a047", "assigned": "#fb8c00", "out_of_service": "#777"}.get(u["status"], "#777")
        c1.markdown(f"<span style='color:{colr}'>●</span> **{u['name']}** <span class='fm-dim'>{u['department']}</span>", unsafe_allow_html=True)
        c2.markdown(f"{u['status'].replace('_', ' ')}{' · ' + by_id.get(u['incident_id'], {}).get('label', '') if u.get('incident_id') else ''}")
        if u["status"] != "available" and c3.button("Release", key=f"rel{u['id']}"):
            api("POST", f"/units/{u['id']}/assign", json={"incident_id": None, "status": "available"})
            st.rerun()

with tab_q:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Answered questions**")
        for q_ in (api("GET", "/questions", params={"status": "answered"}) or [])[:12]:
            st.caption(f"{by_id.get(q_['incident_id'], {}).get('label', '')}: {q_['text'][:90]} → {q_.get('answer') or ''}")
    with c2:
        st.markdown("**Info request**")
        with st.form("info_req"):
            unit_opts = {u["name"]: u["id"] for u in units}
            io = {f"{i.get('label')} · {where(i)}": i["id"] for i in incidents}
            iu = st.selectbox("Unit", list(unit_opts)) if unit_opts else None
            ii = st.selectbox("Incident", list(io)) if io else None
            w = st.text_input("What to find out")
            if st.form_submit_button("Send") and iu and ii and w.strip():
                api("POST", "/info_requests", json={"incident_id": io[ii], "unit_id": unit_opts[iu], "what_to_find": w})
                st.rerun()
        for r_ in (api("GET", "/info_requests") or [])[:6]:
            st.caption(f"{r_['status']}: {r_['unit_id']} → {by_id.get(r_['incident_id'], {}).get('label', '')}: {r_['what_to_find'][:80]}" + (f" · {r_['answer']}" if r_.get("answer") else ""))
        st.markdown("**Decisions**")
        for d_ in (api("GET", "/decisions", params={"limit": 8}) or []):
            st.caption(f"{d_['ts'][11:19]} {d_['kind']} {d_['target_id']}: {d_['detail'][:70]}")

with tab_hw:
    m = api("GET", "/metrics") or {}
    if m:
        dev, gpu, mem, pipe = m.get("device", {}), m.get("gpu", {}), m.get("memory", {}), m.get("pipeline", {})
        k = st.columns(6)
        k[0].metric("Device", dev.get("product", "?"), dev.get("arch"))
        k[1].metric("GPU", gpu.get("name", "?"), f"{gpu.get('utilization_pct')} % util")
        k[2].metric("Unified memory", f"{mem.get('used_gib')} / {mem.get('total_gib')} GiB")
        k[3].metric("Reports / min", pipe.get("reports_per_min"), f"{pipe.get('reports_total')} total")
        k[4].metric("LLM tokens/s", pipe.get("llm_tokens_per_s_avg"))
        k[5].metric("Whisper RTF", pipe.get("whisper_rtf_avg"), f"{pipe.get('audio_reports')} clips")
        st.markdown("**Models resident:** " + ", ".join(f"{x.get('model')} {x.get('size_gib')} GiB" for x in m.get("models_loaded", []) if x.get("model"))
                    + f" · organizer **{health.get('organizer')}** (fallback {health.get('fallback')}) · STT {health.get('stt') or 'Whisper large-v3 (transformers, GPU)'}")
        st.json(pipe.get("routes", {}), expanded=False)
        st.caption(f"{C.DEVICE_LABEL} · all numbers measured live")

with tab_src:
    hz = api("GET", "/hazards") or {}
    st.markdown("**Base map:** OpenStreetMap tiles cached once (z10-16), (c) OpenStreetMap contributors, ODbL. **Place names:** OpenStreetMap via Overpass. **Incidents:** simulated radio traffic.")
    for L in hz.get("layers", []):
        st.markdown(f"- **{L['name']}** ({L.get('features')} features): {L.get('source')} - [{L.get('source_url')}]({L.get('source_url')}) - {L.get('license')}")
