from __future__ import annotations

import json
import os
import re
import uuid
import difflib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .asset_types import list_asset_type_summaries, load_asset_type, apply_asset_type_template
from .portfolio_edit import add_child_asset, can_add_child, find_asset_ref, remove_asset_snapshot
from .portfolio_index import AssetNode, index_portfolio
from .portfolio_io import as_list, ensure_asset_data_fields, ensure_asset_ids, ensure_asset_ontology_fields, safe_str
from .recommendations import openai_client_available
from .ontology import hierarchy_category


def _escape_newlines_in_json_strings(s: str) -> str:
    """Escape raw newlines inside JSON strings.

    LLMs sometimes emit literal newlines within quoted strings, which is invalid JSON.
    This best-effort sanitizer makes such output parseable without changing semantics.
    """

    out: List[str] = []
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


def _parse_jsonish(text: str) -> Optional[Dict[str, Any]]:
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


def _asset_nodes_summary(nodes: List[AssetNode], *, max_items: int = 120) -> str:
    lines: List[str] = []
    for n in nodes[:max_items]:
        if not n.node_id:
            continue
        lines.append(f"- {n.node_id}: {n.path} (kind={n.kind})")
    if len(nodes) > max_items:
        lines.append(f"- ... ({len(nodes) - max_items} more)")
    return "\n".join(lines)


def _asset_types_summary(*, max_items: int = 120) -> str:
    items = list_asset_type_summaries()
    lines: List[str] = []
    for s in items[:max_items]:
        desc = (s.description or "").strip()
        if desc:
            lines.append(f"- {s.id}: {s.label} — {desc}")
        else:
            lines.append(f"- {s.id}: {s.label}")
    if len(items) > max_items:
        lines.append(f"- ... ({len(items) - max_items} more)")
    return "\n".join(lines)


