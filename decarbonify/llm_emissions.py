from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .portfolio_io import safe_str
from .recommendations import openai_client_available


EMISSIONS_KEY = "emissions_tco2e_per_year"


def _default_grid_intensity_kgco2e_per_kwh() -> float:
    """Default grid carbon intensity used when the user doesn't provide one.

    Uses env DEFAULT_GRID_INTENSITY_KGCO2E_PER_KWH (shared with the solar fallback).
    """

    try:
        return float(os.environ.get("DEFAULT_GRID_INTENSITY_KGCO2E_PER_KWH", "0.20") or 0.20)
    except Exception:
        return 0.20


def _field_effective_value(asset: Dict[str, Any], key: str) -> Any:
    fields = asset.get("data_fields")
    if not isinstance(fields, dict):
        return None
    entry = fields.get(key)
    if not isinstance(entry, dict):
        return None
    manual = entry.get("manual")
    if isinstance(manual, dict) and manual.get("value") is not None:
        return manual.get("value")
    derived = entry.get("derived")
    if isinstance(derived, dict):
        return derived.get("value")
    return None


def _is_solar_asset(asset: Dict[str, Any]) -> bool:
    t = safe_str(asset.get("type")).lower()
    n = safe_str(asset.get("name")).lower()
    if any(x in t for x in ["solar", "pv", "energy_generation", "renewable"]):
        return True
    if any(x in n for x in ["solar", "pv", "panel"]):
        return True
    return False


def _is_lighting_asset(asset: Dict[str, Any]) -> bool:
    t = safe_str(asset.get("type")).lower()
    n = safe_str(asset.get("name")).lower()
    if any(x in t for x in ["light", "lighting", "floodlight", "lamp", "led"]):
        return True
    if any(x in n for x in ["light", "lighting", "floodlight", "lamp", "led"]):
        return True
    return False


def _infer_fuel(asset: Dict[str, Any]) -> str:
    # Prefer explicit user-provided values.
    direct = safe_str(asset.get("fuel")) or safe_str(_field_effective_value(asset, "fuel"))
    if direct:
        return direct

    # Conservative defaults only for categories that are overwhelmingly electric.
    if _is_lighting_asset(asset):
        return "electricity"

    return ""


def _lighting_fallback_estimate(asset: Dict[str, Any]) -> Tuple[Optional[float], str, List[str], str]:
    """Deterministic estimate for lighting/floodlights when inputs are available.

    Uses electricity carbon intensity in kgCO2e/kWh. Accepts either total_power_watts OR (count * average_power_watts).
    """

    missing: List[str] = []
    equation = r"tCO_2e/yr = (P_{total}/1000) \cdot h_{day} \cdot 365 \cdot (CI/1000)"

    hours = _field_effective_value(asset, "average_daily_usage_hours")
    if hours is None:
        hours = _field_effective_value(asset, "daily_usage_hours")
    if hours is None:
        missing.append("average_daily_usage_hours")

    total_power_watts = _field_effective_value(asset, "total_power_watts")
    if total_power_watts is None:
        count = _field_effective_value(asset, "count")
        if count is None:
            count = _field_effective_value(asset, "number_of_floodlights")
        watts_each = _field_effective_value(asset, "average_power_watts")
        if watts_each is None:
            watts_each = _field_effective_value(asset, "power_watts_each")
        if count is None:
            missing.append("count")
        if watts_each is None:
            missing.append("average_power_watts")
        if count is not None and watts_each is not None:
            try:
                total_power_watts = float(count) * float(watts_each)
            except Exception:
                total_power_watts = None

    if total_power_watts is None:
        missing.append("total_power_watts")

    ci = _field_effective_value(asset, "carbon_intensity_of_electricity")
    if ci is None:
        ci = _default_grid_intensity_kgco2e_per_kwh()

    if missing:
        # De-duplicate but keep stable order.
        seen: set[str] = set()
        missing2: List[str] = []
        for k in missing:
            if k in seen:
                continue
            seen.add(k)
            missing2.append(k)
        return None, "Insufficient data for deterministic lighting estimate.", missing2, equation

    try:
        p_w = float(total_power_watts)
        h_day = float(hours)
        ci_kg_per_kwh = float(ci)
    except Exception:
        return None, "Could not parse lighting inputs as numbers.", ["total_power_watts", "average_daily_usage_hours"], equation

    annual_kwh = (p_w / 1000.0) * h_day * 365.0
    tco2 = (annual_kwh * ci_kg_per_kwh) / 1000.0

    notes = (
        "Deterministic lighting estimate assuming electricity. "
        f"annual_kwh=(total_power_watts/1000)*hours_per_day*365; "
        f"tCO2e=(annual_kwh*carbon_intensity_of_electricity)/1000. "
        f"Using total_power_watts={p_w:g} W, average_daily_usage_hours={h_day:g} h/day, "
        f"carbon_intensity_of_electricity={ci_kg_per_kwh:g} kgCO2e/kWh (default if not provided)."
    )
    return float(tco2), notes, [], equation


