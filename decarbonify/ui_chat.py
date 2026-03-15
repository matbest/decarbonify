from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from .chat import llm_chat_answer
from .portfolio_index import AssetNode


def render_chat(*, portfolio: Dict[str, Any], nodes: List[AssetNode], selected_node: AssetNode | None) -> None:
    st.subheader("Chat")

    def _scroll_container(*, height: int):
        try:
            return st.container(height=height)
        except TypeError:
            return st.container()

    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = [
            {
                "role": "assistant",
                "content": "Ask me questions about your portfolio (e.g., 'Which asset emits the most carbon?' or 'What should I upgrade first?').",
            }
        ]

    prompt = "Ask about the portfolio"
    with st.form(key="chat_send_form", clear_on_submit=True):
        question = st.text_input("", placeholder=prompt, label_visibility="collapsed")
        submitted = st.form_submit_button("Send", use_container_width=True)

    if submitted and (question or "").strip():
        question = (question or "").strip()
        st.session_state.chat_messages.append({"role": "user", "content": question})

        with st.spinner("Thinking..."):
            answer = llm_chat_answer(portfolio, nodes, question)

        st.session_state.chat_messages.append({"role": "assistant", "content": answer})

        st.rerun()

    messages_box = _scroll_container(height=560)
    with messages_box:
        for msg in st.session_state.chat_messages:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])
