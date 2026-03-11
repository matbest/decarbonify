from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .portfolio_io import as_list, safe_str


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
