from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .portfolio_io import safe_str
from .ontology import display_kind, search_text
from .recommendations import openai_client_available


def _parse_jsonish(text: str) -> Optional[Dict[str, Any]]:
    s = (text or "").strip()
    if not s:
        return None

    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else None
    except Exception:
        pass

    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", s, flags=re.IGNORECASE)
    if m:
        try:
            v = json.loads(m.group(1))
            return v if isinstance(v, dict) else None
        except Exception:
            pass

    i = s.find("{")
    j = s.rfind("}")
    if 0 <= i < j:
        try:
            v = json.loads(s[i : j + 1])
            return v if isinstance(v, dict) else None
        except Exception:
            return None

    return None


def _openai_client() -> Tuple[Optional[Any], Optional[str]]:
    if not openai_client_available():
        return None, "AI is disabled (missing OPENAI_API_KEY)."

    try:
        from openai import OpenAI  # type: ignore

        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        return OpenAI(), model
    except Exception:
        return None, "OpenAI client not available (install requirements.txt)."


def suggest_asset_type_input_values(
    *,
    portfolio: Dict[str, Any],
    asset: Dict[str, Any],
    type_def: Dict[str, Any],
    only_missing_keys: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], str]:
    """Ask the LLM to suggest plausible values for the asset-type inputs.

    Returns: (suggested_values_by_key, raw_reply)

    Notes:
      - This is best-effort and may be wrong; we write suggestions into derived values so users can override via manual.
      - We intentionally keep the prompt narrow and request JSON.
    """

    client, model_or_err = _openai_client()
    if client is None:
        return {}, str(model_or_err or "AI unavailable.")

    model: str = str(model_or_err)

    type_label = safe_str(type_def.get("label")) or safe_str(type_def.get("id"))
    asset_name = safe_str(asset.get("name"))
    asset_kind = display_kind(asset)
    asset_text = search_text(asset)

    inputs = type_def.get("inputs")
    inputs_list: List[Dict[str, Any]] = []
    if isinstance(inputs, list):
        inputs_list = [x for x in inputs if isinstance(x, dict)]

    want: List[Dict[str, Any]] = []
    for inp in inputs_list:
        k = safe_str(inp.get("key")).strip()
        if not k:
            continue
        if only_missing_keys is not None and k not in set(only_missing_keys):
            continue
        want.append(
            {
                "key": k,
                "label": safe_str(inp.get("label")) or k,
                "kind": safe_str(inp.get("kind")) or "string",
                "unit": safe_str(inp.get("unit")),
                "help": safe_str(inp.get("help")),
                "default": inp.get("default"),
            }
        )

    if not want:
        return {}, "No inputs requested."

    prompt = (
        "You are helping a user fill in numeric inputs for a carbon accounting template.\n"
        "Given the asset and the template inputs, suggest plausible values ONLY when they are strongly implied by the provided text.\n"
        "If you cannot infer a value, return null for that key.\n\n"
        f"Asset: {asset_name} ({asset_kind})\n"
        f"Asset text: {asset_text[:1200]}\n\n"
        f"Template: {type_label}\n"
        "Inputs (schema):\n"
        + json.dumps(want, ensure_ascii=False)
        + "\n\n"
        "Return STRICT JSON object with keys:\n"
        "{\n"
        '  "values": {"input_key": number|string|boolean|null, ...},\n'
        '  "notes": "short explanation of what you inferred and what you could not"\n'
        "}\n"
        "No markdown."
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        text = safe_str(resp.choices[0].message.content)
    except Exception as exc:
        return {}, f"AI call failed: {exc}"

    parsed = _parse_jsonish(text) or {}
    values = parsed.get("values") if isinstance(parsed, dict) else None
    out: Dict[str, Any] = values if isinstance(values, dict) else {}
    return out, text


def suggest_asset_type_id(
    *,
    portfolio: Dict[str, Any],
    asset: Dict[str, Any],
    templates: List[Dict[str, Any]],
) -> Tuple[Optional[str], str]:
    """Suggest which asset-type template id to apply for the given asset.

    Args:
      templates: list of {id,label,description} dicts (typically from list_asset_type_summaries()).

    Returns: (asset_type_id_or_none, raw_reply)
    """

    client, model_or_err = _openai_client()
    if client is None:
        return None, str(model_or_err or "AI unavailable.")

    model: str = str(model_or_err)

    asset_name = safe_str(asset.get("name"))
    asset_kind = display_kind(asset)
    asset_text = search_text(asset)

    # Keep the options compact; we generally have a small in-repo library.
    options: List[Dict[str, Any]] = []
    for t in templates:
        if not isinstance(t, dict):
            continue
        tid = safe_str(t.get("id")).strip()
        if not tid:
            continue
        options.append(
            {
                "id": tid,
                "label": safe_str(t.get("label")) or tid,
                "description": safe_str(t.get("description")),
            }
        )

    prompt = (
        "You are helping classify an asset into one of a small set of carbon-accounting templates.\n"
        "Choose the single best template id from the provided list.\n"
        "If none match, return status=\"not_found\" and asset_type_id=null, and say what template should be added.\n\n"
        f"Asset: {asset_name} ({asset_kind})\n"
        f"Asset text: {asset_text[:1400]}\n\n"
        "Templates:\n"
        + json.dumps(options, ensure_ascii=False)
        + "\n\n"
        "Return STRICT JSON ONLY (no markdown):\n"
        "{\n"
        '  \"status\": \"ok\"|\"not_found\",\n'
        '  \"asset_type_id\": string|null,\n'
        '  \"notes\": string\n'
        "}\n"
        "Rules:\n"
        "- If status=ok, asset_type_id MUST equal one of the template ids exactly.\n"
        "- If unsure, choose not_found.\n"
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        text = safe_str(resp.choices[0].message.content)
    except Exception as exc:
        return None, f"AI call failed: {exc}"

    parsed = _parse_jsonish(text) or {}
    status = safe_str(parsed.get("status")).strip().lower()
    suggested = safe_str(parsed.get("asset_type_id")).strip() if isinstance(parsed, dict) else ""

    if status != "ok":
        return None, text

    allowed = {safe_str(x.get("id")).strip() for x in options if safe_str(x.get("id")).strip()}
    if suggested not in allowed:
        return None, text

    return suggested, text
