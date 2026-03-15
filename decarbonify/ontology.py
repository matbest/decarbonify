from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from .portfolio_io import safe_str


CORE_TYPES = (
    "place",
    "activity",
    "asset",
    "energy_system",
    "resource",
    "surface",
)

ENERGY_ROLES = (
    "producer",
    "consumer",
    "converter",
    "storage",
    "transport",
    "loss",
    "passive",
)


def normalize_core_type(value: str) -> str:
    t = safe_str(value).strip().lower().replace(" ", "_")
    # Accept a few common variants.
    if t in {"energysystem", "energy-system", "energy"}:
        t = "energy_system"
    if t in {"places"}:
        t = "place"
    if t in {"assets"}:
        t = "asset"
    return t if t in CORE_TYPES else "asset"


def normalize_energy_role(value: str) -> str:
    t = safe_str(value).strip().lower().replace(" ", "_")
    if t in {"consume", "consumes", "consumer"}:
        t = "consumer"
    if t in {"produce", "produces", "producer"}:
        t = "producer"
    if t in {"convert", "converts", "converter"}:
        t = "converter"
    if t in {"store", "stores", "storage"}:
        t = "storage"
    if t in {"transport", "transports", "move", "moves"}:
        t = "transport"
    if t in {"loss", "losses", "leak", "leaks", "waste"}:
        t = "loss"
    if t in {"passive"}:
        t = "passive"
    return t if t in ENERGY_ROLES else ""


def infer_core_type_and_subtype(*, legacy_type: str) -> Tuple[str, str]:
    """Infer ontology fields from the existing `asset['type']` value.

    This keeps the current app behaviour intact (which still uses `type` heavily),
    while enabling a parallel, more universal ontology via `core_type` + `subtype`.

    Returns: (core_type, subtype)
    """

    t = safe_str(legacy_type).strip().lower().replace(" ", "_")
    if not t or t == "asset":
        return ("asset", "")

    # Place-like legacy types.
    if t in {
        "place",
        "land",
        "building",
        "floor",
        "office",
        "room",
        "hall",
        "area",
        "site",
        "warehouse",
        "kitchen",
        "dining_area",
        "field",
        "outdoor_area",
        "natural_feature",
        "trees",
        "woodland",
        "wetlands",
        "soil",
        "grassland",
    }:
        # Keep the original type as a useful subtype.
        return ("place", t if t != "place" else "")

    # Energy systems.
    if t in {
        "energy_system",
        "energy_generation",
        "battery",
        "solar_pv",
        "solar",
        "pv",
        "wind",
        "boiler",
        "heat_pump",
        "generator",
        "ev_charger",
        "thermal_storage",
    }:
        return ("energy_system", t if t != "energy_system" else "")

    if t in {"surface", "roof", "wall", "window", "floor", "ground"}:
        return ("surface", t if t != "surface" else "")

    if t in {"resource", "electricity", "gas", "water", "heat", "waste", "carbon"}:
        return ("resource", t if t != "resource" else "")

    if t in {"activity", "manufacturing", "distribution", "storage", "retail"}:
        return ("activity", t if t != "activity" else "")

    # Default to generic equipment/assets.
    return ("asset", t)


def infer_current_role(*, core_type: str, subtype: str) -> Optional[str]:
    """Best-effort role inference.

    We keep this conservative and return None unless it's obvious.
    """

    ct = normalize_core_type(core_type)
    st = safe_str(subtype).strip().lower().replace(" ", "_")

    if ct == "surface":
        return "passive"
    if ct == "resource":
        return "transport"
    if ct == "energy_system":
        # Boilers consume a fuel (or electricity) to produce heat; they are not converters
        # in the sense of a grid transformer/inverter.
        if "boiler" in st:
            return "consumer"
        if "battery" in st or "storage" in st:
            return "storage"
        if "inverter" in st or "transformer" in st:
            return "converter"
        if "solar" in st or st in {"pv", "wind", "generator"}:
            return "producer"
        return None
    if ct == "asset":
        # Some equipment is clearly an energy consumer even if it's not modeled as an energy_system.
        # Keep this tight to avoid surprising classifications.
        if any(tok in st for tok in {"fridge", "freezer", "refrigerator"}):
            return "consumer"
        return None
    if ct == "activity":
        return None
    if ct == "place":
        return "passive"
    return None


def display_kind(asset: Dict[str, Any]) -> str:
    """Human-friendly kind label derived from ontology fields.

    Format: core_type[/subtype]
    """

    core_type = normalize_core_type(safe_str(asset.get("core_type")) or "asset")
    subtype = safe_str(asset.get("subtype")).strip()
    return f"{core_type}/{subtype}" if subtype else core_type


def search_text(asset: Dict[str, Any]) -> str:
    """Compact normalized text blob for heuristics.

    Includes name, description, core_type/subtype, and selected attributes.
    """

    parts = [
        safe_str(asset.get("name")),
        safe_str(asset.get("description")),
        safe_str(asset.get("core_type")),
        safe_str(asset.get("subtype")),
        safe_str(asset.get("current_role")),
    ]

    attrs = asset.get("attributes")
    if isinstance(attrs, dict):
        for k in ("fuel", "technology", "carrier", "source", "sink"):
            v = attrs.get(k)
            if v is not None:
                parts.append(f"{k}={safe_str(v)}")

    return " ".join(p for p in parts if safe_str(p).strip()).strip().lower()


def hierarchy_category(asset: Dict[str, Any]) -> str:
    """Map an asset to the legacy hierarchy categories.

    Categories are used for containment rules only:
      - land, building, room, place, component, other

    The goal is to preserve existing constraints (e.g. buildings can't contain land)
    while removing dependency on legacy asset['type'].
    """

    ct = normalize_core_type(safe_str(asset.get("core_type")) or "asset")
    st = safe_str(asset.get("subtype")).strip().lower().replace(" ", "_")

    if ct == "place":
        # Preserve the common land/building/room semantics via subtype.
        if st in {
            "land",
            "natural_feature",
            "trees",
            "woodland",
            "wetlands",
            "soil",
            "grassland",
            "field",
            "outdoor_area",
        }:
            return "land"
        if st in {"building", "warehouse"}:
            return "building"
        if st in {"room", "kitchen", "dining_area", "hall", "office", "floor"}:
            return "room"
        return "place"

    # Non-place core types are treated like equipment/components.
    if ct in {"asset", "energy_system", "resource", "surface", "activity"}:
        return "component"

    return "other"