def _solar_fallback_estimate(asset: Dict[str, Any]) -> Tuple[Optional[float], str, List[str]]:
    """Best-effort deterministic estimate for solar/PV.

    Uses annual_generation_kwh if present. Computes avoided emissions (negative).
    Grid factor is configurable via env var DEFAULT_GRID_INTENSITY_KGCO2E_PER_KWH.
    """

    gen = _field_effective_value(asset, "annual_generation_kwh")
    if gen is None:
        return None, "Missing annual_generation_kwh.", ["annual_generation_kwh"]
    try:
        gen_kwh = float(gen)
    except Exception:
        return None, "annual_generation_kwh is not a number.", ["annual_generation_kwh"]

    assumed = _default_grid_intensity_kgco2e_per_kwh()

    # Avoided emissions from displaced grid electricity.
    tco2 = -(gen_kwh * assumed) / 1000.0
    notes = (
        f"Estimated avoided emissions from solar generation: -annual_generation_kwh * {assumed:.3f} kgCO2e/kWh / 1000. "
        "(Assumed grid displacement factor; override with env DEFAULT_GRID_INTENSITY_KGCO2E_PER_KWH if desired.)"
    )
    return float(tco2), notes, []


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


def suggest_emissions_inputs(
    *,
    portfolio: Dict[str, Any],
    asset: Dict[str, Any],
    max_fields: int = 3,
    focus_missing_keys: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """Ask the LLM which per-asset inputs it needs to estimate annual tCO2e.

    Returns: (fields, reply)

    fields schema (best-effort):
      {"key": str, "label": str, "kind": "number"|"string"|"boolean", "unit": str|"", "question": str}
    """

    try:
        max_fields = int(max_fields or 0)
    except Exception:
        max_fields = 3
    if max_fields <= 0:
        max_fields = 3

    client, model_or_err = _openai_client()
    if client is None:
        return [], str(model_or_err or "AI unavailable.")

    model: str = str(model_or_err)

    # Provide only minimal context to keep this per-asset.
    asset_type = safe_str(asset.get("type"))
    asset_name = safe_str(asset.get("name"))
    inferred_fuel = _infer_fuel(asset)

    fields = asset.get("data_fields")
    fields = fields if isinstance(fields, dict) else {}

    # Only tell the LLM which keys already exist + whether a manual value is present.
    present: List[Dict[str, Any]] = []
    for k, entry in fields.items():
        if not isinstance(k, str) or not k:
            continue
        if not isinstance(entry, dict):
            continue
        manual = entry.get("manual")
        derived = entry.get("derived")
        manual_v = manual.get("value") if isinstance(manual, dict) else None
        derived_v = derived.get("value") if isinstance(derived, dict) else None
        present.append({"key": k, "has_manual": manual_v is not None, "has_derived": derived_v is not None})

    system = (
        "You help collect per-asset inputs for estimating annual CO2 impact. "
        "Return STRICT JSON only (no prose, no markdown)."
    )

    missing_hint = ""
    if isinstance(focus_missing_keys, list) and focus_missing_keys:
        cleaned_missing = [safe_str(x) for x in focus_missing_keys if safe_str(x)]
        if cleaned_missing:
            missing_hint = (
                "\nThe estimator previously reported these missing_field_keys: "
                + json.dumps(cleaned_missing, ensure_ascii=False)
                + "\nPrioritise asking for inputs that satisfy these missing keys (if they make sense for the asset).\n"
            )

    user = (
        "Given ONE asset, propose the minimum set of input fields the user should fill in so you can estimate annual tCO2e produced (positive) or sequestered (negative).\n"
        "Rules:\n"
        "- Ask only questions relevant to this asset type/name.\n"
        "- Do NOT ask for fields that already have a manual or derived value.\n"
        "- Prefer inputs that normal people can answer from bills or common knowledge (counts, areas, monthly bill amounts).\n"
        "- Prefer consolidated inputs that avoid multiplication (e.g. ask for total_power_watts instead of count + watts_each).\n"
        "- Prefer integer-like numeric fields when possible (e.g. panel_count, field_area_acres, gas_bill_per_month).\n"
        "- For solar/PV assets, prefer a bill/monitoring-friendly annual energy number (e.g. annual_generation_kwh or annual_export_kwh). If unknown, fall back to panel_count.\n"
        "- For lighting assets (floodlights/LEDs), assume fuel is electricity unless the user indicates otherwise; do not ask for fuel in that case.\n"
        "- Aim for 1 field if that is sufficient; otherwise 2. Only use 3 if genuinely required.\n"
        "- Use keys in snake_case. Prefer generic units.\n"
        f"- Limit to at most {max_fields} fields.\n"
        "Return schema: {\"reply\": str, \"fields\": [ {\"key\": str, \"label\": str, \"kind\": \"number\"|\"string\"|\"boolean\", \"unit\": str, \"question\": str} ] }\n\n"
        f"Asset name: {asset_name}\n"
        f"Asset type: {asset_type}\n"
        f"Inferred fuel (if any): {inferred_fuel}\n"
        f"Existing fields presence: {json.dumps(present, ensure_ascii=False)}\n"
        f"{missing_hint}"
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
    parsed = _parse_jsonish(content) or {}

    reply = safe_str(parsed.get("reply")) or "OK."
    out_fields = parsed.get("fields")
    if not isinstance(out_fields, list):
        return [], reply

    cleaned: List[Dict[str, Any]] = []
    for item in out_fields:
        if not isinstance(item, dict):
            continue
        key = safe_str(item.get("key"))
        if not key:
            continue
        if key == EMISSIONS_KEY:
            continue
        if key == "fuel" and inferred_fuel:
            # If we can infer fuel safely, don't make the user answer it.
            continue
        kind = safe_str(item.get("kind")).lower() or "string"
        if kind not in {"number", "string", "boolean"}:
            kind = "string"
        cleaned.append(
            {
                "key": key,
                "label": safe_str(item.get("label")) or key,
                "kind": kind,
                "unit": safe_str(item.get("unit")),
                "question": safe_str(item.get("question")) or "",
            }
        )

    # Post-process: for lighting, prefer total_power_watts over (count + per-light power).
    if _is_lighting_asset(asset):
        keys = {safe_str(x.get("key")) for x in cleaned if isinstance(x, dict)}

        def _is_light_count_key(k0: str) -> bool:
            k1 = (k0 or "").lower()
            if k1 in {"count", "number_of_floodlights", "floodlight_count", "floodlights_count", "num_floodlights"}:
                return True
            return ("count" in k1 or "number_of" in k1) and ("light" in k1 or "flood" in k1)

        def _is_per_unit_power_key(k0: str) -> bool:
            k1 = (k0 or "").lower()
            if k1 in {"average_power_watts", "power_watts_each", "watts_each", "wattage_each"}:
                return True
            return "power" in k1 and ("each" in k1 or "per" in k1 or "average" in k1)

        has_count = any(_is_light_count_key(k) for k in keys)
        has_power_each = any(_is_per_unit_power_key(k) for k in keys)
        if has_count and has_power_each:
            cleaned2: List[Dict[str, Any]] = []
            for f in cleaned:
                k = safe_str(f.get("key"))
                if not k:
                    continue
                if _is_light_count_key(k) or _is_per_unit_power_key(k):
                    continue
                cleaned2.append(f)

            if "total_power_watts" not in {safe_str(x.get("key")) for x in cleaned2}:
                cleaned2.append(
                    {
                        "key": "total_power_watts",
                        "label": "Total Power (Watts)",
                        "kind": "number",
                        "unit": "watts",
                        "question": "What is the total power of all the floodlights together (W)? (e.g. sum of each light's wattage)",
                    }
                )
            cleaned = cleaned2

    return cleaned[:max_fields], reply


def estimate_emissions_tco2e_per_year(
    *,
    portfolio: Dict[str, Any],
    asset: Dict[str, Any],
) -> Tuple[Optional[float], str, List[str], str]:
    """Estimate annual tCO2e for this asset given available inputs.

    Returns: (tco2e_per_year_or_None, notes, missing_field_keys)
    """

    # Deterministic fallback for solar/PV: always return missing keys instead of calling the LLM.
    if _is_solar_asset(asset):
        val, notes, missing = _solar_fallback_estimate(asset)
        equation = r"tCO_2e/yr = -annual\_generation\_kwh \cdot CI / 1000"
        return val, notes, missing, equation

    # Deterministic fallback for lighting/floodlights: always return missing keys instead of calling the LLM.
    if _is_lighting_asset(asset) and _infer_fuel(asset).lower() in {"electricity", "electric", "grid"}:
        return _lighting_fallback_estimate(asset)

    client, model_or_err = _openai_client()
    if client is None:
        return None, str(model_or_err or "AI unavailable."), [], ""

    model: str = str(model_or_err)

    # Provide a compact input payload excluding the output emissions field.
    fields = asset.get("data_fields")
    fields = fields if isinstance(fields, dict) else {}

    fuel_effective = _infer_fuel(asset)
    available: Dict[str, Any] = {}
    for k, entry in fields.items():
        if not isinstance(k, str) or not k or k == EMISSIONS_KEY:
            continue
        if not isinstance(entry, dict):
            continue
        manual = entry.get("manual")
        derived = entry.get("derived")
        unit = safe_str(entry.get("unit"))
        v = None
        if isinstance(manual, dict) and manual.get("value") is not None:
            v = manual.get("value")
        elif isinstance(derived, dict) and derived.get("value") is not None:
            v = derived.get("value")
        if v is None:
            continue
        available[k] = {"value": v, "unit": unit}

    # If the asset is electric-powered, auto-supply a default electricity carbon intensity
    # so the model doesn't require the user to look it up.
    if fuel_effective.lower() in {"electricity", "electric", "grid"}:
        if "carbon_intensity_of_electricity" not in available:
            available["carbon_intensity_of_electricity"] = {
                "value": _default_grid_intensity_kgco2e_per_kwh(),
                "unit": "kgCO2e/kWh",
                "notes": "Assumed default from DEFAULT_GRID_INTENSITY_KGCO2E_PER_KWH",
            }

    payload = {
        "name": safe_str(asset.get("name")),
        "type": safe_str(asset.get("type")),
        "fuel": fuel_effective,
        "available_inputs": available,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    system = "You estimate annual tCO2e for a single asset. Return STRICT JSON only."
    user = (
        "Estimate annual tCO2e for this asset. Positive means emissions produced; negative means net sequestration/avoided emissions.\n"
        f"The field '{EMISSIONS_KEY}' is the OUTPUT and must NOT appear in missing_field_keys.\n"
        "If there isn't enough data, return tco2e_per_year=null and list missing_field_keys (only input keys the user can provide).\n"
        "Also include an equation_latex string if possible.\n"
        "Return schema: {\"tco2e_per_year\": number|null, \"notes\": str, \"equation_latex\": str, \"missing_field_keys\": [str]}\n\n"
        f"Asset payload (inputs only): {payload_json}\n"
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
    parsed = _parse_jsonish(content) or {}

    notes = safe_str(parsed.get("notes")) or safe_str(parsed.get("reply")) or ""
    equation = safe_str(parsed.get("equation_latex"))
    missing = parsed.get("missing_field_keys")
    missing_keys: List[str] = []
    if isinstance(missing, list):
        for k in missing:
            ks = safe_str(k)
            if ks:
                if ks == EMISSIONS_KEY:
                    continue
                # This is auto-supplied when fuel is electric.
                if ks == "carbon_intensity_of_electricity" and fuel_effective.lower() in {"electricity", "electric", "grid"}:
                    continue
                # Fuel can be inferred for some categories (e.g. lighting).
                if ks == "fuel" and bool(fuel_effective):
                    continue
                missing_keys.append(ks)

    val = parsed.get("tco2e_per_year")
    if val is None:
        return None, notes or "Insufficient data.", missing_keys, equation

    try:
        return float(val), notes or "OK.", missing_keys, equation
    except Exception:
        return None, notes or "Could not parse estimate.", missing_keys, equation
