from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

import streamlit as st

from .emissions import effective_emissions_tco2e_per_year
from .portfolio_index import AssetNode
from .portfolio_io import as_list, safe_str


def _truncate_one_line(text: str, *, max_chars: int = 60) -> str:
    text = safe_str(text).replace("\n", " ").replace("\r", " ").strip()
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return text[: max_chars - 1].rstrip() + "…"


def _compute_subtree_totals_tco2e(
    assets: List[Dict[str, Any]],
    *,
    overrides: Mapping[str, Any] | None = None,
    id_key: str = "_id",
) -> Dict[str, Optional[float]]:
    """Return mapping of asset_id -> subtree total tCO2e/year.

    A subtree total includes the asset itself and all descendants.
    If no values exist anywhere in the subtree, the mapped value is None.
    """

    totals: Dict[str, Optional[float]] = {}

    def walk(asset: Dict[str, Any]) -> Tuple[float, int]:
        total = 0.0
        contributing = 0

        v = effective_emissions_tco2e_per_year(asset, overrides=overrides, id_key=id_key)
        if v is not None:
            contributing += 1
            total += max(0.0, float(v))

        for child in as_list(asset.get("assets")):
            if not isinstance(child, dict):
                continue
            c_total, c_contrib = walk(child)
            total += c_total
            contributing += c_contrib

        asset_id = safe_str(asset.get(id_key))
        if asset_id:
            totals[asset_id] = total if contributing > 0 else None
        return total, contributing

    for a in assets:
        if isinstance(a, dict):
            walk(a)
    return totals


def _format_subtree_suffix(asset_id: str, *, subtree_totals: Mapping[str, Optional[float]]) -> str:
    total = subtree_totals.get(asset_id)
    if total is None:
        return " [?]"
    return f" [{float(total):.2f}]"


def _build_arborist_tree_data(
    assets: List[Dict[str, Any]],
    *,
    id_key: str = "_id",
    subtree_totals: Mapping[str, Optional[float]],
) -> List[Dict[str, Any]]:
    tree: List[Dict[str, Any]] = []
    for idx, asset in enumerate(assets):
        if not isinstance(asset, dict):
            continue
        name = safe_str(asset.get("name", f"Unnamed {idx}"))
        asset_type = safe_str(asset.get("type", "asset"))
        node_id = safe_str(asset.get(id_key))

        base_label = f"{name} ({asset_type})"
        suffix = _format_subtree_suffix(node_id, subtree_totals=subtree_totals)
        # Try to keep the numeric suffix visible by reserving space for it.
        max_chars = 55
        reserved = len(suffix)
        base_max = max(1, max_chars - reserved)
        full_label = base_label + suffix
        display_name = (_truncate_one_line(base_label, max_chars=base_max) + suffix).replace(" ", "\u00A0")

        node: Dict[str, Any] = {
            "id": node_id,
            "name": display_name,
            "title": full_label,
        }
        children = as_list(asset.get("assets"))
        if children:
            node["children"] = _build_arborist_tree_data(children, id_key=id_key, subtree_totals=subtree_totals)
        tree.append(node)
    return tree


def inject_sidebar_nowrap_css() -> None:
    st.markdown(
        """
<style>
/* Keep the sidebar hierarchy (radio fallback) on a single line */
section[data-testid="stSidebar"] .stRadio [role="radiogroup"] label,
section[data-testid="stSidebar"] .stRadio [role="radiogroup"] label p,
section[data-testid="stSidebar"] .stRadio [role="radiogroup"] label div {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
</style>
""",
        unsafe_allow_html=True,
    )


def render_asset_hierarchy_sidebar(
    *,
    portfolio: Dict[str, Any],
    nodes: List[AssetNode],
    node_by_id: Dict[str, AssetNode],
    selected_node_id: str,
    emissions_overrides: Mapping[str, Any] | None = None,
    tree_key: str = "asset_tree",
) -> Tuple[str, bool]:
    """Render the hierarchy selector.

    Returns: (selected_node_id, selection_changed)
    """

    st.subheader("Asset Hierarchy")
    if not nodes:
        st.info("No assets found in this portfolio.")
        return "", selected_node_id != ""

    roots = as_list(portfolio.get("assets"))
    subtree_totals = _compute_subtree_totals_tco2e(roots, overrides=emissions_overrides, id_key="_id")

    tree_data = _build_arborist_tree_data(roots, id_key="_id", subtree_totals=subtree_totals)
    selected_id: Optional[str] = selected_node_id or None
    selection_changed = False

    try:
        from streamlit_arborist import tree_view  # type: ignore

        if "asset_tree_initialized" not in st.session_state:
            st.session_state.asset_tree_initialized = False
        if st.session_state.get("asset_tree_last_key") != tree_key:
            st.session_state.asset_tree_initialized = False
            st.session_state.asset_tree_last_key = tree_key

        selection_arg = selected_id if not st.session_state.asset_tree_initialized else None

        selected_node_data = tree_view(
            tree_data,
            selection=selection_arg,
            select_internal_nodes=True,
            open_by_default=True,
            height=600,
            key=tree_key,
        )
        st.session_state.asset_tree_initialized = True

        candidate = selected_node_data
        if candidate is None:
            candidate = st.session_state.get(tree_key)
        if isinstance(candidate, dict) and candidate.get("id") in node_by_id:
            new_id = str(candidate["id"])
            if new_id != selected_node_id:
                selection_changed = True
            selected_node_id = new_id

        return selected_node_id, selection_changed

    except Exception:
        options = [n.node_id for n in nodes]
        labels = {
            n.node_id: ("   " * n.depth)
            + _truncate_one_line(
                f"{n.name} ({n.type})" + _format_subtree_suffix(n.node_id, subtree_totals=subtree_totals),
                max_chars=55,
            )
            for n in nodes
        }
        selected = st.radio(
            "",
            options=options,
            format_func=lambda node_id: labels.get(node_id, node_id),
            index=options.index(selected_node_id) if selected_node_id in options else 0,
            label_visibility="collapsed",
        )
        selection_changed = selected != selected_node_id
        return selected, selection_changed
