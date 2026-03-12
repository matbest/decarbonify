from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .portfolio_io import as_list, safe_str
from .ontology import hierarchy_category


def _asset_from_kind_string(kind: str) -> Dict[str, Any]:
    """Best-effort compatibility for older call sites.

    Accepts:
      - "core_type"
      - "core_type/subtype"
      - legacy place-ish tokens like "building" or "room" (treated as place subtypes)
    """

    s = safe_str(kind).strip()
    if not s:
        return {"core_type": "asset", "subtype": ""}
    if "/" in s:
        core, sub = s.split("/", 1)
        return {"core_type": core.strip(), "subtype": sub.strip()}
    if s.lower() in {"land", "building", "room"}:
        return {"core_type": "place", "subtype": s.strip()}
    return {"core_type": s.strip(), "subtype": ""}


def can_add_child(*, parent_asset: Dict[str, Any], child_asset: Dict[str, Any]) -> bool:
    """Return True if parent_asset can contain child_asset.

    This preserves the existing containment constraints, but is driven by
    ontology fields (core_type/subtype) instead of legacy asset['type'].
    """

    parent_cat = hierarchy_category(parent_asset)
    child_cat = hierarchy_category(child_asset)

    # Escape hatch: generic assets are always allowed.
    if safe_str(parent_asset.get("core_type")).strip().lower() == "asset":
        return True
    if safe_str(child_asset.get("core_type")).strip().lower() == "asset":
        return True

    if parent_cat == "building" and child_cat == "land":
        return False
    if parent_cat == "room" and child_cat in {"building", "land"}:
        return False
    if parent_cat == "component" and child_cat in {"room", "building", "land", "place"}:
        return False

    return True


def explain_disallowed_child_assets(*, parent_asset: Dict[str, Any], child_asset: Dict[str, Any]) -> Optional[str]:
    if can_add_child(parent_asset=parent_asset, child_asset=child_asset):
        return None
    parent_cat = hierarchy_category(parent_asset)
    child_cat = hierarchy_category(child_asset)
    if parent_cat == "building" and child_cat == "land":
        return "A building cannot contain land; add that land asset under a land parent instead."
    if parent_cat == "room" and child_cat in {"building", "land"}:
        return "A room cannot contain buildings or land; add it higher in the hierarchy (e.g. under a building or land)."
    if parent_cat == "component" and child_cat in {"room", "building", "land"}:
        return "Equipment/components cannot contain places (land/buildings/rooms)."
    if parent_cat == "component" and child_cat == "place":
        return "Equipment/components cannot contain places."
    return "That child asset isn't allowed here."


def can_add_child_type(*, parent_type: str, child_type: str) -> bool:
    """Compatibility wrapper (deprecated).

    New code should call can_add_child(parent_asset=..., child_asset=...).
    """

    return can_add_child(
        parent_asset=_asset_from_kind_string(parent_type),
        child_asset=_asset_from_kind_string(child_type),
    )


def explain_disallowed_child(*, parent_type: str, child_type: str) -> Optional[str]:
    """Compatibility wrapper (deprecated)."""

    return explain_disallowed_child_assets(
        parent_asset=_asset_from_kind_string(parent_type),
        child_asset=_asset_from_kind_string(child_type),
    )


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

    if not can_add_child(parent_asset=ref.asset, child_asset=child_asset):
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
    parent_asset: Dict[str, Any] = {"core_type": "asset", "subtype": ""}
    if ref.parent_id:
        parent_ref = find_asset_ref(portfolio, asset_id=ref.parent_id, id_key=id_key)
        if parent_ref and isinstance(parent_ref.asset, dict):
            parent_asset = parent_ref.asset
    if not can_add_child(parent_asset=parent_asset, child_asset=new_asset):
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
