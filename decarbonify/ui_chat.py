from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from .chat import llm_chat_answer
from .portfolio_index import AssetNode


def render_chat(*, portfolio: Dict[str, Any], nodes: List[AssetNode]) -> None:
    st.subheader("Chat with AI Assistant")

    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = [
            {
                "role": "assistant",
                "content": "Ask me questions about your portfolio (e.g., 'Which asset emits the most carbon?' or 'What should I upgrade first?').",
            }
        ]

    for msg in st.session_state.chat_messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    question = st.chat_input("Ask about the portfolio")
    if not question:
        return

    st.session_state.chat_messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            answer = llm_chat_answer(portfolio, nodes, question)
        st.write(answer)

    st.session_state.chat_messages.append({"role": "assistant", "content": answer})
