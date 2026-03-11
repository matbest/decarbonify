from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import streamlit as st

from decarbonify import auth
from decarbonify.portfolio_index import index_portfolio
from decarbonify.portfolio_io import (
    as_list,
    ensure_asset_ids,
    ensure_asset_data_fields,
    load_portfolio_from_bytes,
    load_portfolio_from_path,
    safe_str,
)
from decarbonify.portfolio_reorder import PortfolioReorderError, can_move_preorder, move_preorder
from decarbonify.recommendations import openai_client_available
from decarbonify.state_store import load_portfolio_state, save_portfolio_state
from decarbonify.ui_asset_detail import render_asset_detail_and_recommendations
from decarbonify.ui_chat import render_chat
from decarbonify.ui_sidebar import inject_sidebar_nowrap_css, render_asset_hierarchy_sidebar


DEFAULT_PORTFOLIO_PATH = "portfolio.json"


def _portfolio_storage_key(*, source: str, uploaded_name: Optional[str]) -> str:
    if source == "Upload JSON" and uploaded_name:
        return f"upload::{uploaded_name}"
    return f"path::{DEFAULT_PORTFOLIO_PATH}"


def _portfolio_fingerprint(portfolio: Dict[str, Any]) -> str:
    try:
        return str(hash(json.dumps(portfolio, sort_keys=True, ensure_ascii=False)))
    except Exception:
        return str(id(portfolio))


def _deepcopy_jsonable(value: Any) -> Any:
    # Good enough for this app's JSON-shaped portfolio.
    return json.loads(json.dumps(value, ensure_ascii=False))


@st.cache_data(show_spinner=False)
def _load_default_portfolio() -> Dict[str, Any]:
    if os.path.exists(DEFAULT_PORTFOLIO_PATH):
        return load_portfolio_from_path(DEFAULT_PORTFOLIO_PATH)

    return {
        "portfolio_name": "Example Portfolio",
        "assets": [
            {
                "name": "Heelands Site",
                "type": "land",
                "assets": [
                    {
                        "name": "Heelands Meeting Centre",
                        "type": "building",
                        "assets": [
                            {"name": "Kitchen", "type": "room"},
                            {"name": "Gas Boiler", "type": "energy_system", "fuel": "gas"},
                        ],
                    },
                    {"name": "Solar Panels", "type": "energy_generation"},
                ],
            },
            {"name": "Football Field", "type": "land", "assets": [{"name": "Floodlights", "type": "lighting"}]},
        ],
    }


st.set_page_config(layout="wide", initial_sidebar_state="expanded")
inject_sidebar_nowrap_css()

st.title("Portfolio Carbon Insight Tool")

# Login gate
user_email = auth.require_login(app_name="Decarbonify")
st.session_state.auth_user_email = user_email

google_cfg = auth.google_config()
refresh_token = auth.current_refresh_token()

header_left, header_right = st.columns([0.78, 0.22], gap="small")

with header_right:
    with st.expander("...", expanded=False):
        source = st.radio(
            "Load portfolio",
            ["Use default portfolio.json", "Upload JSON"],
            horizontal=False,
        )

        uploaded = None
        if source == "Upload JSON":
            uploaded = st.file_uploader("Portfolio JSON", type=["json"], accept_multiple_files=False)

        st.caption("Optional: set OPENAI_API_KEY for AI recommendations.")


try:
    if source == "Upload JSON" and uploaded is not None:
        loaded_portfolio = load_portfolio_from_bytes(uploaded.getvalue())
    else:
        loaded_portfolio = _load_default_portfolio()
except Exception as exc:
    st.error(str(exc))
    st.stop()

portfolio_name_loaded = safe_str(loaded_portfolio.get("portfolio_name")) or "Portfolio"
storage_key = _portfolio_storage_key(source=source, uploaded_name=(uploaded.name if uploaded is not None else None))
st.session_state.portfolio_storage_key = storage_key
st.session_state.portfolio_storage_name = portfolio_name_loaded


