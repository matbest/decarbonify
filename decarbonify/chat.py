from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .portfolio_index import AssetNode
from .portfolio_io import as_list, safe_str
from .ontology import display_kind
from .portfolio_edit import add_child_asset, can_add_child, explain_disallowed_child_assets
from .portfolio_io import ensure_asset_ids


# Backward-compat shim: some deployments may not yet have ensure_asset_data_fields.
try:
    from .portfolio_io import ensure_asset_data_fields  # type: ignore
except Exception:  # pragma: no cover
    from typing import Any, Dict

    def ensure_asset_data_fields(portfolio: Dict[str, Any]) -> None:  # type: ignore
        assets = portfolio.get("assets")
        if not isinstance(assets, list):
            return
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            fields = asset.get("data_fields")
            if not isinstance(fields, dict):
                fields = {}
                asset["data_fields"] = fields
            key = "emissions_tco2e_per_year"
            entry = fields.get(key)
            if not isinstance(entry, dict):
                entry = {"label": "Emissions", "kind": "number", "unit": "tCO2e/year"}
                fields[key] = entry
            derived = entry.get("derived")
            if not isinstance(derived, dict):
                derived = {}
                entry["derived"] = derived
            derived.setdefault("value", None)
            manual = entry.get("manual")
            if not isinstance(manual, dict):
                manual = {}
                entry["manual"] = manual
            manual.setdefault("value", None)


# Backward-compat shim: some deployments may not yet have ensure_asset_ontology_fields.
try:
    from .portfolio_io import ensure_asset_ontology_fields  # type: ignore
except Exception:  # pragma: no cover
    def ensure_asset_ontology_fields(portfolio: Dict[str, Any]) -> None:  # type: ignore
        return

from .recommendations import openai_client_available


