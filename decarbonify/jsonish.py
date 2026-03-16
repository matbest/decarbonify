from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


def _escape_newlines_in_json_strings(s: str) -> str:
    """Escape literal newlines inside JSON string literals.

    LLMs sometimes return JSON with unescaped newlines inside quoted strings, which
    breaks strict JSON parsing. This function preserves characters while replacing
    in-string \n/\r with escaped sequences.
    """

    out: list[str] = []
    in_string = False
    escape = False

    for ch in s:
        if escape:
            out.append(ch)
            escape = False
            continue

        if ch == "\\":
            out.append(ch)
            escape = True
            continue

        if ch == '"':
            out.append(ch)
            in_string = not in_string
            continue

        if in_string and ch == "\n":
            out.append("\\n")
            continue
        if in_string and ch == "\r":
            out.append("\\r")
            continue

        out.append(ch)

    return "".join(out)


def parse_jsonish(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort parse of a JSON object from messy LLM output.

    Supports:
      - strict JSON
      - fenced code blocks (```json ...```)
      - extra trailing text after a JSON object
      - unescaped newlines inside JSON strings

    Returns a dict or None.
    """

    s = (text or "").strip()
    if not s:
        return None

    def _try_parse(candidate: str) -> Optional[Dict[str, Any]]:
        c = (candidate or "").strip()
        if not c:
            return None
        c = _escape_newlines_in_json_strings(c)

        # Allow trailing text after JSON by decoding a prefix.
        try:
            decoder = json.JSONDecoder()
            obj, _idx = decoder.raw_decode(c)
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass

        try:
            v = json.loads(c)
            return v if isinstance(v, dict) else None
        except Exception:
            return None

    v0 = _try_parse(s)
    if isinstance(v0, dict):
        return v0

    # ```json ... ``` or ``` ... ```
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", s, flags=re.IGNORECASE)
    if m:
        v1 = _try_parse(m.group(1))
        return v1 if isinstance(v1, dict) else None

    # Try to grab the first {...} block.
    m2 = re.search(r"(\{[\s\S]*\})", s)
    if m2:
        v2 = _try_parse(m2.group(1))
        return v2 if isinstance(v2, dict) else None

    return None