# Keep an editable in-memory portfolio (no persistence) so we can reorder.
if (
    "portfolio" not in st.session_state
    or st.session_state.get("portfolio_source") != source
    or (source == "Upload JSON" and uploaded is not None and st.session_state.get("uploaded_name") != uploaded.name)
):
    # Prefer a previously-saved Drive state for this user/portfolio.
    restored_portfolio, _restore_msg = load_portfolio_state(
        cfg=google_cfg,
        refresh_token=refresh_token,
        user_key=user_email,
        portfolio_key=storage_key,
        portfolio_name=portfolio_name_loaded,
    )
    if restored_portfolio is not None:
        st.session_state.portfolio = _deepcopy_jsonable(restored_portfolio)
    else:
        if _restore_msg and _restore_msg != "No saved Drive state found":
            warn_key = "drive_restore_schema_warning_shown"
            if not st.session_state.get(warn_key):
                st.session_state[warn_key] = True
                st.warning(_restore_msg)
        st.session_state.portfolio = _deepcopy_jsonable(loaded_portfolio)
    st.session_state.portfolio_source = source
    st.session_state.uploaded_name = uploaded.name if uploaded is not None else None
    st.session_state.asset_tree_initialized = False
    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1


portfolio: Dict[str, Any] = st.session_state.portfolio

# Ensure stable ids exist for all assets (idempotent).
ensure_asset_ids(portfolio, id_key="_id")
# Ensure data_fields schema exists for all assets (idempotent).
ensure_asset_data_fields(portfolio)

nodes, node_by_id = index_portfolio(portfolio)

current_fp = _portfolio_fingerprint(portfolio)
if st.session_state.get("portfolio_fp") != current_fp:
    st.session_state.portfolio_fp = current_fp
    st.session_state.asset_tree_initialized = False
    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1

if "selected_node_id" not in st.session_state:
    st.session_state.selected_node_id = nodes[0].node_id if nodes else ""
elif st.session_state.selected_node_id and st.session_state.selected_node_id not in node_by_id:
    st.session_state.selected_node_id = nodes[0].node_id if nodes else ""


with header_left:
    portfolio_name = safe_str(portfolio.get("portfolio_name"))
    st.subheader(portfolio_name)
    if openai_client_available():
        st.caption("AI: enabled")
    else:
        st.caption("AI: disabled (set OPENAI_API_KEY to enable)")


with st.sidebar:
    auth.render_logout_sidebar()

    tree_key = f"asset_tree_{int(st.session_state.get('asset_tree_nonce', 0))}"

    selected_node_id, _changed = render_asset_hierarchy_sidebar(
        portfolio=portfolio,
        nodes=nodes,
        node_by_id=node_by_id,
        selected_node_id=str(st.session_state.selected_node_id),
        tree_key=tree_key,
    )
    st.session_state.selected_node_id = selected_node_id

    if st.session_state.selected_node_id:
        can_up = can_move_preorder(portfolio, node_id=st.session_state.selected_node_id, direction=-1)
        can_down = can_move_preorder(portfolio, node_id=st.session_state.selected_node_id, direction=1)
        up_col, down_col = st.columns(2, gap="small")
        with up_col:
            if st.button("Up", use_container_width=True, disabled=not can_up):
                try:
                    move_preorder(portfolio, node_id=st.session_state.selected_node_id, direction=-1)

                    st.caption("Reorder applied locally. Click 'Save to Drive' to persist.")

                    st.session_state.asset_tree_initialized = False
                    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                    st.rerun()
                except PortfolioReorderError as exc:
                    st.error(str(exc))

        with down_col:
            if st.button("Down", use_container_width=True, disabled=not can_down):
                try:
                    move_preorder(portfolio, node_id=st.session_state.selected_node_id, direction=1)

                    st.caption("Reorder applied locally. Click 'Save to Drive' to persist.")

                    st.session_state.asset_tree_initialized = False
                    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                    st.rerun()
                except PortfolioReorderError as exc:
                    st.error(str(exc))

    # Explicit save control (in addition to auto-save on changes)
    if st.button("Save to Drive", use_container_width=True):
        ok, msg = save_portfolio_state(
            cfg=google_cfg,
            refresh_token=refresh_token,
            user_key=user_email,
            portfolio_key=st.session_state.get("portfolio_storage_key", storage_key),
            portfolio_name=safe_str(st.session_state.get("portfolio_storage_name", portfolio_name_loaded)),
            portfolio=portfolio,
        )
        if ok:
            st.success(msg)
        else:
            st.warning("Not saved to Drive: " + msg)


selected_node = node_by_id.get(st.session_state.selected_node_id)
if not selected_node:
    st.subheader("Asset Detail + Recommendations")
    st.info("Select an asset to view details.")
else:
    render_asset_detail_and_recommendations(portfolio=portfolio, selected_node=selected_node)

st.divider()

render_chat(portfolio=portfolio, nodes=nodes)
