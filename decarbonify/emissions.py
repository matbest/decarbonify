from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

from .portfolio_io import as_list, safe_str

DATA_FIELDS_KEY = "data_fields"
EMISSIONS_KEY = "emissions_tco2e_per_year"


def _as_float(value: Any) -> Optional[float]:
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


def _get_field_entry(asset: Dict[str, Any], *, key: str) -> Optional[Dict[str, Any]]:
    fields = asset.get(DATA_FIELDS_KEY)
    if not isinstance(fields, dict):
        return None
    entry = fields.get(key)
    return entry if isinstance(entry, dict) else None


def is_retired(asset: Dict[str, Any]) -> bool:
    lifecycle = asset.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return False
    return safe_str(lifecycle.get("status")).lower() == "retired"


def get_derived_value(asset: Dict[str, Any], *, key: str) -> Any:
    entry = _get_field_entry(asset, key=key)
    if not entry:
        return None
    derived = entry.get("derived")
    if not isinstance(derived, dict):
        return None
    return derived.get("value")


def get_manual_value(asset: Dict[str, Any], *, key: str) -> Any:
    entry = _get_field_entry(asset, key=key)
    if not entry:
        return None
    manual = entry.get("manual")
    if not isinstance(manual, dict):
        return None
    return manual.get("value")


def extract_emissions_tco2e_per_year(asset: Dict[str, Any]) -> Optional[float]:
    """Extract the derived annual emissions for a single asset in tCO2e/year."""

    raw = _as_float(get_derived_value(asset, key=EMISSIONS_KEY))
    return None if raw is None else float(raw)


def effective_emissions_tco2e_per_year(asset: Dict[str, Any]) -> Optional[float]:
    """Return the effective emissions value for this asset.

    Manual value takes precedence over derived value.
    """

    if is_retired(asset):
        return None

    manual = _as_float(get_manual_value(asset, key=EMISSIONS_KEY))
    if manual is not None:
        return float(manual)
    return extract_emissions_tco2e_per_year(asset)


def iter_asset_and_descendants(asset: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    stack = [asset]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            children = as_list(current.get("assets"))
            for child in reversed(children):
                if isinstance(child, dict):
                    stack.append(child)


def sum_emissions_produced_tco2e_per_year(
    asset: Dict[str, Any],
) -> Tuple[float, int, int, int]:
    """Sum *produced* emissions across an asset + all descendants.

    - Produced means negative values are treated as 0 (so sequestration does not reduce this total).

    Returns: (total_tco2e_per_year, contributing_assets_count, visited_assets_count, overrides_used_count)
    """

    total = 0.0
    contributing = 0
    visited = 0
    overrides_used = 0

    for a in iter_asset_and_descendants(asset):
        visited += 1
        if _as_float(get_manual_value(a, key=EMISSIONS_KEY)) is not None:
            overrides_used += 1

        v = effective_emissions_tco2e_per_year(a)
        if v is None:
            continue
        contributing += 1
        total += max(0.0, float(v))

    return total, contributing, visited, overrides_used


def emissions_field_help_text() -> str:
    return (
        "No emissions values found yet. "
        "Enter a manual value in 'Data' for: "
        f"{safe_str(EMISSIONS_KEY)}"
    )