def llm_draft_intake_ops(
    *,
    portfolio: Dict[str, Any],
    nodes: List[AssetNode],
    selected_node: AssetNode | None,
    freeform_text: str,
) -> Dict[str, Any]:
    """Convert arbitrary user text into a draft list of edit ops + follow-up questions."""

    text = safe_str(freeform_text)
    if not text.strip():
        return {
            "status": "empty",
            "ops": [],
            "open_questions": ["Paste some text (listing, notes, PDF copy/paste) and try again."],
            "assumptions": [],
            "notes": "",
        }

    heuristic = heuristic_intake_ops(
        portfolio=portfolio,
        nodes=nodes,
        selected_node=selected_node,
        freeform_text=text,
    )

    if not openai_client_available():
        if heuristic.get("ops"):
            return heuristic
        return {
            "status": "disabled",
            "ops": [],
            "open_questions": [
                "AI is disabled (set OPENAI_API_KEY). What should I add first: a building/site/land, or equipment?",
                "If you paste the building name and a room list, I can structure it without AI.",
            ],
            "assumptions": [],
            "notes": "",
        }

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return {
            "status": "disabled",
            "ops": [],
            "open_questions": [
                "OpenAI client library not installed. Install requirements.txt and retry.",
            ],
            "assumptions": [],
            "notes": "",
        }

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI()

    portfolio_name = safe_str(portfolio.get("portfolio_name") or "Portfolio")
    selected = {
        "id": safe_str(selected_node.node_id) if selected_node else "",
        "path": safe_str(selected_node.path) if selected_node else "",
        "kind": safe_str(selected_node.kind) if selected_node else "",
    }

    # Keep prompt size bounded.
    text_trimmed = text.strip()
    if len(text_trimmed) > 8000:
        text_trimmed = text_trimmed[:8000] + "\n[TRUNCATED]"

    system = (
        "You are a data-entry assistant for a portfolio decarbonisation tool. "
        "Your job is to turn messy input (rambling notes, adverts, scraped PDF text) into SAFE edit operations "
        "that build or patch a JSON portfolio of assets. "
        "Return STRICT JSON only (no prose, no markdown). "
        "If you are unsure about something, DO NOT guess: add an open_questions item instead. "
        "Important: if the text explicitly mentions counts/areas (e.g. '3 buildings', '3.04 acres'), you MUST create ops for that structure instead of returning questions_only."  # noqa: E501
    )

    schema = {
        "status": "ok | questions_only",
        "notes": "short explanation",
        "assumptions": ["..."],
        "open_questions": ["..."],
        "ops": [
            {
                "op": "add_asset",
                "ref": "tmp1",
                "parent_id": "<existing asset _id or '' for root>",
                "parent_ref": "<another op.ref to attach under> (optional)",
                "apply_template_id": "<asset_type_id from allowed list or ''>",
                "asset": {
                    "name": "...",
                    "core_type": "place|energy_system|asset|... (optional)",
                    "subtype": "... (optional)",
                    "current_role": "producer|consumer|converter|... (optional)",
                    "location": "... (optional)",
                    "description": "... (optional)",
                    "occupied": True,
                    "attributes": {"key": "value"},
                    "manual_fields": {"data_field_key": "value"},
                    "assets": ["(optional nested child assets in same shape)"]
                },
            },
            {"op": "apply_template", "asset_id": "...", "asset_ref": "tmp1", "asset_type_id": "..."},
            {"op": "update_asset", "asset_id": "...", "asset_ref": "tmp1", "set": {"name": "...", "attributes": {}}},
        ],
    }

    user = (
        f"Portfolio name: {portfolio_name}\n"
        f"Selected asset: {selected}\n\n"
        "Existing assets (id: path):\n"
        f"{_asset_nodes_summary(nodes)}\n\n"
        "Allowed asset templates (asset_type_id):\n"
        f"{_asset_types_summary()}\n\n"
        "Rules:\n"
        "- Use only asset_type_id values from the allowed list. If none fit, leave apply_template_id empty and ask a question.\n"
        "- For rooms/halls/kitchens/toilets/corridors/lobbies etc: use the generic template 'place_room' when available.\n"
        "- Prefer attaching new things under the most specific existing parent you can (use parent_id). If not sure, attach at root.\n"
        "- Only set manual_fields when the text gives a number/value; otherwise ask a question.\n\n"
        "Output schema (example shape):\n"
        + json.dumps(schema, ensure_ascii=False)
        + "\n\n"
        "Freeform input:\n"
        f"{text_trimmed}\n"
    )

    # Ask for strict JSON when the client/model supports it.
    create_kwargs: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }

    # Newer OpenAI models support response_format={"type":"json_object"} for guaranteed JSON.
    # Fall back silently if the installed client/model doesn't support it.
    try:
        create_kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(**create_kwargs)
    except Exception:
        create_kwargs.pop("response_format", None)
        resp = client.chat.completions.create(**create_kwargs)

    content = (resp.choices[0].message.content or "").strip()
    parsed = _parse_jsonish(content)
    if not isinstance(parsed, dict):
        # One-shot repair: ask the model to re-emit as strict JSON.
        try:
            repair_kwargs: Dict[str, Any] = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Return STRICT JSON only. Repair the user-provided content into a valid JSON object matching the expected schema. Do not add prose.",
                    },
                    {
                        "role": "user",
                        "content": "Repair this into strict JSON (object):\n\n" + content,
                    },
                ],
                "temperature": 0.0,
            }
            try:
                repair_kwargs["response_format"] = {"type": "json_object"}
                resp2 = client.chat.completions.create(**repair_kwargs)
            except Exception:
                repair_kwargs.pop("response_format", None)
                resp2 = client.chat.completions.create(**repair_kwargs)

            content2 = (resp2.choices[0].message.content or "").strip()
            parsed2 = _parse_jsonish(content2)
            if isinstance(parsed2, dict):
                parsed = parsed2
        except Exception:
            parsed = None

    if not isinstance(parsed, dict):
        return {
            "status": "parse_error",
            "ops": [],
            "open_questions": ["I couldn’t parse the AI response as JSON. Try again."],
            "assumptions": [],
            "notes": content[:500],
        }

    # Light normalization.
    status = safe_str(parsed.get("status") or "ok").strip() or "ok"
    out = {
        "status": status,
        "notes": safe_str(parsed.get("notes") or "").strip(),
        "assumptions": [safe_str(x).strip() for x in as_list(parsed.get("assumptions")) if safe_str(x).strip()],
        "open_questions": [safe_str(x).strip() for x in as_list(parsed.get("open_questions")) if safe_str(x).strip()],
        "ops": [x for x in as_list(parsed.get("ops")) if isinstance(x, dict)],
    }

    def _has_building_add_ops(ops0: List[Dict[str, Any]]) -> bool:
        for op0 in ops0:
            if safe_str(op0.get("op")).strip().lower() != "add_asset":
                continue
            tmpl = safe_str(op0.get("apply_template_id") or "").strip()
            asset0 = op0.get("asset") if isinstance(op0.get("asset"), dict) else {}
            subtype = safe_str(asset0.get("subtype") or "").strip().lower()
            if tmpl == "place_building" or subtype == "building":
                return True
        return False

    def _find_site_op(ops0: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for op0 in ops0:
            if safe_str(op0.get("op")).strip().lower() != "add_asset":
                continue
            tmpl = safe_str(op0.get("apply_template_id") or "").strip()
            asset0 = op0.get("asset") if isinstance(op0.get("asset"), dict) else {}
            subtype = safe_str(asset0.get("subtype") or "").strip().lower()
            if tmpl == "place_site" or subtype == "site":
                return op0
        return None

    def _unique_ref(existing_ops: List[Dict[str, Any]], *, base: str) -> str:
        taken = {safe_str(o.get("ref")).strip() for o in existing_ops if isinstance(o, dict)}
        if base not in taken:
            return base
        i = 2
        while f"{base}_{i}" in taken:
            i += 1
        return f"{base}_{i}"

    # If the LLM drafted a site but missed a clearly-stated building count, splice in heuristic buildings.
    try:
        h_ops = heuristic.get("ops") if isinstance(heuristic, dict) else None
        h_ops_list: List[Dict[str, Any]] = [x for x in as_list(h_ops) if isinstance(x, dict)]
        if out.get("ops") and h_ops_list:
            llm_ops: List[Dict[str, Any]] = out.get("ops")  # type: ignore[assignment]
            if (not _has_building_add_ops(llm_ops)) and _has_building_add_ops(h_ops_list):
                site_op = _find_site_op(llm_ops)
                if isinstance(site_op, dict):
                    site_ref = safe_str(site_op.get("ref") or "").strip()
                    if not site_ref:
                        site_ref = _unique_ref(llm_ops, base="tmp_site")
                        site_op["ref"] = site_ref

                    injected: List[Dict[str, Any]] = []
                    taken_refs = {safe_str(o.get("ref")).strip() for o in llm_ops if isinstance(o, dict)}

                    for hop in h_ops_list:
                        if safe_str(hop.get("op")).strip().lower() != "add_asset":
                            continue
                        tmpl = safe_str(hop.get("apply_template_id") or "").strip()
                        asset0 = hop.get("asset") if isinstance(hop.get("asset"), dict) else {}
                        subtype = safe_str(asset0.get("subtype") or "").strip().lower()
                        if tmpl != "place_building" and subtype != "building":
                            continue

                        hop2 = dict(hop)
                        hop2.pop("parent_id", None)
                        hop2["parent_ref"] = site_ref

                        # Ensure the injected building op has a unique ref (for incremental apply ordering).
                        bref = safe_str(hop2.get("ref") or "").strip() or "tmp_bld"
                        if bref in taken_refs:
                            bref = _unique_ref(llm_ops + injected, base=bref)
                        hop2["ref"] = bref
                        taken_refs.add(bref)

                        injected.append(hop2)

                    if injected:
                        llm_ops.extend(injected)
                        out["notes"] = (safe_str(out.get("notes")) + "\nAdded buildings from heuristic extraction.").strip()
                        out["assumptions"] = as_list(out.get("assumptions")) + [
                            "Text mentioned a building count; added building assets under the drafted site.",
                        ]
    except Exception:
        pass

    # If the model refused to draft despite obvious structure, fall back to heuristics.
    if not out.get("ops") and heuristic.get("ops"):
        heuristic["notes"] = (safe_str(out.get("notes")) + "\n" + safe_str(heuristic.get("notes"))).strip()
        # Keep model questions too (but de-dupe lightly)
        qs = [safe_str(x).strip() for x in as_list(out.get("open_questions")) if safe_str(x).strip()]
        for q in as_list(heuristic.get("open_questions")):
            qq = safe_str(q).strip()
            if qq and qq not in qs:
                qs.append(qq)
        heuristic["open_questions"] = qs
        return heuristic

    return out


def _word_number_to_int(tok: str) -> Optional[int]:
    t = safe_str(tok).strip().lower()
    if not t:
        return None
    m = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }.get(t)
    return int(m) if m is not None else None


