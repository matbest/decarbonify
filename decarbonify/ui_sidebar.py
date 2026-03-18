from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

from functools import lru_cache
import time

import streamlit as st

from .asset_types import list_asset_type_summaries, load_asset_type
from .emissions import is_retired
from .ontology import display_kind, hierarchy_category, normalize_core_type, normalize_energy_role
from .portfolio_edit import find_asset_ref, move_asset
from .portfolio_index import AssetNode
from .portfolio_io import as_list, safe_str
from .recommendations import (
    extract_recommendation_items,
    heuristic_recommendations,
    normalize_recommendations_for_display,
    recommendation_id,
)


@lru_cache(maxsize=1)
def _asset_type_label_by_id() -> Dict[str, str]:
    try:
        return {s.id: safe_str(s.label) for s in list_asset_type_summaries()}
    except Exception:
        return {}


def _kind_label(asset: Dict[str, Any]) -> str:
    """Human-friendly kind label for display in the tree.

    Prefer the template label (via asset_type_id) so specialized room templates
    like Kitchen/Hall don't collapse into the generic subtype 'room'.
    """

    subtype = safe_str(asset.get("subtype")).strip()
    type_id = safe_str(asset.get("asset_type_id")).strip()
    if not type_id:
        return subtype

    label = safe_str(_asset_type_label_by_id().get(type_id)).strip()
    if not label:
        try:
            td = load_asset_type(type_id)
            if isinstance(td, dict):
                label = safe_str(td.get("label")).strip()
        except Exception:
            label = ""

    # Shorten "Kitchen (room)" -> "Kitchen" for the suffix.
    if "(" in label:
        head = label.split("(", 1)[0].strip()
        if head:
            return head

    return label or subtype


def _truncate_one_line(text: str, *, max_chars: int = 60) -> str:
    text = safe_str(text).replace("\n", " ").replace("\r", " ").strip()
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return text[: max_chars - 1].rstrip() + "…"


def _strike(text: str) -> str:
    """Best-effort strike-through using Unicode combining overlay.

    Works in plain-text widgets (Arborist node names, radio fallback).
    """

    s = safe_str(text)
    if not s:
        return s

    # If the string begins with an icon prefix (e.g. "⚡ "), don't strike the icon.
    prefix = ""
    rest = s
    first_space = s.find(" ")
    if 0 < first_space <= 6:
        # Heuristic: treat the first token as an icon prefix.
        token = s[:first_space]
        if any(ord(ch) > 127 for ch in token):
            prefix = s[: first_space + 1]
            rest = s[first_space + 1 :]

    overlay = "\u0336"
    out = []
    for ch in rest:
        if ch.isspace():
            out.append(ch)
        else:
            out.append(ch + overlay)
    return prefix + "".join(out)


def _italic(text: str) -> str:
    """Best-effort italic using Unicode Mathematical Italic letters.

    Streamlit widgets used in the sidebar (Arborist node names, radio fallback)
    don't render markdown/HTML italics, so we transform ASCII letters instead.
    """

    s = safe_str(text)
    if not s:
        return s

    # Preserve an icon prefix like "⚡ ".
    prefix = ""
    rest = s
    first_space = s.find(" ")
    if 0 < first_space <= 6:
        token = s[:first_space]
        if any(ord(ch) > 127 for ch in token):
            prefix = s[: first_space + 1]
            rest = s[first_space + 1 :]

    def italic_char(ch: str) -> str:
        o = ord(ch)
        # A-Z => U+1D434..U+1D44D
        if 65 <= o <= 90:
            return chr(0x1D434 + (o - 65))
        # a-z => U+1D44E..U+1D467
        if 97 <= o <= 122:
            return chr(0x1D44E + (o - 97))
        return ch

    return prefix + "".join(italic_char(ch) for ch in rest)


def _fallback_core_type_icon(asset: Dict[str, Any]) -> str:
    ct = normalize_core_type(safe_str(asset.get("core_type")) or "asset")
    stype = safe_str(asset.get("subtype")).strip().lower().replace(" ", "_")

    # Special-case place subtypes for clearer hierarchy scanning.
    if ct == "place":
        if stype in {"land", "grassland", "woodland"} or stype.endswith("_land"):
            return "🟩"
        if hierarchy_category(asset) == "room":
            return "⬜"
        if stype in {"site", "farm", "campus"}:
            return "🗺️"
        if stype in {"building", "warehouse"}:
            return "🏢"
    return {
        "place": "📍",
        "activity": "📝",
        "asset": "📦",
        "energy_system": "⚡",
        "resource": "🧪",
        "surface": "🧱",
    }.get(ct, "📦")


