"""Closes open questions that a later radio call answers. The fast local model
reads the new call and the open questions and names the ones the call answers;
each one is marked answered with the unit and the answer, so it leaves the
main view and stays in the Log."""
from __future__ import annotations

import json
import threading
import time

from app import config as C
from app import db
from app.schemas import RequestLog

_lock = threading.Lock()

SYSTEM = ("You help a fire dispatcher. You get one new radio call and a numbered list of open questions. "
          "Name only the questions that the new call clearly answers. Return JSON: "
          '{"answered": [{"question": <number>, "answer": "<the answer in at most 15 words>"}]}. '
          'If the call answers none, return {"answered": []}.')


def resolve_open_questions(call_text: str, unit: str) -> list[int]:
    """Marks the open questions that call_text answers as answered. Returns their ids."""
    with _lock:
        questions = [q for q in db.list_questions("open") if call_text.strip()[:60] not in q["text"]]
        if not questions:
            return []
        listing = "\n".join(f"{n}. {q['text']}" for n, q in enumerate(questions, 1))
        t0 = time.perf_counter()
        try:
            import ollama

            r = ollama.Client(host=C.OLLAMA_HOST, timeout=25).chat(
                model=C.FALLBACK_MODEL, keep_alive=C.OLLAMA_KEEP_ALIVE, format="json",
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": f"New call from {unit}: {call_text[:600]}\n\nOpen questions:\n{listing}"}],
                options={"temperature": 0.0, "num_ctx": 4096, "num_predict": 200})
            ms = int((time.perf_counter() - t0) * 1000)
            db.log_request(RequestLog(route="edge_question_resolve", model=C.FALLBACK_MODEL, latency_ms=ms,
                                      tokens_in=int(r.prompt_eval_count or 0), tokens_out=int(r.eval_count or 0), ok=True))
            items = json.loads(r.message.content or "{}").get("answered") or []
        except Exception:  # noqa: BLE001  a failed check leaves the questions open
            return []
        closed: list[int] = []
        for item in items:
            try:
                q = questions[int(item["question"]) - 1]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            answer = str(item.get("answer") or "").strip()
            if answer and q["id"] not in closed:
                db.answer_question(q["id"], f"Answered by radio, {unit}: {answer}")
                closed.append(q["id"])
        return closed


def resolve_open_questions_async(call_text: str, unit: str) -> None:
    threading.Thread(target=lambda: resolve_open_questions(call_text, unit), daemon=True).start()
