from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping

from .emissions import effective_emissions_tco2e_per_year, sum_emissions_produced_tco2e_per_year
from .ontology import hierarchy_category, normalize_core_type, search_text
from .portfolio_io import safe_str


def _compact_data_fields(asset: Dict[str, Any], *, max_fields: int = 40) -> List[Dict[str, Any]]:
    fields = asset.get("data_fields")
    if not isinstance(fields, dict) or not fields:
        return []

    out: List[Dict[str, Any]] = []
    for k, entry in fields.items():
        if not isinstance(entry, dict):
            continue
        key = safe_str(k).strip()
        if not key:
            continue

        manual_v = None
        derived_v = None
        manual = entry.get("manual")
        if isinstance(manual, dict):
            manual_v = manual.get("value")
        derived = entry.get("derived")
        if isinstance(derived, dict):
            derived_v = derived.get("value")
        eff_v = manual_v if manual_v is not None else derived_v

        # Skip empty fields to keep prompt compact.
        if eff_v is None:
            continue

        out.append(
            {
                "key": key,
                "label": safe_str(entry.get("label") or key),
                "kind": safe_str(entry.get("kind") or ""),
                "unit": safe_str(entry.get("unit") or ""),
                "effective_value": eff_v,
                "manual_value": manual_v,
                "derived_value": derived_v,
            }
        )
        if len(out) >= max_fields:
            break

    return out


def recommendation_id(rec: Mapping[str, Any]) -> str:
    """Deterministic id for a recommendation payload.

    Used to key per-asset recommendation status like done/ignored.
    """

    payload = {
        "title": safe_str(rec.get("title")),
        "description": safe_str(rec.get("description")),
        "saving": float(rec.get("estimated_saving_tco2_per_year", 0) or 0),
        "action": safe_str(rec.get("action")),
        "add_asset": rec.get("add_asset"),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def openai_client_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def recommendations_model() -> str:
    """Model used for the recommendations 'second pass'.

    This is intentionally separate from other AI usage so you can swap models
    (or providers later) without affecting chat/emissions estimation.
    """

    return os.environ.get("OPENAI_RECOMMENDATIONS_MODEL") or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def generate_recommendations_bundle(portfolio: Dict[str, Any], asset: Dict[str, Any]) -> Dict[str, Any]:
    """Generate and return the stored per-asset recommendations structure."""

    used_openai = openai_client_available()
    model = recommendations_model() if used_openai else ""
    items = llm_recommendations(portfolio, asset) if used_openai else heuristic_recommendations(asset)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": {
            "provider": "openai" if used_openai else "heuristic",
            "model": model,
        },
        "items": items,
    }


