"""Turn model responses into exactly one summary per image, or fail loudly."""
from __future__ import annotations

import json
import re

_NUMBERED = re.compile(r"^\s*(?:image\s*)?(\d+)\s*[\.\):\-]\s*(.+?)\s*$", re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_WS = re.compile(r"\s+")


def clean_summary(text: str) -> str:
    """Collapse whitespace and strip wrapping quotes/bullets without altering content."""
    s = _WS.sub(" ", str(text)).strip()
    s = s.strip("-*• ").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        s = s[1:-1].strip()
    return s


def parse_summaries(text: str, expected: int) -> list[str] | None:
    """Parse a response into *expected* summaries, in order.

    Accepts a JSON array of strings (preferred), a JSON array of objects with a
    "summary"/"description" field, or numbered lines. Returns None when the
    count does not match so the caller can re-ask for the images individually.
    """
    if not text or expected <= 0:
        return None
    body = _FENCE.sub("", text.strip())

    start, end = body.find("["), body.rfind("]")
    if start != -1 and end > start:
        try:
            data = json.loads(body[start : end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            items: list[str] = []
            for entry in data:
                if isinstance(entry, str):
                    items.append(clean_summary(entry))
                elif isinstance(entry, dict):
                    val = entry.get("summary") or entry.get("description") or entry.get("text")
                    items.append(clean_summary(val) if isinstance(val, str) else "")
                else:
                    items.append("")
            if len(items) == expected and all(items):
                return items
            # Fall through to the numbered-line parser only if JSON was not usable.

    numbered: dict[int, str] = {}
    for line in body.splitlines():
        m = _NUMBERED.match(line)
        if m:
            idx = int(m.group(1))
            if idx not in numbered:
                numbered[idx] = clean_summary(m.group(2))
    if numbered and set(numbered) == set(range(1, expected + 1)) and all(numbered.values()):
        return [numbered[i] for i in range(1, expected + 1)]

    if expected == 1:
        # A single image may legitimately come back as one bare line.
        lines = [clean_summary(l) for l in body.splitlines() if l.strip()]
        if len(lines) == 1 and lines[0]:
            return lines
    return None


def parse_tab_lines(text: str) -> dict[str, str]:
    """Parse 'name<TAB>summary' lines (Local Claude CLI engine) into {name: summary}."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "\t" not in line:
            continue
        name, summary = line.split("\t", 1)
        name = name.strip().strip('"').strip("`").strip()
        summary = clean_summary(summary)
        if name and summary and name not in out:
            out[name] = summary
    return out
