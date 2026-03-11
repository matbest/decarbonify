from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .portfolio_io import safe_str


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
                "title": "Replace gas boiler with heat pump",
                "estimated_saving_tco2_per_year": 2.4,
                "explanation": "Switching from gas combustion to an efficient heat pump typically cuts operational emissions, especially with greener electricity.",
            }
        )
        recs.append(
            {
                "title": "Improve building/pipework insulation",
                "estimated_saving_tco2_per_year": 0.6,
                "explanation": "Reducing heat loss lowers heat demand regardless of heating technology.",
            }
        )

    if asset_type in {"lighting", "infrastructure"} or "light" in name.lower():
        recs.append(
            {
                "title": "Upgrade to LED + controls",
                "estimated_saving_tco2_per_year": 0.3,
                "explanation": "LEDs and occupancy/daylight controls reduce electricity consumption while maintaining lighting levels.",
            }
        )

    if asset_type in {"land", "natural_feature"}:
        recs.append(
            {
                "title": "Increase biodiversity planting",
                "estimated_saving_tco2_per_year": 0.5,
                "explanation": "Tree and hedgerow planting, soil improvements, and reduced mowing can increase sequestration over time.",
            }
        )

    if asset_type in {"building", "room"}:
        recs.append(
            {
                "title": "Add smart heating controls",
                "estimated_saving_tco2_per_year": 0.4,
                "explanation": "Better schedules, zoning, and setpoints often reduce wasted heating and improve comfort.",
            }
        )

    if asset_type in {"energy_generation", "renewable_energy"} or "solar" in name.lower():
        recs.append(
            {
                "title": "Verify inverter performance + monitoring",
                "estimated_saving_tco2_per_year": 0.1,
                "explanation": "Monitoring helps catch faults early and ensures the system delivers expected generation.",
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
        "\"estimated_saving_tco2_per_year\": number, \"explanation\": str} ] }. "
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
                cleaned.append(
                    {
                        "title": safe_str(item.get("title")),
                        "estimated_saving_tco2_per_year": float(item.get("estimated_saving_tco2_per_year", 0) or 0),
                        "explanation": safe_str(item.get("explanation")),
                    }
                )
            return cleaned
    except Exception:
        pass

    return heuristic_recommendations(asset)