def extract_recommendation_items(asset: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Read recommendations from the new bundle, with legacy fallback."""

    bundle = asset.get("recommendations")
    if isinstance(bundle, dict):
        items = bundle.get("items")
        if isinstance(items, list):
            return [r for r in items if isinstance(r, dict)]

    legacy = asset.get("llm_recommendations")
    if isinstance(legacy, list):
        return [r for r in legacy if isinstance(r, dict)]

    return []


def carbon_signal(asset: Dict[str, Any]) -> str:
    ct = normalize_core_type(safe_str(asset.get("core_type")) or "asset")
    st = safe_str(asset.get("subtype")).strip().lower().replace(" ", "_")
    fuel = safe_str(asset.get("fuel", "")).lower()
    txt = search_text(asset)

    if fuel in {"gas", "diesel", "petrol", "oil", "lpg"}:
        return "emits (combustion fuel)"

    if ct == "energy_system" and ("solar" in st or "pv" in st or "solar" in txt or "pv" in txt or "wind" in txt):
        return "reduces (onsite generation)"

    if ct == "place" and hierarchy_category(asset) == "land" and any(x in (st + " " + txt) for x in ["trees", "woodland", "wetlands", "soil", "grassland"]):
        return "sequesters (natural carbon)"

    if ct in {"asset", "energy_system"} and any(x in txt for x in ["light", "lighting", "equipment", "hvac", "boiler", "heat"]):
        return "consumes (likely electricity/heat)"

    if ct == "place" and st in {"building", "room"}:
        return "consumes (likely electricity/heat)"

    return "unknown"


def heuristic_recommendations(asset: Dict[str, Any]) -> List[Dict[str, Any]]:
    ct = normalize_core_type(safe_str(asset.get("core_type")) or "asset")
    st = safe_str(asset.get("subtype")).strip().lower().replace(" ", "_")
    cat = hierarchy_category(asset)
    fuel = safe_str(asset.get("fuel", "")).lower()
    name = safe_str(asset.get("name", "asset"))
    # Include asset_type_id to improve matching when name/description is sparse.
    type_id = safe_str(asset.get("asset_type_id") or "").strip().lower()
    txt = (search_text(asset) + (f" {type_id}" if type_id else "")).strip()

    recs: List[Dict[str, Any]] = []

    if fuel == "gas" or "boiler" in txt:
        recs.append(
            {
                "title": "Switch gas boiler to heat pump",
                "estimated_saving_tco2_per_year": 2.4,
                "description": "Switching from gas combustion to an efficient heat pump typically cuts operational emissions, especially with greener electricity.",
                "action": "switch",
                "add_asset": {"name": "Heat Pump", "core_type": "energy_system", "subtype": "heat_pump", "fuel": "electric"},
            }
        )
        recs.append(
            {
                "title": "Improve building/pipework insulation",
                "estimated_saving_tco2_per_year": 0.6,
                "description": "Reducing heat loss lowers heat demand regardless of heating technology.",
                "action": "other",
            }
        )

    if any(x in txt for x in ["light", "lighting", "floodlight", "lamp", "led"]):
        recs.append(
            {
                "title": "Upgrade to LED + controls",
                "estimated_saving_tco2_per_year": 0.3,
                "description": "LEDs and occupancy/daylight controls reduce electricity consumption while maintaining lighting levels.",
                "action": "other",
            }
        )

    if ct == "place" and cat == "land":
        recs.append(
            {
                "title": "Add trees / biodiversity planting",
                "estimated_saving_tco2_per_year": 0.5,
                "description": "Tree and hedgerow planting, soil improvements, and reduced mowing can increase sequestration over time.",
                "action": "add",
                "add_asset": {"name": "Trees / planting", "core_type": "place", "subtype": "trees", "feature": "trees"},
            }
        )

    if ct == "place" and st in {"building", "room"}:
        recs.append(
            {
                "title": "Add smart heating controls",
                "estimated_saving_tco2_per_year": 0.4,
                "description": "Better schedules, zoning, and setpoints often reduce wasted heating and improve comfort.",
                "action": "other",
            }
        )

    if (ct == "energy_system" and ("solar" in st or "pv" in st)) or "solar" in txt or "pv" in txt:
        recs.append(
            {
                "title": "Verify inverter performance + monitoring",
                "estimated_saving_tco2_per_year": 0.1,
                "description": "Monitoring helps catch faults early and ensures the system delivers expected generation.",
                "action": "other",
            }
        )

    return recs[:5]


def llm_recommendations(portfolio: Dict[str, Any], asset: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not openai_client_available():
        return heuristic_recommendations(asset)

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return heuristic_recommendations(asset)

    model = recommendations_model()
    client = OpenAI()

    portfolio_name = safe_str(portfolio.get("portfolio_name"))
    asset_json = json.dumps(asset, ensure_ascii=False)
    eff_emissions = effective_emissions_tco2e_per_year(asset)
    eff_text = "null" if eff_emissions is None else f"{float(eff_emissions):.4f}"

    # Provide context for container assets (site/building/room) where eff emissions may be 0/None.
    subtree_total_tco2e = 0.0
    subtree_contributing = 0
    subtree_visited = 0
    subtree_overrides_used = 0
    try:
        subtree_total_tco2e, subtree_contributing, subtree_visited, subtree_overrides_used = sum_emissions_produced_tco2e_per_year(asset)
    except Exception:
        pass
    subtree_text = (
        "null"
        if subtree_contributing <= 0
        else f"{float(subtree_total_tco2e):.4f} (assets_with_values={subtree_contributing}/{subtree_visited}, overrides_used={subtree_overrides_used})"
    )

    # Compact data_fields summary so the model can reason over numeric inputs/outputs.
    data_fields = _compact_data_fields(asset)

    # Best-effort template metadata.
    tmpl = {
        "asset_type_id": safe_str(asset.get("asset_type_id") or "").strip(),
        "label": "",
        "inputs": [],
        "outputs": [],
    }
    try:
        from .asset_types import load_asset_type

        tid = tmpl["asset_type_id"]
        if tid:
            td = load_asset_type(tid)
            if isinstance(td, dict):
                tmpl["label"] = safe_str(td.get("label") or "").strip()
                if isinstance(td.get("inputs"), list):
                    tmpl["inputs"] = [safe_str(x.get("key")) for x in td.get("inputs") if isinstance(x, dict) and safe_str(x.get("key")).strip()]
                if isinstance(td.get("outputs"), list):
                    tmpl["outputs"] = [safe_str(x.get("key")) for x in td.get("outputs") if isinstance(x, dict) and safe_str(x.get("key")).strip()]
    except Exception:
        pass

    context_obj = {
        "portfolio_name": portfolio_name,
        "asset_effective_emissions_tco2e_per_year": eff_emissions,
        "subtree_produced_emissions_tco2e_per_year": (None if subtree_contributing <= 0 else float(subtree_total_tco2e)),
        "subtree_stats": {
            "visited": subtree_visited,
            "assets_with_values": subtree_contributing,
            "overrides_used": subtree_overrides_used,
        },
        "asset_template": tmpl,
        "asset_data_fields": data_fields,
        "asset_json": asset,
    }

    prompt = (
        "You are a decarbonisation advisor. Given a single asset within a property portfolio, "
        "suggest up to 5 practical emissions-reduction or sequestration improvements. "
        "Return ONLY valid JSON with this schema: {\"recommendations\": [ {\"title\": str, "
        "\"description\": str, \"estimated_saving_tco2_per_year\": number, "
        "\"action\": \"add\"|\"remove\"|\"switch\"|\"other\", \"add_asset\": object|null} ] }. "
        "If action is 'add' or 'switch', include add_asset as a minimal asset JSON object like {\"name\": str, \"core_type\": str, \"subtype\": str} when possible. "
        "If action is 'remove' it applies to the currently selected asset. "
        "If action is 'switch' it means: add add_asset at the same level as the selected asset, and retire the selected asset. "
        "Use the best available baseline for savings: prefer subtree produced emissions (asset + children) when provided, else asset effective emissions. "
        "Savings must be non-negative and should not exceed the baseline magnitude.\n\n"
        f"Asset effective emissions tCO2e/year (positive emits, negative sequesters, null unknown): {eff_text}\n"
        f"Subtree produced emissions tCO2e/year (asset + descendants, negative treated as 0): {subtree_text}\n\n"
        "Context JSON (template/data_fields/subtree stats included):\n"
        + json.dumps(context_obj, ensure_ascii=False)
        + "\n"
    )

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You respond with strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )

    content = (resp.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(content)
        recs = parsed.get("recommendations", [])
        if isinstance(recs, list):
            cleaned: List[Dict[str, Any]] = []
            for item in recs[:5]:
                if not isinstance(item, dict):
                    continue
                action = safe_str(item.get("action") or "other").lower()
                if action not in {"add", "remove", "switch", "other"}:
                    action = "other"
                add_asset = item.get("add_asset")
                if not isinstance(add_asset, dict):
                    add_asset = None
                try:
                    saving = float(item.get("estimated_saving_tco2_per_year", 0) or 0)
                except Exception:
                    saving = 0.0
                if saving < 0:
                    saving = 0.0
                # Clamp to baseline magnitude when known to keep savings realistic.
                # Prefer subtree baseline (container assets often have 0 direct emissions).
                baseline_mag = None
                try:
                    if subtree_contributing > 0:
                        baseline_mag = abs(float(subtree_total_tco2e))
                except Exception:
                    baseline_mag = None
                try:
                    if eff_emissions is not None:
                        eff_mag = abs(float(eff_emissions))
                        baseline_mag = eff_mag if baseline_mag is None else max(baseline_mag, eff_mag)
                except Exception:
                    pass
                if baseline_mag is not None:
                    try:
                        if saving > baseline_mag:
                            saving = float(baseline_mag)
                    except Exception:
                        pass

                cleaned.append(
                    {
                        "title": safe_str(item.get("title")),
                        "estimated_saving_tco2_per_year": float(saving),
                        "description": safe_str(item.get("description") or item.get("explanation")),
                        "action": action,
                        "add_asset": add_asset,
                    }
                )
            return cleaned
    except Exception:
        pass

    return heuristic_recommendations(asset)
