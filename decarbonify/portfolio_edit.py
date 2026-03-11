from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .portfolio_io import as_list, safe_str


def normalize_asset_type(asset_type: str) -> str:
    t = safe_str(asset_type).strip().lower()
    return t or "asset"


_LANDLIKE_TYPES = {
    "land",
    "natural_feature",
    "trees",
    "woodland",
    "wetlands",
    "soil",
    "grassland",
}

_BUILDINGLIKE_TYPES = {"building"}
_ROOMLIKE_TYPES = {"room"}

# Types that usually represent equipment/components rather than places.
_COMPONENTLIKE_TYPES = {
    "energy_system",
    "energy_generation",
    "lighting",
    "equipment",
    "infrastructure",
}


def _type_category(asset_type: str) -> str:
    t = normalize_asset_type(asset_type)
    if t in _LANDLIKE_TYPES:
        return "land"
    if t in _BUILDINGLIKE_TYPES:
        return "building"
    if t in _ROOMLIKE_TYPES:
        return "room"
    if t in _COMPONENTLIKE_TYPES:
        return "component"
    return "other"


def can_add_child_type(*, parent_type: str, child_type: str) -> bool:
    """Return True if a parent asset of parent_type can contain a child of child_type.

    This is intentionally permissive for unknown/custom types, but enforces the
    main place-hierarchy constraints (e.g. buildings cannot contain land).
    """

    pt = normalize_asset_type(parent_type)
    ct = normalize_asset_type(child_type)

    # The generic type acts as an "escape hatch" and is always allowed.
    if pt == "asset" or ct == "asset":
        return True

    parent_cat = _type_category(pt)
    child_cat = _type_category(ct)

    # Key constraints:
    # - Buildings cannot contain land-like assets (incl. natural features)
    if parent_cat == "building" and child_cat == "land":
        return False
    # - Rooms cannot contain buildings or land-like assets
    if parent_cat == "room" and child_cat in {"building", "land"}:
        return False
    # - Component-like assets cannot contain place-like assets
    if parent_cat == "component" and child_cat in {"room", "building", "land"}:
        return False

    return True


def explain_disallowed_child(*, parent_type: str, child_type: str) -> Optional[str]:
    if can_add_child_type(parent_type=parent_type, child_type=child_type):
        return None
    pt = normalize_asset_type(parent_type)
    ct = normalize_asset_type(child_type)
    if _type_category(pt) == "building" and _type_category(ct) == "land":
        return "A building cannot contain land; add that land asset under a land parent instead."
    if _type_category(pt) == "room" and _type_category(ct) in {"building", "land"}:
        return "A room cannot contain buildings or land; add it higher in the hierarchy (e.g. under a building or land)."
    if _type_category(pt) == "component" and _type_category(ct) in {"room", "building", "land"}:
        return "Equipment/components cannot contain places (land/buildings/rooms)."
    return f"A '{pt}' cannot contain a '{ct}'."


@dataclass
class AssetRef:
    asset: Dict[str, Any]
    parent_list: List[Dict[str, Any]]
    index_in_parent: int
    parent_id: Optional[str]


@dataclass
class RemovedAsset:
    asset: Dict[str, Any]
    parent_id: Optional[str]
    index_in_parent: int


def find_asset_ref(portfolio: Dict[str, Any], *, asset_id: str, id_key: str = "_id") -> Optional[AssetRef]:
    """Find an asset and its parent list/index within a portfolio."""

    target = safe_str(asset_id)
    if not target:
        return None

    def walk(assets: List[Dict[str, Any]], *, parent_id: Optional[str]) -> Optional[AssetRef]:
        for idx, asset in enumerate(assets):
            if not isinstance(asset, dict):
                continue
            if safe_str(asset.get(id_key)) == target:
                return AssetRef(asset=asset, parent_list=assets, index_in_parent=idx, parent_id=parent_id)
            children = as_list(asset.get("assets"))
            if children:
                found = walk(children, parent_id=safe_str(asset.get(id_key)) or parent_id)
                if found:
                    return found
        return None

    roots = as_list(portfolio.get("assets"))
    return walk(roots, parent_id=None)


