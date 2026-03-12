from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .portfolio_io import as_list, safe_str
from .ontology import display_kind


class PortfolioReorderError(ValueError):
    pass


@dataclass
class PreorderRef:
    node_id: str
    parent_id: Optional[str]
    depth: int
    asset: Dict[str, Any]
    parent_list: List[Dict[str, Any]]
    index_in_parent: int


def iter_preorder_refs(
    assets: List[Dict[str, Any]],
    *,
    parent_id: Optional[str],
    depth: int,
    id_key: str = "_id",
) -> Iterable[PreorderRef]:
    for idx, asset in enumerate(assets):
        if not isinstance(asset, dict):
            continue
        node_id = safe_str(asset.get(id_key))
        yield PreorderRef(
            node_id=node_id,
            parent_id=parent_id,
            depth=depth,
            asset=asset,
            parent_list=assets,
            index_in_parent=idx,
        )
        children = as_list(asset.get("assets"))
        if children:
            yield from iter_preorder_refs(children, parent_id=node_id, depth=depth + 1, id_key=id_key)


def build_preorder_index(portfolio: Dict[str, Any]) -> List[PreorderRef]:
    roots = as_list(portfolio.get("assets"))
    return list(iter_preorder_refs(roots, parent_id=None, depth=0, id_key="_id"))


def _subtree_end_index(pre: List[PreorderRef], start_idx: int) -> int:
    start_depth = pre[start_idx].depth
    end = start_idx
    for j in range(start_idx + 1, len(pre)):
        if pre[j].depth > start_depth:
            end = j
            continue
        break
    return end


def move_preorder(
    portfolio: Dict[str, Any],
    *,
    node_id: str,
    direction: int,
) -> Tuple[Dict[str, Any], str]:
    """Move a node up/down in preorder order.

    - direction = -1: move up (swap with previous visible node)
    - direction = +1: move down (swap with next node *after this node's subtree*)

    Returns: (moved_asset_dict, operation)
    operation is a short string for UI/debug.
    """

    if direction not in (-1, 1):
        raise PortfolioReorderError("direction must be -1 or 1")

    pre = build_preorder_index(portfolio)
    pos_by_id = {r.node_id: i for i, r in enumerate(pre)}
    if node_id not in pos_by_id:
        raise PortfolioReorderError("Selected node not found")

    i = pos_by_id[node_id]
    ref = pre[i]

    if direction == -1:
        if i == 0:
            return ref.asset, "noop"
        neighbor = pre[i - 1]
        # Insert before neighbor in its parent list.
        insert_list = neighbor.parent_list
        insert_at = neighbor.index_in_parent

        # Remove from old parent list.
        old_list = ref.parent_list
        old_idx = ref.index_in_parent
        moved = old_list.pop(old_idx)

        if old_list is insert_list and old_idx < insert_at:
            insert_at -= 1

        insert_list.insert(insert_at, moved)
        return moved, "moved_up"

    # direction == +1
    end = _subtree_end_index(pre, i)
    if end >= len(pre) - 1:
        return ref.asset, "noop"

    neighbor = pre[end + 1]
    insert_list = neighbor.parent_list
    insert_at = neighbor.index_in_parent + 1

    old_list = ref.parent_list
    old_idx = ref.index_in_parent
    moved = old_list.pop(old_idx)

    if old_list is insert_list and old_idx < insert_at:
        insert_at -= 1

    if insert_at < 0:
        insert_at = 0
    if insert_at > len(insert_list):
        insert_at = len(insert_list)

    insert_list.insert(insert_at, moved)
    return moved, "moved_down"


def can_move_preorder(portfolio: Dict[str, Any], *, node_id: str, direction: int) -> bool:
    """Return True if a preorder move would change anything."""

    if direction not in (-1, 1):
        return False
    pre = build_preorder_index(portfolio)
    pos_by_id = {r.node_id: i for i, r in enumerate(pre)}
    if node_id not in pos_by_id:
        return False
    i = pos_by_id[node_id]
    if direction == -1:
        return i > 0
    end = _subtree_end_index(pre, i)
    return end < len(pre) - 1


def describe_node(ref: PreorderRef) -> str:
    name = safe_str(ref.asset.get("name"))
    kind = display_kind(ref.asset)
    return f"{name} ({kind})".strip()
