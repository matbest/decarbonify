from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List


DATA_FIELDS_KEY = "data_fields"
EMISSIONS_KEY = "emissions_tco2e_per_year"


def safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def load_portfolio_from_bytes(raw_bytes: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    validate_portfolio(data)
    return data


def load_portfolio_from_path(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    validate_portfolio(data)
    return data


def validate_portfolio(portfolio: Dict[str, Any]) -> None:
    if not isinstance(portfolio, dict):
        raise ValueError("Portfolio must be a JSON object")
    if "portfolio_name" not in portfolio:
        raise ValueError("Portfolio must contain 'portfolio_name'")
    if "assets" not in portfolio or not isinstance(portfolio["assets"], list):
        raise ValueError("Portfolio must contain 'assets' as a list")


def ensure_asset_ids(portfolio: Dict[str, Any], *, id_key: str = "_id") -> None:
    """Ensure every asset dict in the portfolio has a stable unique id.

    This mutates the portfolio in-place. It is idempotent.
    """

    seen: set[str] = set()

    def walk(assets: Any) -> None:
        for asset in as_list(assets):
            if not isinstance(asset, dict):
                continue

            existing = asset.get(id_key)
            asset_id = existing if isinstance(existing, str) and existing.strip() else ""

            if not asset_id or asset_id in seen:
                # uuid4().hex is URL/DOM-friendly and short enough for widget keys.
                asset_id = uuid.uuid4().hex
                while asset_id in seen:
                    asset_id = uuid.uuid4().hex
                asset[id_key] = asset_id

            seen.add(asset_id)
            walk(asset.get("assets"))

    walk(portfolio.get("assets"))


def ensure_asset_data_fields(portfolio: Dict[str, Any]) -> None:
    """Ensure every asset has a `data_fields` mapping with a minimum emissions field.

    This mutates the portfolio in-place. It is idempotent.

    Schema (per field key):
        {
          "label": str,
          "kind": "number"|"string"|...,
          "unit": str (optional),
          "derived": {"value": Any, ...},
          "manual": {"value": Any, ...}
        }
    """

    def ensure_field(asset: Dict[str, Any], *, key: str, label: str, kind: str, unit: str) -> None:
        fields = asset.get(DATA_FIELDS_KEY)
        if not isinstance(fields, dict):
            fields = {}
            asset[DATA_FIELDS_KEY] = fields

        entry = fields.get(key)
        if not isinstance(entry, dict):
            entry = {}
            fields[key] = entry

        entry.setdefault("label", label)
        entry.setdefault("kind", kind)
        if unit:
            entry.setdefault("unit", unit)

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

    def walk(assets: Any) -> None:
        for asset in as_list(assets):
            if not isinstance(asset, dict):
                continue
            ensure_field(
                asset,
                key=EMISSIONS_KEY,
                label="Emissions",
                kind="number",
                unit="tCO2e/year",
            )
            walk(asset.get("assets"))

    walk(portfolio.get("assets"))


def ensure_asset_ontology_fields(portfolio: Dict[str, Any]) -> None:
    """Ensure every asset has optional ontology fields.

    This enforces the ontology fields used by the app.

    Portfolios created with older versions may still contain legacy `asset['type']`.
    We infer (core_type, subtype) from that field when needed, then remove `type`
    to keep the schema consistent.

    Fields added (if missing):
      - core_type: one of place/activity/asset/energy_system/resource/surface
      - subtype: free-text string
      - current_role: one of producer/consumer/converter/storage/transport/loss/passive (or "")
      - location: free-text string
      - quantity: number or None
      - attributes: dict

    This mutates the portfolio in-place. It is idempotent.
    """

    # Local import to avoid circular deps and keep this module lightweight.
    from .ontology import (
        infer_core_type_and_subtype,
        infer_current_role,
        normalize_core_type,
        normalize_energy_role,
    )

    def walk(assets: Any) -> None:
        for asset in as_list(assets):
            if not isinstance(asset, dict):
                continue

            legacy_type = safe_str(asset.get("type")).strip()

            # core_type / subtype
            core_type = safe_str(asset.get("core_type"))
            subtype = safe_str(asset.get("subtype"))

            if core_type.strip():
                asset["core_type"] = normalize_core_type(core_type)
            else:
                inferred_core, inferred_sub = infer_core_type_and_subtype(legacy_type=legacy_type)
                asset["core_type"] = inferred_core
                if not subtype.strip() and inferred_sub:
                    asset["subtype"] = inferred_sub
                else:
                    asset.setdefault("subtype", subtype)

            # If legacy type exists, keep it only as a subtype hint.
            # (e.g. older portfolios used type='building'; we already mapped that.)

            # current_role
            cur_role = normalize_energy_role(safe_str(asset.get("current_role")))
            if cur_role:
                asset["current_role"] = cur_role
            else:
                # Best-effort inference when missing.
                inferred_role = infer_current_role(core_type=safe_str(asset.get("core_type")), subtype=safe_str(asset.get("subtype")))
                asset.setdefault("current_role", inferred_role or "")

            # potential_roles used to exist in an earlier ontology draft; remove it.
            asset.pop("potential_roles", None)

            # Remove the legacy field once we've migrated it.
            asset.pop("type", None)

            # location
            loc = asset.get("location")
            if loc is None:
                asset.setdefault("location", "")
            else:
                asset["location"] = safe_str(loc).strip()

            # quantity
            if "quantity" not in asset:
                asset["quantity"] = None
            else:
                qty = asset.get("quantity")
                if qty is None or isinstance(qty, (int, float)):
                    pass
                elif isinstance(qty, str):
                    s = qty.strip()
                    if not s:
                        asset["quantity"] = None
                    else:
                        try:
                            asset["quantity"] = float(s) if ("." in s) else int(s)
                        except Exception:
                            # Leave as-is if it's a meaningful non-numeric string.
                            pass

            # attributes
            attrs = asset.get("attributes")
            if not isinstance(attrs, dict):
                asset["attributes"] = {}

            walk(asset.get("assets"))

    walk(portfolio.get("assets"))