def remove_asset_by_id(portfolio: Dict[str, Any], *, asset_id: str, id_key: str = "_id") -> Optional[str]:
    """Remove an asset from the portfolio.

    Returns the removed asset's parent_id (or None if it was a root or not found).
    """

    ref = find_asset_ref(portfolio, asset_id=asset_id, id_key=id_key)
    if not ref:
        return None
    if ref.index_in_parent < 0 or ref.index_in_parent >= len(ref.parent_list):
        return None
    ref.parent_list.pop(ref.index_in_parent)
    return ref.parent_id


def remove_asset_snapshot(portfolio: Dict[str, Any], *, asset_id: str, id_key: str = "_id") -> Optional[RemovedAsset]:
    """Remove an asset and return a snapshot that can be restored later."""

    ref = find_asset_ref(portfolio, asset_id=asset_id, id_key=id_key)
    if not ref:
        return None
    if ref.index_in_parent < 0 or ref.index_in_parent >= len(ref.parent_list):
        return None
    removed = ref.parent_list.pop(ref.index_in_parent)
    return RemovedAsset(asset=removed, parent_id=ref.parent_id, index_in_parent=ref.index_in_parent)


def restore_removed_asset(portfolio: Dict[str, Any], *, removed: RemovedAsset, id_key: str = "_id") -> bool:
    """Restore a previously removed asset into its original parent (best-effort)."""

    if not isinstance(removed, RemovedAsset):
        return False
    asset = removed.asset
    if not isinstance(asset, dict):
        return False

    if removed.parent_id:
        parent_ref = find_asset_ref(portfolio, asset_id=removed.parent_id, id_key=id_key)
        if parent_ref and isinstance(parent_ref.asset, dict):
            children = parent_ref.asset.get("assets")
            if not isinstance(children, list):
                children = []
                parent_ref.asset["assets"] = children
            idx = max(0, min(int(removed.index_in_parent), len(children)))
            children.insert(idx, asset)
            return True

    # Root restore
    roots = portfolio.get("assets")
    if not isinstance(roots, list):
        roots = []
        portfolio["assets"] = roots
    idx = max(0, min(int(removed.index_in_parent), len(roots)))
    roots.insert(idx, asset)
    return True


def add_child_asset(
    portfolio: Dict[str, Any],
    *,
    parent_id: str,
    child_asset: Dict[str, Any],
    id_key: str = "_id",
) -> bool:
    """Add child_asset under the asset with parent_id.

    Returns True if added, False if parent not found.
    """

    ref = find_asset_ref(portfolio, asset_id=parent_id, id_key=id_key)
    if not ref:
        return False

    parent_type = safe_str(ref.asset.get("type"))
    child_type = safe_str(child_asset.get("type"))
    if not can_add_child_type(parent_type=parent_type, child_type=child_type):
        return False

    children = ref.asset.get("assets")
    if not isinstance(children, list):
        children = []
        ref.asset["assets"] = children

    children.append(child_asset)
    return True


def add_sibling_asset_after(
    portfolio: Dict[str, Any],
    *,
    sibling_of_id: str,
    new_asset: Dict[str, Any],
    id_key: str = "_id",
) -> bool:
    """Insert new_asset immediately after sibling_of_id in the same parent list."""

    ref = find_asset_ref(portfolio, asset_id=sibling_of_id, id_key=id_key)
    if not ref:
        return False

    # Enforce the same parent→child constraints for sibling insertion.
    parent_type = "asset"
    if ref.parent_id:
        parent_ref = find_asset_ref(portfolio, asset_id=ref.parent_id, id_key=id_key)
        if parent_ref and isinstance(parent_ref.asset, dict):
            parent_type = safe_str(parent_ref.asset.get("type")) or parent_type
    child_type = safe_str(new_asset.get("type"))
    if not can_add_child_type(parent_type=parent_type, child_type=child_type):
        return False

    insert_at = ref.index_in_parent + 1
    if insert_at < 0:
        insert_at = 0
    if insert_at > len(ref.parent_list):
        insert_at = len(ref.parent_list)
    ref.parent_list.insert(insert_at, new_asset)
    return True


def mark_asset_retired(asset: Dict[str, Any]) -> None:
    lifecycle = asset.get("lifecycle")
    if not isinstance(lifecycle, dict):
        lifecycle = {}
        asset["lifecycle"] = lifecycle
    lifecycle["status"] = "retired"


def mark_asset_active(asset: Dict[str, Any]) -> None:
    lifecycle = asset.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return
    status = safe_str(lifecycle.get("status")).lower()
    if status == "retired":
        lifecycle["status"] = "active"
