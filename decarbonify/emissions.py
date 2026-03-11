from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

from .portfolio_io import as_list, safe_str


EMISSIONS_FIELD = "estimated_emissions_tco2e_per_year"
USER_OVERRIDE_FIELD = "user_emissions_override_tco2e_per_year"


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


def extract_emissions_tco2e_per_year(asset: Dict[str, Any]) -> Optional[float]:
    """Extract a single asset's annual emissions in tCO2e/year.

    Uses a single, standard field to keep the schema unambiguous for later
    LLM population.

    Returns None when no supported value exists.
    """

    raw = _as_float(asset.get(EMISSIONS_FIELD))
    if raw is None:
        return None
    return float(raw)


def effective_emissions_tco2e_per_year(asset: Dict[str, Any]) -> Optional[float]:
    """Return the emissions value for this asset.

    If a user override is present on the asset JSON, it takes precedence.
    """

    ov = _as_float(asset.get(USER_OVERRIDE_FIELD))
    if ov is not None:
        return float(ov)
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
        if _as_float(a.get(USER_OVERRIDE_FIELD)) is not None:
            overrides_used += 1

        v = effective_emissions_tco2e_per_year(a)
        if v is None:
            continue
        contributing += 1
        total += max(0.0, float(v))

    return total, contributing, visited, overrides_used


def emissions_field_help_text() -> str:
    return (
        "No per-asset emissions fields found. "
        "Add this numeric field to assets (interpreted as tCO₂e/year): "
        f"{safe_str(EMISSIONS_FIELD)}"
    )
