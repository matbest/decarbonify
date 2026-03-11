from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from .portfolio_index import AssetNode
from .portfolio_io import safe_str
from . import auth
from .state_store import save_portfolio_state
from .emissions import (
    EMISSIONS_FIELD,
    USER_OVERRIDE_FIELD,
    emissions_field_help_text,
    extract_emissions_tco2e_per_year,
    sum_emissions_produced_tco2e_per_year,
)
from .recommendations import carbon_signal, llm_recommendations


def render_asset_detail_and_recommendations(*, portfolio: Dict[str, Any], selected_node: AssetNode) -> None:
    st.subheader("Asset Detail + Recommendations")

    asset = selected_node.data

    # Per-asset override input (stored separately from portfolio JSON).
    with st.expander("Emissions override", expanded=False):
        asset_id = safe_str(asset.get("_id"))
        base = extract_emissions_tco2e_per_year(asset)
        current_override = asset.get(USER_OVERRIDE_FIELD)
        st.caption(
            f"Base field: {EMISSIONS_FIELD}="
            + (f"{float(base):.2f} tCO₂e/yr" if base is not None else "(missing)")
        )

        with st.form(key=f"emissions_override_form_{selected_node.node_id}"):
            raw = st.text_input(
                "Override (tCO₂e/year)",
                value="" if current_override is None else str(current_override),
                placeholder="e.g. 1.25 (leave blank to clear)",
            )
            submitted = st.form_submit_button("Apply override", use_container_width=True)
            if submitted:
                s = (raw or "").strip()
                if not asset_id:
                    st.error("This asset is missing an internal id; cannot store override.")
                elif s == "":
                    if USER_OVERRIDE_FIELD in asset:
                        del asset[USER_OVERRIDE_FIELD]
                    user_key = safe_str(st.session_state.get("auth_user_email")) or "anonymous"
                    ok, msg = save_portfolio_state(
                        cfg=auth.google_config(),
                        refresh_token=auth.current_refresh_token(),
                        user_key=user_key,
                        portfolio_key=safe_str(st.session_state.get("portfolio_storage_key")),
                        portfolio_name=safe_str(st.session_state.get("portfolio_storage_name")),
                        portfolio=st.session_state.get("portfolio") or {},
                        emissions_overrides={},
                    )
                    st.session_state.asset_tree_initialized = False
                    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                    if ok:
                        st.success("Override cleared. " + msg)
                    else:
                        st.warning("Override cleared, but not saved to Drive: " + msg)
                    st.rerun()
                else:
                    try:
                        v = float(s)
                    except Exception:
                        st.error("Please enter a number (e.g. 1.25) or leave blank to clear.")
                    else:
                        asset[USER_OVERRIDE_FIELD] = v
                        user_key = safe_str(st.session_state.get("auth_user_email")) or "anonymous"
                        ok, msg = save_portfolio_state(
                            cfg=auth.google_config(),
                            refresh_token=auth.current_refresh_token(),
                            user_key=user_key,
                            portfolio_key=safe_str(st.session_state.get("portfolio_storage_key")),
                            portfolio_name=safe_str(st.session_state.get("portfolio_storage_name")),
                            portfolio=st.session_state.get("portfolio") or {},
                            emissions_overrides={},
                        )
                        st.session_state.asset_tree_initialized = False
                        st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                        if ok:
                            st.success("Override saved. " + msg)
                        else:
                            st.warning("Override saved locally for this session, but not saved to Drive: " + msg)
                        st.rerun()

    total_tco2e, contributing, visited, overrides_used = sum_emissions_produced_tco2e_per_year(asset)
    if contributing > 0:
        st.metric(
            "Estimated CO₂e produced (t/year)",
            f"{total_tco2e:.2f}",
            help=(
                f"Sum of selected asset + {visited - 1} descendants. "
                f"Values present for {contributing} of {visited} assets; overrides used for {overrides_used} assets."
            ),
        )
    else:
        st.info(emissions_field_help_text())

    st.markdown(f"**Path:** {selected_node.path}")
    st.markdown(f"**Type:** {selected_node.type}")
    st.markdown(f"**Carbon effect (qualitative):** {carbon_signal(asset)}")

    with st.expander("Asset JSON", expanded=False):
        st.json(asset)

    st.markdown("### Recommendations")
    with st.spinner("Generating recommendations..."):
        recs: List[Dict[str, Any]] = llm_recommendations(portfolio, asset)

    if not recs:
        st.write("No recommendations for this asset.")
        return

    for r in recs:
        title = safe_str(r.get("title"))
        saving = r.get("estimated_saving_tco2_per_year", 0)
        expl = safe_str(r.get("explanation"))
        st.markdown(f"- **{title}** — Estimated saving: {saving:.2f} tCO₂/year")
        if expl:
            st.caption(expl)
