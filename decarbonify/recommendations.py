from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Mapping

from .portfolio_io import safe_str


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


def carbon_signal(asset: Dict[str, Any]) -> str:
    asset_type = safe_str(asset.get("type", ""))
    fuel = safe_str(asset.get("fuel", "")).lower()

    if fuel in {"gas", "diesel", "petrol", "oil", "lpg"}:
        return "emits (combustion fuel)"
    if asset_type in {"energy_generation", "renewable_energy", "solar", "solar_panels"}:
        return "reduces (onsite generation)"
    if asset_type in {"natural_feature", "trees", "woodland", "wetlands", "soil", "grassland"}:
        return "sequesters (natural carbon)"
    if asset_type in {"lighting", "equipment", "infrastructure", "building", "room"}:
        return "consumes (likely electricity/heat)"
    if asset_type in {"energy_system", "hvac", "boiler"}:
        return "emits/consumes (heating system)"
    return "unknown"


def heuristic_recommendations(asset: Dict[str, Any]) -> List[Dict[str, Any]]:
    asset_type = safe_str(asset.get("type", ""))
    fuel = safe_str(asset.get("fuel", "")).lower()
    name = safe_str(asset.get("name", "asset"))

    recs: List[Dict[str, Any]] = []

    if fuel == "gas" or "boiler" in name.lower():
        recs.append(
            {
                "title": "Switch gas boiler to heat pump",
                "estimated_saving_tco2_per_year": 2.4,
                "description": "Switching from gas combustion to an efficient heat pump typically cuts operational emissions, especially with greener electricity.",
                "action": "switch",
                "add_asset": {"name": "Heat Pump", "type": "energy_system", "fuel": "electric"},
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

    if asset_type in {"lighting", "infrastructure"} or "light" in name.lower():
        recs.append(
            {
                "title": "Upgrade to LED + controls",
                "estimated_saving_tco2_per_year": 0.3,
                "description": "LEDs and occupancy/daylight controls reduce electricity consumption while maintaining lighting levels.",
                "action": "other",
            }
        )

    if asset_type in {"land", "natural_feature"}:
        recs.append(
            {
                "title": "Add trees / biodiversity planting",
                "estimated_saving_tco2_per_year": 0.5,
                "description": "Tree and hedgerow planting, soil improvements, and reduced mowing can increase sequestration over time.",
                "action": "add",
                "add_asset": {"name": "Trees / planting", "type": "natural_feature", "feature": "trees"},
            }
        )

    if asset_type in {"building", "room"}:
        recs.append(
            {
                "title": "Add smart heating controls",
                "estimated_saving_tco2_per_year": 0.4,
                "description": "Better schedules, zoning, and setpoints often reduce wasted heating and improve comfort.",
                "action": "other",
            }
        )

    if asset_type in {"energy_generation", "renewable_energy"} or "solar" in name.lower():
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

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI()

    portfolio_name = safe_str(portfolio.get("portfolio_name"))
    asset_json = json.dumps(asset, ensure_ascii=False)

    prompt = (
        "You are a decarbonisation advisor. Given a single asset within a property portfolio, "
        "suggest up to 5 practical emissions-reduction or sequestration improvements. "
        "Return ONLY valid JSON with this schema: {\"recommendations\": [ {\"title\": str, "
        "\"description\": str, \"estimated_saving_tco2_per_year\": number, "
        "\"action\": \"add\"|\"remove\"|\"switch\"|\"other\", \"add_asset\": object|null} ] }. "
        "If action is 'add' or 'switch', include add_asset as a minimal asset JSON object like {\"name\": str, \"type\": str}. "
        "If action is 'remove' it applies to the currently selected asset. "
        "If action is 'switch' it means: add add_asset at the same level as the selected asset, and retire the selected asset. "
        "Keep estimated_saving_tco2_per_year plausible and non-negative.\n\n"
        f"Portfolio name: {portfolio_name}\n"
        f"Asset JSON: {asset_json}\n"
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
                cleaned.append(
                    {
                        "title": safe_str(item.get("title")),
                        "estimated_saving_tco2_per_year": float(item.get("estimated_saving_tco2_per_year", 0) or 0),
                        "description": safe_str(item.get("description") or item.get("explanation")),
                        "action": action,
                        "add_asset": add_asset,
                    }
                )
            return cleaned
    except Exception:
        pass

    return heuristic_recommendations(asset)
