from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .portfolio_index import AssetNode
from .portfolio_io import as_list, safe_str
from .portfolio_edit import add_child_asset, can_add_child_type, explain_disallowed_child
from .portfolio_io import ensure_asset_data_fields, ensure_asset_ids
from .recommendations import openai_client_available


def portfolio_compact_summary(nodes: List[AssetNode], max_items: int = 80) -> str:
    lines: List[str] = []
    for n in nodes[:max_items]:
        lines.append(f"- {n.path} (type={n.type})")
    if len(nodes) > max_items:
        lines.append(f"- ... ({len(nodes) - max_items} more)")
    return "\n".join(lines)


def llm_chat_answer(portfolio: Dict[str, Any], nodes: List[AssetNode], question: str) -> str:
    if not openai_client_available():
        # Simple offline fallback: highlight obvious hotspots and suggest next steps.
        q = question.lower()
        if "upgrade" in q or "first" in q or "priority" in q:
            return (
                "Start with obvious fossil-fuel systems (gas boilers, oil heating) and high-usage electricity loads (lighting). "
                "Then improve insulation/controls, and consider onsite renewables where suitable. "
                "If you share energy bills or runtime estimates, I can prioritise more accurately."
            )
        return (
            "I can help answer questions about this portfolio, but AI is currently disabled (set OPENAI_API_KEY). "
            "Ask about a specific asset or share energy/usage data for better prioritisation."
        )

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return (
            "AI recommendations are unavailable because the OpenAI client library isn't installed. "
            "Install dependencies from requirements.txt and try again."
        )

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI()

    portfolio_name = safe_str(portfolio.get("portfolio_name"))
    summary = portfolio_compact_summary(nodes)

    system = (
        "You are a helpful assistant for a property portfolio decarbonisation tool. "
        "You must base answers on the provided portfolio summary and user question. "
        "If data is missing, say what is missing and give a best-effort qualitative answer."
    )
    user = (
        f"Portfolio name: {portfolio_name}\n"
        f"Portfolio assets (paths):\n{summary}\n\n"
        f"Question: {question}\n"
    )

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()


def _subtree_compact_summary(asset: Dict[str, Any], *, max_items: int = 50) -> str:
    lines: List[str] = []

    def walk(a: Dict[str, Any], path: str) -> None:
        if len(lines) >= max_items:
            return
        name = safe_str(a.get("name")) or "Unnamed"
        asset_type = safe_str(a.get("type")) or "asset"
        here = name if not path else f"{path} / {name}"
        lines.append(f"- {here} (type={asset_type})")
        for ch in as_list(a.get("assets")):
            if isinstance(ch, dict):
                walk(ch, here)

    walk(asset, "")
    if len(lines) >= max_items:
        lines.append("- ...")
    return "\n".join(lines)