def _extract_float(text: str, pattern: str) -> Optional[float]:
    m = re.search(pattern, text, flags=re.IGNORECASE)
    if not m:
        return None
    s = safe_str(m.group(1)).strip().replace(",", "")
    try:
        return float(s)
    except Exception:
        return None


def _extract_int(text: str, pattern: str) -> Optional[int]:
    m = re.search(pattern, text, flags=re.IGNORECASE)
    if not m:
        return None
    g = safe_str(m.group(1)).strip()
    try:
        return int(g)
    except Exception:
        w = _word_number_to_int(g)
        return int(w) if w is not None else None


def heuristic_intake_ops(
    *,
    portfolio: Dict[str, Any],
    nodes: List[AssetNode],
    selected_node: AssetNode | None,
    freeform_text: str,
) -> Dict[str, Any]:
    """Heuristic intake that extracts obvious place structure from text.

    Designed as a fallback when the LLM is disabled or returns questions_only.
    """

    text = safe_str(freeform_text)
    t = text.strip()
    if not t:
        return {"status": "empty", "ops": [], "open_questions": [], "assumptions": [], "notes": ""}

    acres = _extract_float(t, r"\b([0-9]+(?:\.[0-9]+)?)\s*acres?\b")
    mva = _extract_float(t, r"\b([0-9]+(?:\.[0-9]+)?)\s*mva\b")
    buildings_n = (
        _extract_int(t, r"\bcampus\s+of\s+([a-z0-9]+)\s+buildings\b")
        or _extract_int(t, r"\b([0-9]+)\s+buildings\b")
    )
    gia_sqft = _extract_float(t, r"\bGIA\s+of\s+([0-9][0-9,]*)\s*sq\s*ft\b")

    # Location: "situated in X" or "in X" (keep it short)
    loc = ""
    m = re.search(r"\bsituated\s+in\s+([^\n,]+)", t, flags=re.IGNORECASE)
    if m:
        loc = safe_str(m.group(1)).strip()
    if not loc:
        m2 = re.search(r"\bin\s+(bletchley|milton\s+keynes)\b", t, flags=re.IGNORECASE)
        if m2:
            loc = safe_str(m2.group(1)).strip()

    # If we don't have any strong signals, don't fabricate ops.
    if acres is None and buildings_n is None and gia_sqft is None and mva is None:
        return {"status": "no_signals", "ops": [], "open_questions": [], "assumptions": [], "notes": ""}

    parent_id = safe_str(selected_node.node_id) if selected_node else ""

    ops: List[Dict[str, Any]] = []
    assumptions: List[str] = []
    questions: List[str] = []

    # If acreage is known, create an explicit Land container so buildings are "on" land.
    land_ref = ""
    if acres is not None:
        # 1 acre = 0.404685642 ha
        area_ha = float(acres) * 0.404685642
        land_ref = "tmp_land"
        land_name = "Land"
        if loc:
            land_name = f"{loc} Land"
        elif re.search(r"milton\s+keynes", t, flags=re.IGNORECASE):
            land_name = "Milton Keynes Land"

        ops.append(
            {
                "op": "add_asset",
                "ref": land_ref,
                "parent_id": parent_id,
                "apply_template_id": "land_field",
                "asset": {
                    "name": land_name,
                    "core_type": "place",
                    "subtype": "field",
                    "location": loc,
                    "description": "Imported from listing text.",
                    "attributes": {"area_acres": float(acres)},
                    "manual_fields": {"area_ha": area_ha},
                },
            }
        )
        assumptions.append("Created a Land container because acreage was provided.")
        questions.append(
            "What land type best fits this site (field/garden/woodland/grassland/soil)? I used 'field' as a generic default."
        )

    site_ref = "tmp_site"
    site_name = "Site"
    # Best-effort name from headline.
    if re.search(r"milton\s+keynes", t, flags=re.IGNORECASE):
        site_name = "Milton Keynes Site"
    if loc:
        # Prefer a concise location based name.
        if "milton" in loc.lower() and "keynes" in loc.lower():
            site_name = "Milton Keynes Site"
        elif loc:
            site_name = f"{loc} Site"

    site_attrs: Dict[str, Any] = {}
    if acres is not None:
        site_attrs["area_acres"] = float(acres)
        assumptions.append("Recorded the freehold acreage on the site attributes.")
    if mva is not None:
        site_attrs["incoming_power_mva"] = float(mva)
    if gia_sqft is not None:
        site_attrs["campus_gia_sqft"] = float(gia_sqft)
        assumptions.append("Recorded the stated GIA on the site (not split per building).")

    ops.append(
        {
            "op": "add_asset",
            "ref": site_ref,
            "parent_id": parent_id if not land_ref else "",
            "parent_ref": land_ref,
            "apply_template_id": "place_site",
            "asset": {
                "name": site_name,
                "core_type": "place",
                "subtype": "site",
                "location": loc,
                "description": "Imported from listing text.",
                "attributes": site_attrs,
                "manual_fields": ({"area_acres": float(acres)} if acres is not None else {}),
            },
        }
    )

    if buildings_n is None:
        # If the text mentions GIA but not count, still propose a single building.
        if gia_sqft is not None and re.search(r"\bbuilding\b", t, flags=re.IGNORECASE):
            buildings_n = 1
            assumptions.append("No building count found; drafted a single building.")

    if buildings_n is not None:
        bn = max(1, min(int(buildings_n), 20))
        if bn != int(buildings_n):
            assumptions.append("Capped drafted building count for safety.")
        for i in range(1, bn + 1):
            ops.append(
                {
                    "op": "add_asset",
                    "ref": f"tmp_bld_{i}",
                    "parent_ref": site_ref,
                    "apply_template_id": "place_building",
                    "asset": {
                        "name": f"Building {i}",
                        "core_type": "place",
                        "subtype": "building",
                        "location": loc,
                        "description": "Part of campus (drafted from listing text).",
                    },
                }
            )

        if bn > 1 and gia_sqft is not None:
            questions.append("Do you want to split the 68,353 sq ft GIA across the 3 buildings (and if so, how)?")

    # Add obvious follow-ups.
    if loc:
        questions.append("What should this site/building be called in your portfolio (official name)?")
    questions.append("Are these buildings occupied/operational now, or vacant (and from when)?")
    if mva is not None:
        questions.append("Is the incoming power figure a capacity limit, or a typical peak load?")

    return {
        "status": "ok",
        "notes": "Drafted from obvious structure in the text (heuristic fallback).",
        "ops": ops,
        "open_questions": questions,
        "assumptions": assumptions,
    }


