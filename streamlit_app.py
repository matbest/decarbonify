from __future__ import annotations

import json
import os
from typing import Any, Dict

import streamlit as st

from decarbonify import auth
from decarbonify.portfolio_index import index_portfolio
from decarbonify.portfolio_io import as_list, load_portfolio_from_bytes, load_portfolio_from_path, safe_str
from decarbonify.recommendations import openai_client_available
from decarbonify.ui_asset_detail import render_asset_detail_and_recommendations
from decarbonify.ui_chat import render_chat
from decarbonify.ui_sidebar import inject_sidebar_nowrap_css, render_asset_hierarchy_sidebar


DEFAULT_PORTFOLIO_PATH = "portfolio.json"


def _portfolio_fingerprint(portfolio: Dict[str, Any]) -> str:
    try:
        return str(hash(json.dumps(portfolio, sort_keys=True, ensure_ascii=False)))
    except Exception:
        return str(id(portfolio))


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
auth.require_login(app_name="Decarbonify")

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
        portfolio = load_portfolio_from_bytes(uploaded.getvalue())
    else:
        portfolio = _load_default_portfolio()
except Exception as exc:
    st.error(str(exc))
    st.stop()

nodes, node_by_id = index_portfolio(portfolio)

current_fp = _portfolio_fingerprint(portfolio)
if st.session_state.get("portfolio_fp") != current_fp:
    st.session_state.portfolio_fp = current_fp
    st.session_state.asset_tree_initialized = False
    st.session_state.selected_node_id = nodes[0].node_id if nodes else ""

if "selected_node_id" not in st.session_state:
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

    selected_node_id, _changed = render_asset_hierarchy_sidebar(
        portfolio=portfolio,
        nodes=nodes,
        node_by_id=node_by_id,
        selected_node_id=str(st.session_state.selected_node_id),
    )
    st.session_state.selected_node_id = selected_node_id


selected_node = node_by_id.get(st.session_state.selected_node_id)
if not selected_node:
    st.subheader("Asset Detail + Recommendations")
    st.info("Select an asset to view details.")
else:
    render_asset_detail_and_recommendations(portfolio=portfolio, selected_node=selected_node)

st.divider()

render_chat(portfolio=portfolio, nodes=nodes)