def _infer_energy_carrier(asset: Dict[str, Any]) -> str:
    """Best-effort inference for the energy carrier/source/output.

    Returns one of: electricity | gas | heat | oil | ""
    """

    attrs = asset.get("attributes")
    parts: List[str] = []
    if isinstance(attrs, dict):
        for k in ("energy_type", "fuel", "carrier", "emissions_type", "source", "sink", "technology"):
            v = attrs.get(k)
            if v is not None:
                parts.append(safe_str(v))

    for k in ("asset_type_id", "subtype", "name", "description"):
        v = asset.get(k)
        if v is not None:
            parts.append(safe_str(v))

    s = " ".join(p for p in parts if safe_str(p).strip()).strip().lower().replace("-", "_")

    if any(tok in s for tok in ["electricity", "electric", "grid", "solar_pv", "pv", "kwh"]):
        return "electricity"
    if "gas" in s:
        return "gas"
    if any(tok in s for tok in ["heating_oil", "oil", "diesel", "petrol", "kerosene"]):
        return "oil"
    if any(tok in s for tok in ["heat", "thermal", "hot_water", "hot water", "solar_thermal", "solar water"]):
        return "heat"
    return ""


def _node_icon(asset: Dict[str, Any]) -> str:
    """Pick an icon for the node.

    Spec:
      - consumer of electricity -> plug
      - consumer of gas -> oil barrel
      - producer of electricity -> lightning bolt
      - producer of heat -> hot springs
    """

    type_id = safe_str(asset.get("asset_type_id")).strip().lower()
    if type_id:
        # Template-driven overrides for clearer hierarchy scanning.
        # Land assets: always render as a green square.
        if type_id.startswith("land_"):
            return "🟩"

        # Vehicles: prefer the specific vehicle emoji over the generic energy icons.
        vehicle_icons = {
            "vehicle_tractor": "🚜",
            "vehicle_car": "🚗",
            "vehicle_van": "🚐",
            "vehicle_lorry": "🚚",
        }
        icon = vehicle_icons.get(type_id)
        if icon:
            return icon

    role = normalize_energy_role(safe_str(asset.get("current_role")))
    carrier = _infer_energy_carrier(asset)

    if role == "consumer":
        if carrier == "electricity":
            return "🔌"
        if carrier in {"gas", "oil"}:
            return "🛢️"

    if role == "producer":
        if carrier == "electricity":
            return "⚡"
        if carrier == "heat":
            return "♨️"

    return _fallback_core_type_icon(asset)

def _asset_savings_tco2_per_year(asset: Dict[str, Any]) -> Tuple[float, float]:
    """Return (done, possible) savings for a single asset.

    - done: sum of savings for recommendations with done=True
    - possible: sum of savings for currently cached recommendations
    """

    done = 0.0
    possible = 0.0

    status = asset.get("recommendation_status")
    status_map = status if isinstance(status, dict) else {}

    # Match the detail view: prefer stored bundle/legacy recs if present.
    recs = extract_recommendation_items(asset)
    if not recs:
        recs = heuristic_recommendations(asset)

    # Ground savings to current baseline so rollups update after input edits.
    recs = normalize_recommendations_for_display(asset, recs)
    for r in recs:
        rid = recommendation_id(r)
        st0 = status_map.get(rid)
        ignored = bool(st0.get("ignored")) if isinstance(st0, dict) else False
        if ignored:
            continue

        try:
            saving = float(r.get("estimated_saving_tco2_per_year", 0) or 0)
        except Exception:
            saving = 0.0
        possible += saving

        if isinstance(st0, dict) and bool(st0.get("done")):
            done += saving

    return float(done), float(possible)


def _compute_subtree_savings(
    assets: List[Dict[str, Any]],
    *,
    id_key: str = "_id",

) -> Dict[str, Tuple[float, float]]:
    """Return mapping of asset_id -> (done, possible) savings totals for the subtree."""

    totals: Dict[str, Tuple[float, float]] = {}

    def walk(asset: Dict[str, Any]) -> Tuple[float, float]:
        done, possible = _asset_savings_tco2_per_year(asset)

        for child in as_list(asset.get("assets")):
            if not isinstance(child, dict):
                continue
            c_done, c_possible = walk(child)
            done += c_done
            possible += c_possible

        asset_id = safe_str(asset.get(id_key))
        if asset_id:
            totals[asset_id] = (float(done), float(possible))
        return float(done), float(possible)

    for a in assets:
        if isinstance(a, dict):
            walk(a)
    return totals


def _format_subtree_suffix(asset_id: str, *, subtree_totals: Mapping[str, Tuple[float, float]]) -> str:
    done, possible = subtree_totals.get(asset_id, (0.0, 0.0))
    return f" [{float(done):.2f}/{float(possible):.2f}]"


