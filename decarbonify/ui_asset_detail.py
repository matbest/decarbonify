from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from .portfolio_index import AssetNode
from .portfolio_io import safe_str
from .recommendations import carbon_signal, llm_recommendations


def render_asset_detail_and_recommendations(*, portfolio: Dict[str, Any], selected_node: AssetNode) -> None:
    st.subheader("Asset Detail + Recommendations")

    asset = selected_node.data
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
