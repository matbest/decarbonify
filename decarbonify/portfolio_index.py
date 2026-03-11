from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .portfolio_io import as_list, safe_str


@dataclass(frozen=True)
class AssetNode:
    node_id: str
    name: str
    type: str
    data: Dict[str, Any]
    parent_id: Optional[str]
    depth: int
    path: str


def iter_assets_tree(
    assets: List[Dict[str, Any]],
    *,
    parent_id: Optional[str],
    depth: int,
    parent_path: str,
    id_key: str = "_id",
) -> Iterable[AssetNode]:
    for idx, asset in enumerate(assets):
        if not isinstance(asset, dict):
            continue
        name = safe_str(asset.get("name", f"Unnamed {idx}"))
        asset_type = safe_str(asset.get("type", "asset"))
        node_id = safe_str(asset.get(id_key))
        path = name if not parent_path else f"{parent_path} / {name}"
        yield AssetNode(
            node_id=node_id,
            name=name,
            type=asset_type,
            data=asset,
            parent_id=parent_id,
            depth=depth,
            path=path,
        )
        children = as_list(asset.get("assets"))
        if children:
            yield from iter_assets_tree(
                children,
                parent_id=node_id,
                depth=depth + 1,
                parent_path=path,
                id_key=id_key,
            )


def index_portfolio(portfolio: Dict[str, Any]) -> Tuple[List[AssetNode], Dict[str, AssetNode]]:
    roots = as_list(portfolio.get("assets"))
    nodes = list(
        iter_assets_tree(
            roots,
            parent_id=None,
            depth=0,
            parent_path=safe_str(portfolio.get("portfolio_name", "Portfolio")),
            id_key="_id",
        )
    )
    return nodes, {n.node_id: n for n in nodes}