def _build_arborist_tree_data(
    assets: List[Dict[str, Any]],
    *,
    id_key: str = "_id",
    subtree_totals: Mapping[str, Tuple[float, float]],
) -> List[Dict[str, Any]]:
    tree: List[Dict[str, Any]] = []
    for idx, asset in enumerate(assets):
        if not isinstance(asset, dict):
            continue
        name = safe_str(asset.get("name", f"Unnamed {idx}"))
        icon = _node_icon(asset)
        kind = _kind_label(asset)
        node_id = safe_str(asset.get(id_key))

        base_label = f"{icon} {name}" + (f" ({kind})" if kind else "")
        if not safe_str(asset.get("asset_type_id")).strip():
            base_label = _italic(base_label)
        if is_retired(asset):
            base_label = _strike(base_label)
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


def _build_arborist_component_tree_data(
    assets: List[Dict[str, Any]],
    *,
    id_key: str = "_id",
    subtree_totals: Mapping[str, Tuple[float, float]],
) -> List[Dict[str, Any]]:
    """Build node data for the local React Arborist Streamlit Component.

    Unlike `_build_arborist_tree_data`, this avoids Streamlit markdown styling and
    instead provides structured fields for rendering/rename/DnD.
    """

    tree: List[Dict[str, Any]] = []
    for idx, asset in enumerate(assets):
        if not isinstance(asset, dict):
            continue
        node_id = safe_str(asset.get(id_key))
        if not node_id:
            # Skip nodes without stable ids; they can't be safely selected/moved/renamed.
            continue

        name = safe_str(asset.get("name", f"Unnamed {idx}"))
        icon = _node_icon(asset)
        kind = _kind_label(asset)
        suffix = _format_subtree_suffix(node_id, subtree_totals=subtree_totals)

        node: Dict[str, Any] = {
            "id": node_id,
            "name": name,
            "icon": icon,
            "kind": kind,
            "suffix": suffix,
            "hasType": bool(safe_str(asset.get("asset_type_id")).strip()),
            "retired": bool(is_retired(asset)),
            "cat": hierarchy_category(asset),
        }

        children = as_list(asset.get("assets"))
        if children:
            node["children"] = _build_arborist_component_tree_data(
                children,
                id_key=id_key,
                subtree_totals=subtree_totals,
            )
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
    portfolio_fp: str = "",
    tree_key: str = "asset_tree",
) -> Tuple[str, bool]:
    """Render the hierarchy selector.

    Returns: (selected_node_id, selection_changed)
    """

    if not nodes:
        st.info("No assets found in this portfolio.")
        return "", selected_node_id != ""

    roots = as_list(portfolio.get("assets"))

    # Computing subtree totals can be expensive on large portfolios (walks all nodes and
    # evaluates heuristic recommendations). Cache it across reruns unless the portfolio changed.
    cache_fp_key = "sb_subtree_totals_fp"
    cache_val_key = "sb_subtree_totals"
    cached_fp = safe_str(st.session_state.get(cache_fp_key))
    cached_totals = st.session_state.get(cache_val_key)
    if portfolio_fp and cached_fp == portfolio_fp and isinstance(cached_totals, dict):
        subtree_totals = cached_totals
    else:
        subtree_totals = _compute_subtree_savings(roots, id_key="_id")
        if portfolio_fp:
            st.session_state[cache_fp_key] = portfolio_fp
            st.session_state[cache_val_key] = subtree_totals

    tree_data = _build_arborist_tree_data(roots, id_key="_id", subtree_totals=subtree_totals)
    selected_id: Optional[str] = selected_node_id or None
    selection_changed = False

    # Prefer the local React Arborist Streamlit Component (supports DnD + inline rename).
    try:
        from .arborist_component import arborist_tree, arborist_tree_available

        if arborist_tree_available():
            comp_tree_data = _build_arborist_component_tree_data(roots, id_key="_id", subtree_totals=subtree_totals)

            state = arborist_tree(
                data=comp_tree_data,
                selection=selected_id,
                height=600,
                key=tree_key,
            )
            if not isinstance(state, dict):
                state = {}

            # ── Selection: read the component's current selectedId (state, not event) ──
            comp_selected = safe_str(state.get("selectedId"))

            if comp_selected and comp_selected in node_by_id:
                prev_sid = safe_str(st.session_state.get("selected_node_id"))
                if comp_selected != prev_sid:
                    st.session_state.selected_node_id = comp_selected

            # ── Actions: process rename/move via lastAction with dedup ──
            last_action_id = int(state.get("lastActionId", 0) or 0)
            last_action = state.get("lastAction")
            action_id_key = f"{tree_key}::last_action_id"
            prev_action_id = int(st.session_state.get(action_id_key, 0) or 0)

            if last_action_id and last_action_id > prev_action_id and isinstance(last_action, dict):
                st.session_state[action_id_key] = last_action_id
                action_type = safe_str(last_action.get("type"))


                if action_type == "rename":
                    cid = safe_str(last_action.get("id"))
                    new_name = safe_str(last_action.get("name"))
                    if cid and new_name.strip():
                        ref = find_asset_ref(portfolio, asset_id=cid)
                        if not ref:
                            st.warning("Could not rename: asset not found.")
                        else:
                            ref.asset["name"] = new_name.strip()
                            st.session_state.selected_node_id = cid
                            st.session_state.asset_tree_initialized = False
                            st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                            st.rerun()

                elif action_type == "move":
                    drag_ids = last_action.get("dragIds")
                    parent_id = safe_str(last_action.get("parentId"))
                    idx = int(last_action.get("index", 0) or 0)
                    if isinstance(drag_ids, list) and drag_ids:
                        moved_id = safe_str(drag_ids[0])
                        ok, msg = move_asset(
                            portfolio,
                            asset_id=moved_id,
                            new_parent_id=parent_id or None,
                            index=idx,
                        )
                        if not ok:
                            st.warning(msg)
                        else:
                            st.session_state.selected_node_id = moved_id
                            st.session_state.asset_tree_initialized = False
                            st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                            st.rerun()

            new_id = safe_str(st.session_state.get("selected_node_id"))
            if new_id and new_id in node_by_id:
                if new_id != selected_node_id:
                    selection_changed = True
                selected_node_id = new_id



            # ── Persistent error console ──
            errors = state.get("errors")
            if isinstance(errors, list) and errors:
                if "hierarchy_error_log" not in st.session_state:
                    st.session_state["hierarchy_error_log"] = []
                # Only append new errors
                prev_log = st.session_state["hierarchy_error_log"]
                new_errors = [e for e in errors if e not in prev_log]
                if new_errors:
                    st.session_state["hierarchy_error_log"] = prev_log + new_errors
                with st.expander("Hierarchy Console", expanded=True):
                    for err in st.session_state["hierarchy_error_log"][-20:]:
                        st.write(f"❌ {err}")
                    if st.button("Clear console", key="clear_hierarchy_console", use_container_width=True):
                        st.session_state["hierarchy_error_log"] = []
                        st.experimental_rerun()
            return selected_node_id, selection_changed
    except Exception:
        pass

    def _sync_arborist_selection() -> None:
        candidate = st.session_state.get(tree_key)
        if isinstance(candidate, dict):
            cid = safe_str(candidate.get("id"))
            if cid and cid in node_by_id:
                st.session_state.selected_node_id = cid
        elif isinstance(candidate, str):
            cid = safe_str(candidate)
            if cid and cid in node_by_id:
                st.session_state.selected_node_id = cid

    try:
        from streamlit_arborist import tree_view  # type: ignore

        selected_node_data = tree_view(
            tree_data,
            selection=selected_id,
            select_internal_nodes=True,
            open_by_default=True,
            height=600,
            key=tree_key,
            on_change=_sync_arborist_selection,
        )

        if isinstance(selected_node_data, dict):
            cid = safe_str(selected_node_data.get("id"))
            if cid and cid in node_by_id:
                st.session_state.selected_node_id = cid
        elif isinstance(selected_node_data, str):
            cid = safe_str(selected_node_data)
            if cid and cid in node_by_id:
                st.session_state.selected_node_id = cid

        _sync_arborist_selection()

        new_id = safe_str(st.session_state.get("selected_node_id"))
        if new_id and new_id in node_by_id:
            if new_id != selected_node_id:
                selection_changed = True
            selected_node_id = new_id

        return selected_node_id, selection_changed

    except Exception:
        options = [n.node_id for n in nodes]
        labels = {
            n.node_id: ("   " * n.depth)
            + _truncate_one_line(
                (
                    (
                        _strike(
                            _italic(
                                f"{_node_icon(n.data)} {n.name}"
                                + (f" ({_kind_label(n.data)})" if _kind_label(n.data) else "")
                            )
                            if not safe_str(n.data.get("asset_type_id")).strip()
                            else (
                                f"{_node_icon(n.data)} {n.name}"
                                + (f" ({_kind_label(n.data)})" if _kind_label(n.data) else "")
                            )
                        )
                        if is_retired(n.data)
                        else (
                            _italic(
                                f"{_node_icon(n.data)} {n.name}"
                                + (f" ({_kind_label(n.data)})" if _kind_label(n.data) else "")
                            )
                            if not safe_str(n.data.get("asset_type_id")).strip()
                            else (
                                f"{_node_icon(n.data)} {n.name}"
                                + (f" ({_kind_label(n.data)})" if _kind_label(n.data) else "")
                            )
                        )
                    )
                )
                + _format_subtree_suffix(n.node_id, subtree_totals=subtree_totals),
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