def portfolio_compact_summary(nodes: List[AssetNode], max_items: int = 80) -> str:
    lines: List[str] = []
    for n in nodes[:max_items]:
        lines.append(f"- {n.path} (kind={n.kind})")
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
        kind = display_kind(a)
        here = name if not path else f"{path} / {name}"
        lines.append(f"- {here} (kind={kind})")
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

    ai_enabled = openai_client_available()
    client = None
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    if ai_enabled:
        try:
            from openai import OpenAI  # type: ignore

            client = OpenAI()
        except Exception:
            # Gracefully fall back to offline heuristics.
            ai_enabled = False

    selected_asset = selected_node.data
    selected_id = safe_str(selected_asset.get("_id"))
    selected_path = safe_str(selected_node.path)
    selected_kind = safe_str(selected_node.kind)
    subtree_summary = _subtree_compact_summary(selected_asset)

    selected_asset_name = safe_str(selected_asset.get("name")).strip()

    def _add_under_parent_or_root(*, parent_id: str, asset_obj: Dict[str, Any]) -> bool:
        """Add an asset under parent_id, or at the portfolio root when parent_id is blank."""

        pid = safe_str(parent_id).strip()
        if pid:
            try:
                return bool(add_child_asset(portfolio, parent_id=pid, child_asset=asset_obj))
            except Exception:
                return False

        roots = portfolio.get("assets")
        if not isinstance(roots, list):
            roots = []
            portfolio["assets"] = roots
        roots.append(asset_obj)
        return True

    def _pick_best_parent_for_child(
        *,
        child_asset: Dict[str, Any],
        preferred_parent_id: str,
    ) -> Tuple[str, str]:
        """Return (parent_id, parent_path) where child_asset should be inserted.

        Strategy:
          1) Use preferred_parent_id if allowed.
          2) Walk up ancestors from the selected node and pick the first allowed.
          3) Fall back to the portfolio root.
        """

        try:
            from .portfolio_index import index_portfolio

            _nodes, by_id = index_portfolio(portfolio)
        except Exception:
            by_id = {}

        pref_id = safe_str(preferred_parent_id).strip()
        if pref_id:
            pref_node = by_id.get(pref_id)
            if pref_node and isinstance(getattr(pref_node, "data", None), dict):
                try:
                    if can_add_child(parent_asset=pref_node.data, child_asset=child_asset):
                        return pref_id, safe_str(pref_node.path)
                except Exception:
                    pass
            # Back-compat: if the preferred parent is the currently selected node, use it.
            if pref_id == safe_str(selected_id).strip():
                try:
                    if can_add_child(parent_asset=selected_asset, child_asset=child_asset):
                        return pref_id, selected_path
                except Exception:
                    pass

        parent_id = safe_str(selected_node.parent_id).strip()
        while parent_id:
            n = by_id.get(parent_id)
            if not n:
                break
            try:
                if can_add_child(parent_asset=n.data, child_asset=child_asset):
                    return parent_id, safe_str(n.path)
            except Exception:
                pass
            parent_id = safe_str(n.parent_id).strip()

        portfolio_name = safe_str(portfolio.get("portfolio_name") or "Portfolio")
        return "", portfolio_name

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

    def _sanitize_asset_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Coerce/clean optional ontology fields without being destructive."""

        from .ontology import infer_core_type_and_subtype, normalize_core_type, normalize_energy_role

        out: Dict[str, Any] = dict(payload)

        # Normalize a few free-text fields.
        for k in ("subtype", "location", "description"):
            if k in out and out[k] is not None:
                out[k] = safe_str(out.get(k)).strip()

        # core_type
        if "core_type" in out:
            ct = safe_str(out.get("core_type")).strip()
            out["core_type"] = normalize_core_type(ct) if ct else ""
        elif "type" in out and safe_str(out.get("type")).strip():
            # LLMs may still emit legacy 'type'. Migrate it immediately.
            inferred_core, inferred_sub = infer_core_type_and_subtype(legacy_type=safe_str(out.get("type")))
            out["core_type"] = inferred_core
            if not safe_str(out.get("subtype")).strip() and inferred_sub:
                out["subtype"] = inferred_sub
            out.pop("type", None)

        # Always drop legacy type if present.
        out.pop("type", None)

        # asset_type_id (template id)
        if "asset_type_id" in out and out["asset_type_id"] is not None:
            out["asset_type_id"] = safe_str(out.get("asset_type_id")).strip()

        # current_role
        if "current_role" in out:
            out["current_role"] = normalize_energy_role(safe_str(out.get("current_role")))

        # potential_roles used to exist in an earlier ontology draft; remove it.
        out.pop("potential_roles", None)

        # quantity
        qty = out.get("quantity")
        if isinstance(qty, (int, float)):
            pass
        elif isinstance(qty, str):
            s = qty.strip()
            if s:
                try:
                    out["quantity"] = float(s) if ("." in s) else int(s)
                except Exception:
                    # Leave it absent rather than storing an invalid type.
                    out.pop("quantity", None)
            else:
                out.pop("quantity", None)
        elif "quantity" in out:
            out.pop("quantity", None)

        # attributes
        attrs = out.get("attributes")
        if isinstance(attrs, dict):
            pass
        elif "attributes" in out:
            out["attributes"] = {}

        return out

    def _best_effort_template_id(*, name: str, user_text: str, candidates: List[Dict[str, str]]) -> str:
        """Heuristic template picker used when AI is off or LLM omitted asset_type_id."""

        s = f"{name} {user_text}".lower()

        # Common hard-coded intents first.
        if "heat pump" in s:
            if any(x in s for x in ["ground", "gs", "borehole"]):
                return "ground_source_heat_pump"
            return "air_source_heat_pump"
        if "boiler" in s and "gas" in s:
            return "gas_boiler"
        if any(x in s for x in ["solar", "pv", "photovoltaic"]):
            return "solar_pv"
        if any(x in s for x in ["floodlight", "flood light", "floodlights"]):
            return "floodlights_electric"
        if "lighting" in s or "lights" in s:
            return "lighting_electric"
        if any(x in s for x in ["fridge", "refrigerator", "freezer"]):
            return "refrigerator_freezer"
        if "battery" in s:
            return "mains_battery"

        # Place/building templates.
        if any(x in s for x in ["room", "kitchen", "hall", "toilet", "loo", "wc", "bathroom", "garage"]):
            return "place_room"
        if any(x in s for x in ["house", "home", "flat", "apartment", "bungalow", "cottage", "building"]):
            return "place_building"

        # Fallback: keyword match against template ids/labels/descriptions.
        words = [w for w in re.split(r"[^a-z0-9]+", s) if len(w) >= 4]
        if not words:
            return ""

        best_id = ""
        best_score = 0
        for c in candidates:
            tid = (c.get("id") or "").lower()
            blob = f"{c.get('id','')} {c.get('label','')} {c.get('description','')}".lower()
            score = 0
            for w in words:
                if w in blob:
                    score += 2
                if w in tid:
                    score += 1
            if score > best_score:
                best_score = score
                best_id = c.get("id") or ""
        return best_id if best_score >= 3 else ""

    def _apply_template_if_any(*, asset_obj: Dict[str, Any], template_id: str) -> None:
        if not template_id:
            return
        try:
            from .asset_types import apply_asset_type_template, load_asset_type

            td = load_asset_type(template_id)
            if isinstance(td, dict):
                apply_asset_type_template(asset=asset_obj, type_def=td, portfolio=portfolio)
        except Exception:
            return

    def _ensure_heat_pump_semantics(asset_obj: Dict[str, Any]) -> None:
        """Ensure a heat pump behaves as an electricity consumer for UI semantics."""

        atid = safe_str(asset_obj.get("asset_type_id")).strip().lower()
        subtype = safe_str(asset_obj.get("subtype")).strip().lower()

        is_heat_pump = (subtype == "heat_pump") or ("heat_pump" in atid) or ("heat pump" in safe_str(asset_obj.get("name")).lower())
        if not is_heat_pump:
            return

        if not safe_str(asset_obj.get("current_role")).strip():
            asset_obj["current_role"] = "consumer"

        attrs = asset_obj.get("attributes")
        if not isinstance(attrs, dict):
            attrs = {}
            asset_obj["attributes"] = attrs

        et = safe_str(attrs.get("energy_type")).strip().lower().replace("-", "_")
        if not et:
            attrs["energy_type"] = "electricity"

    def _tidy_added_name(*, asset_obj: Dict[str, Any]) -> None:
        """Avoid names like 'Parent / Air-source heat pump' for child assets."""

        name = safe_str(asset_obj.get("name")).strip()
        if not name:
            return

        # If name contains a path separator, keep only the last segment.
        if " / " in name:
            parts = [p.strip() for p in name.split("/") if p.strip()]
            if parts:
                tail = parts[-1].strip()
                # Only apply this cleanup if it looks like the parent leaked into the name.
                if selected_asset_name and selected_asset_name.lower() in name.lower():
                    asset_obj["name"] = tail
                    name = tail

        # If still overly verbose, prefer template label for templated assets.
        atid = safe_str(asset_obj.get("asset_type_id")).strip()
        if atid:
            try:
                from .asset_types import load_asset_type

                td = load_asset_type(atid)
                if isinstance(td, dict):
                    label = safe_str(td.get("label")).strip()
                    if label and (len(name) > 40 or selected_asset_name.lower() in name.lower()):
                        asset_obj["name"] = label
            except Exception:
                pass

    def _infer_core_type_and_subtype(*, name: str, user_text: str) -> Tuple[str, str]:
        s = f"{name} {user_text}".lower()
        if "garage" in s:
            return ("place", "garage")
        if any(
            x in s
            for x in [
                "building",
                "house",
                "home",
                "flat",
                "apartment",
                "bungalow",
                "cottage",
                "clubhouse",
                "club house",
                "hall",
                "centre",
                "warehouse",
            ]
        ):
            return ("place", "building")
        if "room" in s:
            return ("place", "room")
        if any(x in s for x in ["area", "field", "pitch", "court", "grounds", "land"]):
            return ("place", "land")
        if any(x in s for x in ["boiler", "heat pump", "hvac", "chiller", "generator", "battery", "inverter"]):
            return ("energy_system", "")
        if any(x in s for x in ["solar", "pv", "wind"]):
            return ("energy_system", "solar_pv" if "solar" in s or "pv" in s else "")
        if any(x in s for x in ["electricity", "gas", "water", "diesel"]):
            return ("resource", "")
        if any(x in s for x in ["roof", "wall", "window", "floor"]):
            return ("surface", "")
        return ("asset", "")

    def _guess_asset_name(user_text: str) -> str:
        t = (user_text or "").strip()
        if not t:
            return "New asset"

        # Common descriptive add phrasing: "it's got a heat pump"
        m_has = re.search(
            r"\b(?:also\s+)?(?:has|have|got|with|includes|including)\b\s+(?:a|an|the)?\s*(.+)$",
            t,
            flags=re.IGNORECASE,
        )
        if m_has and (m_has.group(1) or "").strip():
            candidate = (m_has.group(1) or "").strip()
            candidate = candidate.strip(" .!?:;\"'“”‘’`")
            if candidate:
                if candidate == candidate.lower():
                    candidate = " ".join(w.capitalize() for w in candidate.split())
                return candidate

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

    template_candidates: List[Dict[str, str]] = []
    try:
        from .asset_types import list_asset_type_summaries

        summaries = list_asset_type_summaries()
        for s in summaries[:80]:
            template_candidates.append(
                {
                    "id": safe_str(getattr(s, "id", "")),
                    "label": safe_str(getattr(s, "label", "")),
                    "description": safe_str(getattr(s, "description", "")),
                }
            )
    except Exception:
        template_candidates = []

    system = (
        "You are an assistant that edits a portfolio asset hierarchy. "
        "You may ONLY propose operations that add new child assets under the currently selected node. "
        "Make best-effort assumptions and ask the user for clarification only if you truly need it. "
        "Prefer setting asset_type_id to one of the provided template IDs when the match is obvious (e.g. heat pump, gas boiler, solar PV, lighting). "
        "Containment rules matter: land/natural features can contain buildings; buildings contain rooms and equipment; buildings and rooms cannot contain land/natural features. "
        "You MAY include optional fields on the new asset when you can infer them: asset_type_id, core_type, subtype, current_role, location, quantity, attributes, description. "
        "You MUST return STRICT JSON ONLY (no prose, no markdown). "
        "Return JSON with schema: "
        "{\"reply\": str, \"ops\": [ {\"op\": \"add_child\", \"asset\": {\"name\": str, \"asset_type_id\": str, \"core_type\": str, \"subtype\": str, ...} } ] }. "
        "If you truly need clarification, return {\"reply\": str, \"ops\": []}."
    )
    user = (
        f"Selected asset (editable scope): {selected_path} (id={selected_id}, kind={selected_kind})\n"
        f"Current subtree (paths):\n{subtree_summary}\n\n"
        f"Available templates (choose asset_type_id from these IDs when relevant):\n"
        + "\n".join([f"- {t.get('id')}: {t.get('label')} — {t.get('description')}" for t in template_candidates if t.get("id")])
        + "\n\n"
        f"User request: {user_message}\n"
    )

    parsed: Dict[str, Any]
    if ai_enabled and client is not None:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )

        content = (resp.choices[0].message.content or "").strip()
        parsed = _parse_jsonish(content) or {"reply": content, "ops": []}
    else:
        # Offline mode: no LLM ops, we'll fall back to heuristic add if it looks like an add request.
        parsed = {"reply": "", "ops": []}

    reply = safe_str(parsed.get("reply"))
    ops = parsed.get("ops")
    if not isinstance(ops, list):
        ops = []

    applied = False
    added_asset_id: Optional[str] = None
    coercions: List[str] = []

    # If we create a building/land in this request, subsequent equipment/components should
    # prefer to attach to that new place rather than the original selected component.
    preferred_container_id = safe_str(selected_id).strip()
    preferred_container_path = safe_str(selected_path).strip()
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
        new_asset = _sanitize_asset_payload(asset_payload)

        # If the model omits core_type/subtype, infer best-effort defaults.
        if not safe_str(new_asset.get("core_type")).strip():
            inferred_core, inferred_sub = _infer_core_type_and_subtype(name=name, user_text=user_message)
            new_asset["core_type"] = inferred_core
            if inferred_sub and not safe_str(new_asset.get("subtype")).strip():
                new_asset["subtype"] = inferred_sub
        new_asset.setdefault("subtype", "")

        # Apply a template if specified or can be inferred.
        tid = safe_str(new_asset.get("asset_type_id")).strip()
        if not tid and template_candidates:
            tid = _best_effort_template_id(name=name, user_text=user_message, candidates=template_candidates)
        if tid:
            _apply_template_if_any(asset_obj=new_asset, template_id=tid)

        _ensure_heat_pump_semantics(new_asset)
        _tidy_added_name(asset_obj=new_asset)

        # Pick a valid insertion parent. If the selection is a component (e.g. a heat pump)
        # and the user is adding a new building/land, we should climb to a valid container
        # or use the portfolio root rather than coercing types.
        parent_id, parent_path = _pick_best_parent_for_child(child_asset=new_asset, preferred_parent_id=preferred_container_id)
        if not parent_id and safe_str(preferred_container_id).strip() and not can_add_child(parent_asset=selected_asset, child_asset=new_asset):
            why = explain_disallowed_child_assets(parent_asset=selected_asset, child_asset=new_asset)
            coercions.append(
                f"Added '{name}' at the portfolio root because '{display_kind(new_asset)}' isn't allowed under '{selected_kind}'."
                + (f" ({why})" if why else "")
            )
        elif parent_id and parent_id != preferred_container_id and not can_add_child(parent_asset=selected_asset, child_asset=new_asset):
            why = explain_disallowed_child_assets(parent_asset=selected_asset, child_asset=new_asset)
            coercions.append(
                f"Added '{name}' under {parent_path} because '{display_kind(new_asset)}' isn't allowed under '{selected_kind}'."
                + (f" ({why})" if why else "")
            )

        new_asset.setdefault("_id", uuid.uuid4().hex)
        ok = _add_under_parent_or_root(parent_id=parent_id, asset_obj=new_asset)
        if ok:
            applied = True
            added_asset_id = safe_str(new_asset.get("_id")) or added_asset_id

            # If this new asset is a place container, prefer it for subsequent adds.
            try:
                from .ontology import hierarchy_category

                cat = hierarchy_category(new_asset)
                if cat in {"land", "building", "place"}:
                    preferred_container_id = safe_str(new_asset.get("_id")).strip() or preferred_container_id
                    preferred_container_path = safe_str(new_asset.get("name")).strip() or preferred_container_path
            except Exception:
                pass

    # Fallback: if the model didn't return ops, but the user seems to be asking to add something,
    # add one best-effort child asset anyway.
    if not applied:
        lower = user_message.lower()
        wants_add = (
            ("add" in lower)
            or bool(re.search(r"\b(create|make|install|put|place|fit|build)\b", lower))
            or (bool(re.search(r"\b(also\s+)?(has|have|got|with|includes|including)\b", lower)) and bool(re.search(r"\b(heat pump|boiler|solar|pv|battery|lighting|lights|floodlight|fridge|refrigerator|freezer|oven)\b", lower)))
        )
        if wants_add:
            guessed_name = _guess_asset_name(user_message)
            inferred_core, inferred_sub = _infer_core_type_and_subtype(name=guessed_name, user_text=user_message)
            fallback_asset: Dict[str, Any] = {
                "_id": uuid.uuid4().hex,
                "name": guessed_name,
                "core_type": inferred_core,
                "subtype": inferred_sub,
            }

            # Apply a best-effort template in offline fallback too.
            if template_candidates:
                tid = _best_effort_template_id(name=guessed_name, user_text=user_message, candidates=template_candidates)
                if tid:
                    _apply_template_if_any(asset_obj=fallback_asset, template_id=tid)

            _ensure_heat_pump_semantics(fallback_asset)
            _tidy_added_name(asset_obj=fallback_asset)

            parent_id, parent_path = _pick_best_parent_for_child(child_asset=fallback_asset, preferred_parent_id=selected_id)
            if not parent_id and not can_add_child(parent_asset=selected_asset, child_asset=fallback_asset):
                why = explain_disallowed_child_assets(parent_asset=selected_asset, child_asset=fallback_asset)
                coercions.append(
                    f"Added '{guessed_name}' at the portfolio root because '{display_kind(fallback_asset)}' isn't allowed under '{selected_kind}'."
                    + (f" ({why})" if why else "")
                )
            elif parent_id and parent_id != selected_id and not can_add_child(parent_asset=selected_asset, child_asset=fallback_asset):
                why = explain_disallowed_child_assets(parent_asset=selected_asset, child_asset=fallback_asset)
                coercions.append(
                    f"Added '{guessed_name}' under {parent_path} because '{display_kind(fallback_asset)}' isn't allowed under '{selected_kind}'."
                    + (f" ({why})" if why else "")
                )

            ok = _add_under_parent_or_root(parent_id=parent_id, asset_obj=fallback_asset)
            if ok:
                applied = True
                added_asset_id = safe_str(fallback_asset.get("_id")) or added_asset_id
                if not reply:
                    reply = f"Added '{guessed_name}' under {parent_path}." if parent_id else f"Added '{guessed_name}' at the portfolio root."
                else:
                    where = (f"under {parent_path}" if parent_id else "at the portfolio root")
                    reply = reply.rstrip() + f"\n\nApplied: added '{guessed_name}' {where}."

                if safe_str(fallback_asset.get("asset_type_id")).strip():
                    reply = reply.rstrip() + f"\nTemplate: {safe_str(fallback_asset.get('asset_type_id')).strip()}"

    if coercions:
        extra = "\n".join(coercions)
        reply = (reply or "OK.").rstrip() + "\n\n" + extra

    if applied:
        ensure_asset_ids(portfolio, id_key="_id")
        ensure_asset_data_fields(portfolio)
        ensure_asset_ontology_fields(portfolio)

    return (reply or ("Added." if applied else "No changes applied."), applied, added_asset_id)