def llm_edit_selected_subtree(
    *,
    portfolio: Dict[str, Any],
    selected_node: AssetNode,
    user_message: str,
) -> Tuple[str, bool, Optional[str]]:
    """Edit the portfolio within the selected subtree.

    Current scope: add child assets under the selected node.
    Returns: (assistant_message, applied_changes, added_asset_id_if_any)
    """

    if not openai_client_available():
        return (
            "Edit mode requires AI (set OPENAI_API_KEY). You can still add assets manually in the JSON/file for now.",
            False,
            None,
        )

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return (
            "AI edit mode is unavailable because the OpenAI client library isn't installed. Install requirements.txt and try again.",
            False,
            None,
        )

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI()

    selected_asset = selected_node.data
    selected_id = safe_str(selected_asset.get("_id"))
    selected_path = safe_str(selected_node.path)
    selected_type = safe_str(selected_asset.get("type"))
    subtree_summary = _subtree_compact_summary(selected_asset)

    def _parse_jsonish(text: str) -> Optional[Dict[str, Any]]:
        """Best-effort JSON extraction.

        The model is instructed to return strict JSON, but in practice it may wrap
        JSON in prose or code fences.
        """

        s = (text or "").strip()
        if not s:
            return None

        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else None
        except Exception:
            pass

        # ```json ... ``` or ``` ... ```
        m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", s, flags=re.IGNORECASE)
        if m:
            try:
                v = json.loads(m.group(1))
                return v if isinstance(v, dict) else None
            except Exception:
                pass

        # Last-resort: try from first '{' to last '}'
        i = s.find("{")
        j = s.rfind("}")
        if 0 <= i < j:
            candidate = s[i : j + 1]
            try:
                v = json.loads(candidate)
                return v if isinstance(v, dict) else None
            except Exception:
                pass
        return None

    def _infer_type(*, name: str, user_text: str) -> str:
        s = f"{name} {user_text}".lower()
        if "building" in s or "clubhouse" in s or "club house" in s or "hall" in s or "centre" in s:
            inferred = "building"
        elif "room" in s:
            inferred = "room"
        elif "area" in s or "field" in s or "pitch" in s or "court" in s or "grounds" in s:
            inferred = "land"
        elif "boiler" in s or "heat pump" in s or "hvac" in s:
            inferred = "energy_system"
        elif "solar" in s or "pv" in s:
            inferred = "energy_generation"
        elif "light" in s or "lighting" in s or "floodlight" in s:
            inferred = "lighting"
        else:
            inferred = "asset"
        return inferred

    def _guess_asset_name(user_text: str) -> str:
        t = (user_text or "").strip()
        if not t:
            return "New asset"

        # Try to extract what's being added: "add X to/under Y"
        m = re.search(r"\badd\b\s+(?:a|an|the)?\s*(.+?)(?:\s+\b(?:to|under|into|in|within|on)\b\s+.+)?$", t, flags=re.IGNORECASE)
        candidate = (m.group(1) if m else t).strip()
        candidate = candidate.strip(" .!?:;\"'“”‘’`")
        if not candidate:
            return "New asset"

        # If user includes quotes, prefer inside quotes.
        mq = re.search(r"\"([^\"]+)\"", t)
        if mq:
            q = mq.group(1).strip()
            if q:
                candidate = q

        # Normalize casing only if user used all-lowercase.
        if candidate and candidate == candidate.lower():
            candidate = " ".join(w.capitalize() for w in candidate.split())
        return candidate

    system = (
        "You are an assistant that edits a portfolio asset hierarchy. "
        "You may ONLY propose operations that add new child assets under the currently selected node. "
        "Make best-effort assumptions and DO NOT ask for the asset type unless absolutely necessary. "
        "If the user says 'X building' or similar, use type='building'. Otherwise choose a reasonable type or omit it (it will default to 'asset'). "
        "Type relationships matter: land/natural features can contain buildings; buildings contain rooms and equipment; buildings and rooms cannot contain land/natural features. "
        "You MUST return STRICT JSON ONLY (no prose, no markdown). "
        "Return JSON with schema: "
        "{\"reply\": str, \"ops\": [ {\"op\": \"add_child\", \"asset\": {\"name\": str, \"type\": str, ...} } ] }. "
        "Examples: "
        "- Add a bowls club building -> {\"reply\":\"OK\",\"ops\":[{\"op\":\"add_child\",\"asset\":{\"name\":\"Bowls Club\",\"type\":\"building\"}}]} "
        "- Add a storage shed -> {\"reply\":\"OK\",\"ops\":[{\"op\":\"add_child\",\"asset\":{\"name\":\"Storage Shed\"}}]} "
        "If you truly need clarification, return {\"reply\": str, \"ops\": []}."
    )
    user = (
        f"Selected asset (editable scope): {selected_path} (id={selected_id}, type={selected_type})\n"
        f"Current subtree (paths):\n{subtree_summary}\n\n"
        f"User request: {user_message}\n"
    )

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )

    content = (resp.choices[0].message.content or "").strip()
    parsed = _parse_jsonish(content)
    if parsed is None:
        # Treat as a plain reply (we'll fallback-add if it looks like an add request).
        parsed = {"reply": content, "ops": []}

    reply = safe_str(parsed.get("reply"))
    ops = parsed.get("ops")
    if not isinstance(ops, list):
        ops = []

    applied = False
    added_asset_id: Optional[str] = None
    coercions: List[str] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        if safe_str(op.get("op")) != "add_child":
            continue
        asset_payload = op.get("asset")
        if not isinstance(asset_payload, dict):
            continue
        name = safe_str(asset_payload.get("name"))
        if not name:
            continue
        new_asset = dict(asset_payload)
        # If the model omits type (or leaves it vague), infer a best-effort default.
        raw_type = safe_str(new_asset.get("type"))
        if not raw_type:
            new_asset["type"] = _infer_type(name=name, user_text=user_message)
        else:
            new_asset["type"] = raw_type

        # Enforce parent→child type relationships. If disallowed, coerce to generic.
        if not can_add_child_type(parent_type=selected_type, child_type=safe_str(new_asset.get("type"))):
            why = explain_disallowed_child(parent_type=selected_type, child_type=safe_str(new_asset.get("type")))
            coercions.append(
                f"Used type='asset' for '{name}' because '{safe_str(new_asset.get('type'))}' isn't allowed under '{selected_type}'."
                + (f" ({why})" if why else "")
            )
            new_asset["type"] = "asset"

        new_asset.setdefault("_id", uuid.uuid4().hex)
        ok = add_child_asset(portfolio, parent_id=selected_id, child_asset=new_asset)
        if ok:
            applied = True
            added_asset_id = safe_str(new_asset.get("_id")) or added_asset_id

    # Fallback: if the model didn't return ops, but the user seems to be asking to add something,
    # add one best-effort child asset anyway.
    if not applied:
        wants_add = "add" in user_message.lower()
        if wants_add:
            guessed_name = _guess_asset_name(user_message)
            fallback_asset: Dict[str, Any] = {
                "_id": uuid.uuid4().hex,
                "name": guessed_name,
                "type": _infer_type(name=guessed_name, user_text=user_message),
            }

            if not can_add_child_type(parent_type=selected_type, child_type=safe_str(fallback_asset.get("type"))):
                why = explain_disallowed_child(parent_type=selected_type, child_type=safe_str(fallback_asset.get("type")))
                coercions.append(
                    f"Used type='asset' for '{guessed_name}' because '{safe_str(fallback_asset.get('type'))}' isn't allowed under '{selected_type}'."
                    + (f" ({why})" if why else "")
                )
                fallback_asset["type"] = "asset"

            ok = add_child_asset(portfolio, parent_id=selected_id, child_asset=fallback_asset)
            if ok:
                applied = True
                added_asset_id = safe_str(fallback_asset.get("_id")) or added_asset_id
                if not reply:
                    reply = f"Added '{guessed_name}' under {selected_path}."
                else:
                    reply = reply.rstrip() + f"\n\nApplied: added '{guessed_name}' under {selected_path}."

    if coercions:
        extra = "\n".join(coercions)
        reply = (reply or "OK.").rstrip() + "\n\n" + extra

    if applied:
        ensure_asset_ids(portfolio, id_key="_id")
        ensure_asset_data_fields(portfolio)

    return (reply or ("Added." if applied else "No changes applied."), applied, added_asset_id)
