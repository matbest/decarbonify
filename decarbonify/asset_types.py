from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .portfolio_io import safe_str
from .safe_formula import FormulaError, eval_arithmetic


@dataclass(frozen=True)
class AssetTypeSummary:
    id: str
    label: str
    description: str = ""


def _repo_root() -> Path:
    # decarbonify/asset_types.py -> repo_root/decarbonify/asset_types.py
    return Path(__file__).resolve().parents[1]


def asset_type_dir() -> Path:
    return _repo_root() / "asset_types"


@lru_cache(maxsize=1)
def list_asset_type_summaries() -> List[AssetTypeSummary]:
    out: List[AssetTypeSummary] = []
    root = asset_type_dir()
    if not root.exists() or not root.is_dir():
        return out

    for p in sorted(root.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        type_id = safe_str(raw.get("id"))
        label = safe_str(raw.get("label")) or type_id
        desc = safe_str(raw.get("description"))
        if not type_id:
            continue
        out.append(AssetTypeSummary(id=type_id, label=label, description=desc))

    # stable order by label
    out.sort(key=lambda s: (s.label.lower(), s.id.lower()))
    return out


@lru_cache(maxsize=128)
def load_asset_type(type_id: str) -> Optional[Dict[str, Any]]:
    type_id = safe_str(type_id).strip()
    if not type_id:
        return None

    root = asset_type_dir()
    path = root / f"{type_id}.json"
    if not path.exists():
        # allow lookup by scanning (in case filename differs)
        for p in root.glob("*.json"):
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(raw, dict) and safe_str(raw.get("id")) == type_id:
                return raw
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception:
        return None


def _ensure_data_fields(asset: Dict[str, Any]) -> Dict[str, Any]:
    fields = asset.get("data_fields")
    if not isinstance(fields, dict):
        fields = {}
        asset["data_fields"] = fields
    return fields


def _as_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            return None
    return None


def _portfolio_defaults(portfolio: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(portfolio, dict):
        return {}
    d = portfolio.get("defaults")
    return d if isinstance(d, dict) else {}


def _ensure_field_entry(fields: Dict[str, Any], *, key: str, label: str, kind: str, unit: str, help_text: str = "") -> Dict[str, Any]:
    entry = fields.get(key)
    if not isinstance(entry, dict):
        entry = {}
        fields[key] = entry

    entry.setdefault("label", label or key)
    entry.setdefault("kind", kind or "string")
    if unit:
        entry.setdefault("unit", unit)
    if help_text:
        entry.setdefault("question", help_text)

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

    return entry


def apply_asset_type_template(*, asset: Dict[str, Any], type_def: Dict[str, Any], portfolio: Optional[Dict[str, Any]] = None) -> None:
    """Attach an asset type to an asset and ensure the fields exist.

    - Adds/updates asset['asset_type_id']
    - Applies ontology defaults from the template (core_type/subtype/current_role)
    - Applies default attributes from the template (e.g. attributes.energy_type)
    - Ensures input/output field entries exist under asset['data_fields']
    - Applies input defaults into derived.value (only when manual is empty and derived is empty)
    """

    type_id = safe_str(type_def.get("id")).strip()
    if not type_id:
        raise ValueError("Asset type definition missing id")

    asset["asset_type_id"] = type_id

    # Apply ontology fields from template (if present).
    # Local import to avoid circular deps.
    from .ontology import normalize_core_type, normalize_energy_role

    tmpl_core_type = safe_str(type_def.get("core_type")).strip()
    if tmpl_core_type:
        asset["core_type"] = normalize_core_type(tmpl_core_type)

    tmpl_subtype = safe_str(type_def.get("subtype")).strip()
    if tmpl_subtype:
        asset["subtype"] = tmpl_subtype

    tmpl_role = normalize_energy_role(safe_str(type_def.get("current_role")))
    if tmpl_role:
        asset["current_role"] = tmpl_role

    tmpl_attrs = type_def.get("attributes")
    if isinstance(tmpl_attrs, dict) and tmpl_attrs:
        attrs = asset.get("attributes")
        if not isinstance(attrs, dict):
            attrs = {}
            asset["attributes"] = attrs
        # Apply attribute defaults only when missing/blank.
        for k, v in tmpl_attrs.items():
            kk = safe_str(k).strip()
            if not kk or v is None:
                continue
            if safe_str(attrs.get(kk)).strip():
                continue
            attrs[kk] = v

    fields = _ensure_data_fields(asset)

    defaults = _portfolio_defaults(portfolio)

    inputs = type_def.get("inputs")
    if isinstance(inputs, list):
        for item in inputs:
            if not isinstance(item, dict):
                continue
            key = safe_str(item.get("key")).strip()
            if not key:
                continue
            entry = _ensure_field_entry(
                fields,
                key=key,
                label=safe_str(item.get("label")) or key,
                kind=safe_str(item.get("kind")) or "string",
                unit=safe_str(item.get("unit")),
                help_text=safe_str(item.get("help")),
            )

            manual = entry.get("manual")
            derived = entry.get("derived")
            if not isinstance(manual, dict) or not isinstance(derived, dict):
                continue

            # If there's a portfolio-level default for this key, prefill it (so the UI doesn't ask).
            if key in defaults and manual.get("value") is None and derived.get("value") is None:
                dv = defaults.get(key)
                if dv is not None:
                    derived["value"] = dv
                    derived["source"] = "portfolio_default"
                    derived["notes"] = "Prefilled from portfolio defaults. Override manually if needed."
                continue

            # If there's a portfolio-level default for this key, prefer that over per-template defaults.
            if key in defaults:
                continue

            default = item.get("default")
            if default is None:
                continue
            if manual.get("value") is None and derived.get("value") is None:
                derived["value"] = default
                derived.setdefault("source", "default")

    outputs = type_def.get("outputs")
    if isinstance(outputs, list):
        for item in outputs:
            if not isinstance(item, dict):
                continue
            key = safe_str(item.get("key")).strip()
            if not key:
                continue
            _ensure_field_entry(
                fields,
                key=key,
                label=safe_str(item.get("label")) or key,
                kind=safe_str(item.get("kind")) or "number",
                unit=safe_str(item.get("unit")),
                help_text=safe_str(item.get("help")),
            )


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


def compute_asset_type_outputs(
    *,
    asset: Dict[str, Any],
    type_def: Dict[str, Any],
    portfolio: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, float], List[str], List[str]]:
    """Compute formula outputs for an asset type.

    Returns: (computed_values_by_key, missing_inputs, errors)

    - missing_inputs: variable keys that are missing/non-numeric
    - errors: formula errors not directly tied to a missing variable
    """

    outputs = type_def.get("outputs")
    if not isinstance(outputs, list):
        return {}, [], ["Asset type has no outputs"]

    # Build variable map from *inputs* (by key), using effective values.
    inputs = type_def.get("inputs")
    input_keys: List[str] = []
    if isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, dict):
                k = safe_str(item.get("key")).strip()
                if k:
                    input_keys.append(k)

    variables: Dict[str, Any] = {k: _field_effective_value(asset, k) for k in input_keys}

    # Fill missing/non-numeric values from portfolio-level defaults (if present).
    # Also inject defaults for keys not listed as explicit inputs so formulas can
    # reference shared constants like carbon_intensity_of_electricity.
    defaults = _portfolio_defaults(portfolio)
    for k, v in defaults.items():
        if _as_number(v) is None:
            continue
        if k not in variables or _as_number(variables.get(k)) is None:
            variables[k] = v

    computed: Dict[str, float] = {}
    missing: List[str] = []
    errors: List[str] = []

    for out_item in outputs:
        if not isinstance(out_item, dict):
            continue
        out_key = safe_str(out_item.get("key")).strip()
        expr = safe_str(out_item.get("formula")).strip()
        if not out_key or not expr:
            continue

        try:
            value = float(eval_arithmetic(expr, variables=variables))
        except FormulaError as exc:
            msg = str(exc)
            # Heuristic: surface missing variable names separately.
            if msg.startswith("Missing or non-numeric variable:"):
                var = msg.split(":", 1)[-1].strip()
                if var:
                    missing.append(var)
            elif msg.startswith("Unknown variable:"):
                var = msg.split(":", 1)[-1].strip()
                if var:
                    missing.append(var)
            else:
                errors.append(f"{out_key}: {msg}")
            continue
        except Exception as exc:
            errors.append(f"{out_key}: {exc}")
            continue

        computed[out_key] = float(value)

    # De-duplicate missing (keep order)
    seen: set[str] = set()
    missing2: List[str] = []
    for k in missing:
        if k in seen:
            continue
        seen.add(k)
        missing2.append(k)

    return computed, missing2, errors


def persist_computed_outputs(
    *,
    asset: Dict[str, Any],
    type_def: Dict[str, Any],
    computed: Dict[str, float],
) -> None:
    fields = _ensure_data_fields(asset)
    type_id = safe_str(type_def.get("id"))

    outputs = type_def.get("outputs")
    if not isinstance(outputs, list):
        return

    for out_item in outputs:
        if not isinstance(out_item, dict):
            continue
        out_key = safe_str(out_item.get("key")).strip()
        if not out_key or out_key not in computed:
            continue
        label = safe_str(out_item.get("label")) or out_key
        kind = safe_str(out_item.get("kind")) or "number"
        unit = safe_str(out_item.get("unit"))
        expr = safe_str(out_item.get("formula"))

        entry = _ensure_field_entry(fields, key=out_key, label=label, kind=kind, unit=unit)
        derived = entry.get("derived")
        if not isinstance(derived, dict):
            derived = {}
            entry["derived"] = derived

        derived["value"] = float(computed[out_key])
        derived["source"] = "formula"
        if type_id:
            derived["asset_type"] = type_id
        if expr:
            derived["formula"] = expr
        derived["notes"] = f"Computed from asset type '{type_id}' formula. Manual overrides (if any) still take precedence."
