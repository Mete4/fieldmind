"""Strip personal data before anything leaves the device: phone numbers,
e-mail addresses, SSN-like numbers, house numbers and personal names that
follow a cue word ("caller is Jane Doe", "Mr. Smith"). Radio call signs and
real place names (gazetteer) are never touched. Returns the text and counts."""
from __future__ import annotations

import json
import re

from app import config as C

PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
HOUSE_NUMBER = re.compile(r"\b\d{2,6}(?=\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+(?:Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl|Highway|Hwy|Boulevard|Blvd)\b)")
NAME_CUE = re.compile(
    r"\b(?i:my name is|name is|caller is|caller named|resident named|victim named|patient named|named|"
    r"this is|speaking with|identified as|mr\.?|mrs\.?|ms\.?|miss|dr\.?)\s+"
    r"((?:[A-Z][a-z]{1,20})(?:\s+[A-Z][a-z]{1,20}){0,2})")
# words that can follow a cue but are not names (radio talk, units, roles, places)
SAFE_WORDS = {"engine", "medic", "truck", "battalion", "command", "dispatch", "station", "rescue", "unit", "chief",
              "air", "attack", "water", "paradise", "magalia", "skyway", "cal", "fire", "county", "butte", "pd",
              "police", "sheriff", "the", "a", "an", "an", "we", "it", "i", "he", "she", "they", "our", "your",
              "ems", "ambulance", "control", "ic", "incident", "staging", "ops", "operations", "base", "camp"}


def _gazetteer_words() -> set[str]:
    try:
        return {w.lower() for e in json.loads(C.GAZETTEER_PATH.read_text()) for w in e["name"].split()}
    except Exception:  # noqa: BLE001
        return set()


_PLACE_WORDS: set[str] | None = None


def redact(text: str) -> tuple[str, dict[str, int]]:
    global _PLACE_WORDS
    if _PLACE_WORDS is None:
        _PLACE_WORDS = _gazetteer_words()
    counts = {"phone": 0, "email": 0, "ssn": 0, "house_number": 0, "name": 0}
    out, n = EMAIL.subn("[email]", text)
    counts["email"] += n
    out, n = SSN.subn("[id-number]", out)
    counts["ssn"] += n
    out, n = PHONE.subn("[phone]", out)
    counts["phone"] += n
    out, n = HOUSE_NUMBER.subn("[number]", out)
    counts["house_number"] += n

    def _name(m: re.Match) -> str:
        cand = m.group(1)
        words = cand.split()
        if all(w.lower() in SAFE_WORDS or w.lower() in _PLACE_WORDS for w in words):
            return m.group(0)
        counts["name"] += 1
        return m.group(0)[: m.start(1) - m.start(0)] + "[name]"

    out = NAME_CUE.sub(_name, out)
    return out, counts


def redacted_bytes(text: str) -> int:
    return len(text.encode("utf-8"))