@dataclass
class ApplyOpResult:
    ok: bool
    message: str
    op: Dict[str, Any]


@dataclass
class ApplyOpsSummary:
    results: List[ApplyOpResult]
    ref_to_id: Dict[str, str]
    last_added_asset_id: str = ""

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"true", "yes", "y", "1"}:
            return True
        if s in {"false", "no", "n", "0"}:
            return False
    return None


def _ensure_field_manual(asset: Dict[str, Any], *, key: str, value: Any) -> None:
    fields = asset.get("data_fields")
    if not isinstance(fields, dict):
        fields = {}
        asset["data_fields"] = fields

    entry = fields.get(key)
    if not isinstance(entry, dict):
        entry = {"label": key, "kind": "number" if isinstance(value, (int, float)) else "string"}
        fields[key] = entry

    manual = entry.get("manual")
    if not isinstance(manual, dict):
        manual = {}
        entry["manual"] = manual
    manual["value"] = value

    derived = entry.get("derived")
    if not isinstance(derived, dict):
        derived = {}
        entry["derived"] = derived
    derived.setdefault("value", None)


def _coerce_numberish(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if not s:
            return None
        # Drop common unit suffixes the LLM might include.
        s = re.sub(r"\s*(acres?|ha|hectares?|sq\s*ft|sqft|m2|m²)\b", "", s).strip()
        s = s.replace(",", "")
        try:
            return float(s)
        except Exception:
            return None
    return None


def _apply_template_input_values_from_attributes(
    *,
    asset: Dict[str, Any],
    type_def: Dict[str, Any],
    attrs: Dict[str, Any],
    manual_fields_payload: Optional[Dict[str, Any]],
) -> None:
    """If an op sets attributes that correspond to template inputs, copy into manual data_fields.

    This helps when the LLM writes e.g. attributes.area_acres instead of manual_fields.area_acres.
    """

    inputs = type_def.get("inputs")
    if not isinstance(inputs, list):
        return

    manual_keys = set()
    if isinstance(manual_fields_payload, dict):
        manual_keys = {safe_str(k).strip() for k in manual_fields_payload.keys() if safe_str(k).strip()}

    for item in inputs:
        if not isinstance(item, dict):
            continue
        key = safe_str(item.get("key")).strip()
        if not key:
            continue
        if key in manual_keys:
            continue
        if key not in attrs:
            continue

        v = attrs.get(key)
        kind = safe_str(item.get("kind") or "string").strip().lower()
        if kind == "number":
            nv = _coerce_numberish(v)
            if nv is not None:
                v = nv
        _ensure_field_manual(asset, key=key, value=v)


def _clean_asset_payload(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    asset: Dict[str, Any] = {}
    for k in ("name", "core_type", "subtype", "current_role", "location", "description"):
        v = raw.get(k)
        if v is None:
            continue
        s = safe_str(v).strip()
        if s:
            asset[k] = s

    occ = _coerce_bool(raw.get("occupied"))
    if occ is not None:
        asset["occupied"] = occ

    attrs = raw.get("attributes")
    if isinstance(attrs, dict) and attrs:
        asset["attributes"] = {safe_str(kk): vv for kk, vv in attrs.items() if safe_str(kk).strip()}

    manual_fields = raw.get("manual_fields")
    if isinstance(manual_fields, dict) and manual_fields:
        asset["manual_fields"] = {safe_str(kk): vv for kk, vv in manual_fields.items() if safe_str(kk).strip()}

    children = raw.get("assets")
    if isinstance(children, list) and children:
        asset["assets"] = [c for c in children if isinstance(c, dict)]

    return asset


def _pick_parent_id(
    *,
    portfolio: Dict[str, Any],
    desired_parent_id: str,
    selected_node_id: str,
    child_stub: Dict[str, Any],
) -> str:
    """Pick a parent id that can contain child_stub.

    Tries desired_parent_id, then walks up its ancestors, then tries selected_node_id ancestors, else root.
    """

    try:
        _nodes, by_id = index_portfolio(portfolio)
    except Exception:
        by_id = {}

    def ok_parent(pid: str) -> bool:
        if not pid:
            # root is treated as generic container
            return True
        n = by_id.get(pid)
        if not n or not isinstance(getattr(n, "data", None), dict):
            return False
        try:
            return bool(can_add_child(parent_asset=n.data, child_asset=child_stub))
        except Exception:
            return False

    def walk_up(start_id: str) -> str:
        cur = safe_str(start_id).strip()
        while cur:
            if ok_parent(cur):
                return cur
            n = by_id.get(cur)
            cur = safe_str(n.parent_id).strip() if n else ""
        return ""

    d = safe_str(desired_parent_id).strip()
    picked = walk_up(d) if d else ""
    if picked or (d == "" and ok_parent("")):
        return picked

    s = safe_str(selected_node_id).strip()
    picked2 = walk_up(s) if s else ""
    if picked2:
        return picked2

    return ""


def apply_intake_ops(
    *,
    portfolio: Dict[str, Any],
    nodes: List[AssetNode],
    selected_node: AssetNode | None,
    ops: List[Dict[str, Any]],
    existing_ref_to_id: Optional[Dict[str, str]] = None,
) -> ApplyOpsSummary:
    """Apply a list of intake ops to the portfolio (best-effort).

    This mutates portfolio in-place.
    """

    ref_to_id: Dict[str, str] = dict(existing_ref_to_id or {})
    results: List[ApplyOpResult] = []
    last_added = ""

    selected_node_id = safe_str(selected_node.node_id) if selected_node else ""

    def resolve_asset_id(*, asset_id: str, asset_ref: str) -> str:
        aid = safe_str(asset_id).strip()
        if aid:
            return aid
        ref = safe_str(asset_ref).strip()
        return ref_to_id.get(ref, "")

    def apply_template_to(asset: Dict[str, Any], *, asset_type_id: str) -> Tuple[bool, str]:
        tid = safe_str(asset_type_id).strip()
        if not tid:
            return False, "Missing asset_type_id"
        td = load_asset_type(tid)
        if not isinstance(td, dict):
            return False, f"Unknown template: {tid}"
        try:
            apply_asset_type_template(asset=asset, type_def=td, portfolio=portfolio)
            return True, f"Applied template {tid}"
        except Exception as exc:
            return False, f"Failed to apply template {tid}: {exc}"

    def add_asset_recursive(
        *,
        parent_id: str,
        parent_ref: str,
        ref: str,
        apply_template_id: str,
        raw_asset: Dict[str, Any],
    ) -> Tuple[bool, str]:
        nonlocal last_added

        clean = _clean_asset_payload(raw_asset)
        name = safe_str(clean.get("name")).strip() or "Unnamed"
        new_id = uuid.uuid4().hex

        new_asset: Dict[str, Any] = {
            "_id": new_id,
            "name": name,
        }
        for k in ("core_type", "subtype", "current_role", "location", "description", "occupied"):
            if k in clean:
                new_asset[k] = clean[k]
        if isinstance(clean.get("attributes"), dict):
            new_asset["attributes"] = dict(clean.get("attributes") or {})

        # Apply template first (so data_fields exist and ontology fields are normalized).
        tmpl_id = safe_str(apply_template_id or "").strip()
        td_for_inputs: Optional[Dict[str, Any]] = None
        if tmpl_id:
            _ok, _msg = apply_template_to(new_asset, asset_type_id=tmpl_id)
            td0 = load_asset_type(tmpl_id)
            if isinstance(td0, dict):
                td_for_inputs = td0

        # Apply manual fields (if any)
        manual_fields = clean.get("manual_fields")
        if isinstance(manual_fields, dict):
            for k, v in manual_fields.items():
                kk = safe_str(k).strip()
                if not kk:
                    continue
                _ensure_field_manual(new_asset, key=kk, value=v)

        # If the LLM put input-ish values in attributes, copy them into template inputs.
        attrs_payload = clean.get("attributes")
        if td_for_inputs and isinstance(attrs_payload, dict) and attrs_payload:
            _apply_template_input_values_from_attributes(
                asset=new_asset,
                type_def=td_for_inputs,
                attrs=attrs_payload,
                manual_fields_payload=manual_fields if isinstance(manual_fields, dict) else None,
            )

        # Pick a safe parent.
        desired_parent_id = safe_str(parent_id).strip()
        if not desired_parent_id and safe_str(parent_ref).strip():
            desired_parent_id = ref_to_id.get(safe_str(parent_ref).strip(), "")

        parent_stub = {"core_type": "asset", "subtype": ""}
        try:
            picked_parent_id = _pick_parent_id(
                portfolio=portfolio,
                desired_parent_id=desired_parent_id,
                selected_node_id=selected_node_id,
                child_stub=new_asset,
            )
        except Exception:
            picked_parent_id = desired_parent_id

        ok = False
        if safe_str(picked_parent_id).strip():
            ok = bool(add_child_asset(portfolio, parent_id=picked_parent_id, child_asset=new_asset))
        else:
            roots = portfolio.get("assets")
            if not isinstance(roots, list):
                roots = []
                portfolio["assets"] = roots
            roots.append(new_asset)
            ok = True

        if not ok:
            return False, "Could not insert asset (containment rule?)"

        if safe_str(ref).strip():
            ref_to_id[safe_str(ref).strip()] = new_id

        last_added = new_id

        # Recurse for nested children.
        for child in as_list(clean.get("assets")):
            if not isinstance(child, dict):
                continue
            child_ref = safe_str(child.get("ref")).strip()
            child_tmpl = safe_str(child.get("apply_template_id")).strip() or safe_str(child.get("asset_type_id")).strip()
            _child_ok, _child_msg = add_asset_recursive(
                parent_id=new_id,
                parent_ref="",
                ref=child_ref,
                apply_template_id=child_tmpl,
                raw_asset=child,
            )

        return True, new_id

    for op in ops:
        op_name = safe_str(op.get("op")).strip().lower()

        if op_name == "add_asset":
            ok, msg = add_asset_recursive(
                parent_id=safe_str(op.get("parent_id")),
                parent_ref=safe_str(op.get("parent_ref")),
                ref=safe_str(op.get("ref")),
                apply_template_id=safe_str(op.get("apply_template_id")),
                raw_asset=op.get("asset") if isinstance(op.get("asset"), dict) else {},
            )
            results.append(ApplyOpResult(ok=ok, message=("added" if ok else msg), op=op))
            continue

        if op_name == "apply_template":
            aid = resolve_asset_id(asset_id=safe_str(op.get("asset_id")), asset_ref=safe_str(op.get("asset_ref")))
            if not aid:
                results.append(ApplyOpResult(ok=False, message="Missing asset_id/asset_ref", op=op))
                continue
            ref = find_asset_ref(portfolio, asset_id=aid)
            if not ref:
                results.append(ApplyOpResult(ok=False, message=f"Asset not found: {aid}", op=op))
                continue
            ok, msg = apply_template_to(ref.asset, asset_type_id=safe_str(op.get("asset_type_id")))
            results.append(ApplyOpResult(ok=ok, message=msg, op=op))
            continue

        if op_name == "update_asset":
            aid = resolve_asset_id(asset_id=safe_str(op.get("asset_id")), asset_ref=safe_str(op.get("asset_ref")))
            if not aid:
                results.append(ApplyOpResult(ok=False, message="Missing asset_id/asset_ref", op=op))
                continue
            ref = find_asset_ref(portfolio, asset_id=aid)
            if not ref:
                results.append(ApplyOpResult(ok=False, message=f"Asset not found: {aid}", op=op))
                continue
            patch = op.get("set")
            if not isinstance(patch, dict):
                results.append(ApplyOpResult(ok=False, message="Missing set dict", op=op))
                continue

            # Only allow safe fields.
            for k in ("name", "core_type", "subtype", "current_role", "location", "description"):
                if k in patch and safe_str(patch.get(k)).strip():
                    ref.asset[k] = safe_str(patch.get(k)).strip()

            occ = _coerce_bool(patch.get("occupied"))
            if occ is not None:
                ref.asset["occupied"] = occ

            attrs = patch.get("attributes")
            if isinstance(attrs, dict):
                existing = ref.asset.get("attributes")
                if not isinstance(existing, dict):
                    existing = {}
                    ref.asset["attributes"] = existing
                for kk, vv in attrs.items():
                    kks = safe_str(kk).strip()
                    if not kks:
                        continue
                    existing[kks] = vv

            manual_fields = patch.get("manual_fields")
            if isinstance(manual_fields, dict):
                for kk, vv in manual_fields.items():
                    kks = safe_str(kk).strip()
                    if not kks:
                        continue
                    _ensure_field_manual(ref.asset, key=kks, value=vv)

            # If inputs were provided as attributes, copy them into template manual fields.
            tid = safe_str(ref.asset.get("asset_type_id")).strip()
            if tid and isinstance(attrs, dict) and attrs:
                td0 = load_asset_type(tid)
                if isinstance(td0, dict):
                    _apply_template_input_values_from_attributes(
                        asset=ref.asset,
                        type_def=td0,
                        attrs=attrs,
                        manual_fields_payload=manual_fields if isinstance(manual_fields, dict) else None,
                    )

            results.append(ApplyOpResult(ok=True, message="updated", op=op))
            continue

        results.append(ApplyOpResult(ok=False, message=f"Unknown op: {op_name}", op=op))

    # Normalize portfolio schema.
    try:
        ensure_asset_ids(portfolio, id_key="_id")
        ensure_asset_data_fields(portfolio)
        ensure_asset_ontology_fields(portfolio)
    except Exception:
        pass

    return ApplyOpsSummary(results=results, ref_to_id=ref_to_id, last_added_asset_id=last_added)


def summarize_ops_for_ui(*, ops: List[Dict[str, Any]], nodes: List[AssetNode]) -> List[str]:
    by_id = {n.node_id: n for n in nodes if n.node_id}
    out: List[str] = []

    def _fmt_scalar(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            try:
                f = float(v)
                # Keep it readable (avoid 3.0400000001)
                s = ("%g" % f)
                return s
            except Exception:
                return str(v)
        s = safe_str(v).strip()
        # Avoid huge blobs in the one-line summary.
        if len(s) > 60:
            s = s[:57] + "…"
        return s

    def _collect_params(asset_payload: Dict[str, Any]) -> List[str]:
        params: List[str] = []

        manual_fields = asset_payload.get("manual_fields")
        if isinstance(manual_fields, dict):
            for k, v in manual_fields.items():
                kk = safe_str(k).strip()
                if not kk:
                    continue
                vv = _fmt_scalar(v)
                if vv != "":
                    params.append(f"{kk}={vv}")

        # Include a few common scalar attributes (helpful for quick review).
        attrs = asset_payload.get("attributes")
        if isinstance(attrs, dict):
            preferred = [
                "area_acres",
                "area_ha",
                "incoming_power_mva",
                "campus_gia_sqft",
                "gia_sqft",
                "gia_m2",
            ]
            seen_keys = {p.split("=", 1)[0] for p in params if "=" in p}
            for kk in preferred:
                if kk in seen_keys:
                    continue
                if kk in attrs:
                    vv = _fmt_scalar(attrs.get(kk))
                    if vv != "":
                        params.append(f"{kk}={vv}")

            # If still empty, include up to 3 other scalar attrs.
            if not params:
                n = 0
                for k, v in attrs.items():
                    if n >= 3:
                        break
                    kk = safe_str(k).strip()
                    if not kk:
                        continue
                    if isinstance(v, (str, int, float, bool)):
                        vv = _fmt_scalar(v)
                        if vv != "":
                            params.append(f"{kk}={vv}")
                            n += 1

        return params

    for op in ops:
        name = safe_str(op.get("op")).strip().lower()
        if name == "add_asset":
            parent_id = safe_str(op.get("parent_id")).strip()
            parent_ref = safe_str(op.get("parent_ref")).strip()
            if parent_id and parent_id in by_id:
                parent_label = by_id[parent_id].path
            elif not parent_id and parent_ref:
                parent_label = f"(draft {parent_ref})"
            else:
                parent_label = "(root)" if not parent_id else parent_id
            asset = op.get("asset") if isinstance(op.get("asset"), dict) else {}
            asset_name = safe_str(asset.get("name") or "Unnamed")
            tmpl = safe_str(op.get("apply_template_id")).strip()
            suffix = f" [template: {tmpl}]" if tmpl else ""
            params = _collect_params(asset)
            ptxt = f" ({', '.join(params)})" if params else ""
            child_txt = ""
            kids = asset.get("assets")
            if isinstance(kids, list) and kids:
                names: List[str] = []
                for c in kids:
                    if not isinstance(c, dict):
                        continue
                    nm = safe_str(c.get("name") or "").strip()
                    if nm:
                        names.append(nm)
                    if len(names) >= 4:
                        break
                if names:
                    more = "" if len(kids) <= len(names) else f" +{len(kids) - len(names)} more"
                    child_txt = f" [children: {', '.join(names)}{more}]"
                else:
                    child_txt = f" [children: {len(kids)}]"

            out.append(f"Add: {asset_name} under {parent_label}{suffix}{ptxt}{child_txt}")
            continue
        if name == "apply_template":
            aid = safe_str(op.get("asset_id")).strip() or safe_str(op.get("asset_ref")).strip()
            tmpl = safe_str(op.get("asset_type_id")).strip()
            out.append(f"Apply template: {tmpl} to {aid}")
            continue
        if name == "update_asset":
            aid = safe_str(op.get("asset_id")).strip() or safe_str(op.get("asset_ref")).strip()
            keys = []
            s = op.get("set")
            if isinstance(s, dict):
                keys = [safe_str(k) for k in s.keys()]
            params: List[str] = []
            if isinstance(s, dict):
                if "manual_fields" in s and isinstance(s.get("manual_fields"), dict):
                    params.extend(_collect_params({"manual_fields": s.get("manual_fields")}))
                if "attributes" in s and isinstance(s.get("attributes"), dict):
                    params.extend(_collect_params({"attributes": s.get("attributes")}))
                for k0 in ("name", "location", "subtype", "core_type", "current_role", "occupied"):
                    if k0 in s:
                        vv = _fmt_scalar(s.get(k0))
                        if vv != "":
                            params.append(f"{k0}={vv}")
            ptxt = f" ({', '.join(params)})" if params else ""
            out.append(f"Update: {aid} (set {', '.join(keys)}){ptxt}")
            continue
        out.append(f"{op}")

    return out


def _canonical_name(name: str) -> str:
    s = safe_str(name).strip().lower()
    if not s:
        return ""

    # Common spelling normalizations.
    s = s.replace("centre", "center")
    s = s.replace("&", " and ")

    # Remove punctuation-ish characters but keep spaces.
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _name_similarity(a: str, b: str) -> float:
    a0 = _canonical_name(a)
    b0 = _canonical_name(b)
    if not a0 or not b0:
        return 0.0
    if a0 == b0:
        return 1.0
    return float(difflib.SequenceMatcher(a=a0, b=b0).ratio())


def suggest_duplicate_assets(
    *,
    nodes: List[AssetNode],
    min_score: float = 0.86,
    max_pairs: int = 8,
) -> List[Dict[str, Any]]:
    """Suggest likely-duplicate assets based on soft name matching.

    Returns list of {a_id,b_id,a_path,b_path,score,category}.
    """

    # Only consider assets that look mergeable: same hierarchy category.
    candidates: List[AssetNode] = [n for n in nodes if n.node_id and safe_str(n.name).strip()]

    out: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()

    # O(n^2) but nodes are small; cap output.
    for i in range(len(candidates)):
        ni = candidates[i]
        ci = hierarchy_category(ni.data)
        for j in range(i + 1, len(candidates)):
            nj = candidates[j]
            if ni.node_id == nj.node_id:
                continue
            cj = hierarchy_category(nj.data)
            if ci != cj:
                continue

            key = tuple(sorted((ni.node_id, nj.node_id)))
            if key in seen:
                continue
            seen.add(key)

            score = _name_similarity(ni.name, nj.name)
            if score < float(min_score):
                continue

            out.append(
                {
                    "a_id": ni.node_id,
                    "b_id": nj.node_id,
                    "a_path": ni.path,
                    "b_path": nj.path,
                    "score": score,
                    "category": ci,
                }
            )

    out.sort(key=lambda d: float(d.get("score") or 0.0), reverse=True)
    return out[: max(0, int(max_pairs))]


def _asset_info_score(asset: Dict[str, Any]) -> int:
    """Heuristic: higher score => better merge target (keep this asset)."""

    score = 0
    if safe_str(asset.get("asset_type_id")).strip():
        score += 3
    if isinstance(asset.get("attributes"), dict) and asset.get("attributes"):
        score += 2
    if isinstance(asset.get("data_fields"), dict) and asset.get("data_fields"):
        score += 2
    score += min(5, len(as_list(asset.get("assets"))))
    if safe_str(asset.get("description")).strip():
        score += 1
    if safe_str(asset.get("location")).strip():
        score += 1
    return int(score)


def suggest_merge_direction(*, a: Dict[str, Any], b: Dict[str, Any]) -> Tuple[str, str]:
    """Return (source, target) as asset ids (when available) based on heuristics."""

    a_id = safe_str(a.get("_id")).strip()
    b_id = safe_str(b.get("_id")).strip()
    if not a_id or not b_id:
        return "", ""
    sa = _asset_info_score(a)
    sb = _asset_info_score(b)
    # Prefer keeping the more "complete" asset as the target.
    if sb > sa:
        return a_id, b_id
    return b_id, a_id


def merge_assets(*, portfolio: Dict[str, Any], source_id: str, target_id: str) -> Tuple[bool, str]:
    """Merge source asset into target asset (best-effort, safe).

    - Moves children if containment allows.
    - Copies missing scalar fields from source into target.
    - Merges attributes/data_fields conservatively (target wins on conflicts).
    - Removes source from portfolio.
    """

    sid = safe_str(source_id).strip()
    tid = safe_str(target_id).strip()
    if not sid or not tid or sid == tid:
        return False, "Invalid merge ids"

    src_ref = find_asset_ref(portfolio, asset_id=sid)
    tgt_ref = find_asset_ref(portfolio, asset_id=tid)
    if not src_ref or not tgt_ref:
        return False, "Asset not found"

    src = src_ref.asset
    tgt = tgt_ref.asset

    # Prevent merging a node into its own descendant/ancestor.
    def contains(parent: Dict[str, Any], child_id: str) -> bool:
        for ch in as_list(parent.get("assets")):
            if not isinstance(ch, dict):
                continue
            if safe_str(ch.get("_id")).strip() == child_id:
                return True
            if contains(ch, child_id):
                return True
        return False

    if contains(src, tid) or contains(tgt, sid):
        return False, "Refusing to merge ancestor/descendant assets"

    if hierarchy_category(src) != hierarchy_category(tgt):
        return False, "Assets are different categories; merge would be unsafe"

    # Check that target can contain all source children.
    src_children = [c for c in as_list(src.get("assets")) if isinstance(c, dict)]
    for ch in src_children:
        if not can_add_child(parent_asset=tgt, child_asset=ch):
            return False, "Target cannot contain all source children (containment rule)"

    # Copy missing scalar fields.
    for k in ("name", "core_type", "subtype", "current_role", "location", "description", "occupied"):
        if k not in tgt or (isinstance(tgt.get(k), str) and not safe_str(tgt.get(k)).strip()):
            if k in src:
                tgt[k] = src.get(k)

    # Merge attributes.
    src_attrs = src.get("attributes")
    tgt_attrs = tgt.get("attributes")
    if isinstance(src_attrs, dict) and src_attrs:
        if not isinstance(tgt_attrs, dict):
            tgt_attrs = {}
            tgt["attributes"] = tgt_attrs
        for kk, vv in src_attrs.items():
            kks = safe_str(kk).strip()
            if not kks:
                continue
            if kks not in tgt_attrs or tgt_attrs.get(kks) in (None, ""):
                tgt_attrs[kks] = vv

    # Merge data_fields conservatively.
    src_fields = src.get("data_fields")
    tgt_fields = tgt.get("data_fields")
    if isinstance(src_fields, dict) and src_fields:
        if not isinstance(tgt_fields, dict):
            tgt_fields = {}
            tgt["data_fields"] = tgt_fields
        for fk, fentry in src_fields.items():
            fks = safe_str(fk).strip()
            if not fks or not isinstance(fentry, dict):
                continue
            if fks not in tgt_fields:
                tgt_fields[fks] = fentry
                continue
            te = tgt_fields.get(fks)
            if not isinstance(te, dict):
                continue
            # Prefer target label/kind/unit; copy missing.
            for kk in ("label", "kind", "unit", "question"):
                if kk not in te and kk in fentry:
                    te[kk] = fentry.get(kk)

            # Merge manual/derived if target is empty.
            for bucket in ("manual", "derived"):
                sb = fentry.get(bucket)
                tb = te.get(bucket)
                if not isinstance(sb, dict):
                    continue
                if not isinstance(tb, dict):
                    tb = {}
                    te[bucket] = tb
                if tb.get("value") is None and sb.get("value") is not None:
                    tb["value"] = sb.get("value")

    # Move children.
    if src_children:
        tgt_children = tgt.get("assets")
        if not isinstance(tgt_children, list):
            tgt_children = []
            tgt["assets"] = tgt_children
        tgt_children.extend(src_children)

    # Remove source.
    removed = remove_asset_snapshot(portfolio, asset_id=sid)
    if not removed:
        return False, "Failed to remove source asset"

    # Ensure schema is consistent.
    try:
        ensure_asset_ids(portfolio, id_key="_id")
        ensure_asset_data_fields(portfolio)
        ensure_asset_ontology_fields(portfolio)
    except Exception:
        pass

    return True, "Merged"
