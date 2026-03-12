from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from .chat import llm_chat_answer, llm_edit_selected_subtree
from .portfolio_index import AssetNode


def render_chat(*, portfolio: Dict[str, Any], nodes: List[AssetNode], selected_node: AssetNode | None) -> None:
    st.subheader("Chat")

    def _scroll_container(*, height: int):
        try:
            return st.container(height=height)
        except TypeError:
            return st.container()

    mode = st.radio(
        "Mode",
        ["Ask", "Edit selected subtree"],
        horizontal=False,
        label_visibility="collapsed",
        key="chat_mode",
    )

    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = [
            {
                "role": "assistant",
                "content": "Ask me questions about your portfolio (e.g., 'Which asset emits the most carbon?' or 'What should I upgrade first?').",
            }
        ]

    if mode == "Edit selected subtree":
        if selected_node is None:
            st.info("Select an asset first to enable edit mode.")
            return
        st.caption(f"Editable scope: {selected_node.path}")

    prompt = "Ask about the portfolio" if mode == "Ask" else "Tell me what to add under the selected asset"
    with st.form(key="chat_send_form", clear_on_submit=True):
        question = st.text_input("", placeholder=prompt, label_visibility="collapsed")
        submitted = st.form_submit_button("Send", use_container_width=True)

    if submitted and (question or "").strip():
        question = (question or "").strip()
        st.session_state.chat_messages.append({"role": "user", "content": question})

        with st.spinner("Thinking..."):
            if mode == "Ask" or selected_node is None:
                answer = llm_chat_answer(portfolio, nodes, question)
                applied = False
            else:
                answer, applied, added_asset_id = llm_edit_selected_subtree(
                    portfolio=portfolio,
                    selected_node=selected_node,
                    user_message=question,
                )

        st.session_state.chat_messages.append({"role": "assistant", "content": answer})

        if mode == "Edit selected subtree" and selected_node is not None and applied:
            if added_asset_id:
                st.session_state.selected_node_id = str(added_asset_id)
            st.session_state.asset_tree_initialized = False
            st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
            st.rerun()

        st.rerun()

    messages_box = _scroll_container(height=560)
    with messages_box:
        for msg in st.session_state.chat_messages:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])
