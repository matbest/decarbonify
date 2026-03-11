from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .portfolio_index import AssetNode
from .portfolio_io import safe_str
from .recommendations import openai_client_available


def portfolio_compact_summary(nodes: List[AssetNode], max_items: int = 80) -> str:
    lines: List[str] = []
    for n in nodes[:max_items]:
        lines.append(f"- {n.path} (type={n.type})")
    if len(nodes) > max_items:
        lines.append(f"- ... ({len(nodes) - max_items} more)")
    return "\n".join(lines)


def llm_chat_answer(portfolio: Dict[str, Any], nodes: List[AssetNode], question: str) -> str:
    if not openai_client_available():
        # Simple offline fallback: highlight obvious hotspots and suggest next steps.
        q = question.lower()
        if "upgrade" in q or "first" in q or "priority" in q:
            return (
                "Start with obvious fossil-fuel systems (gas boilers, oil heating) and high-usage electricity loads (lighting). "
                "Then improve insulation/controls, and consider onsite renewables where suitable. "
                "If you share energy bills or runtime estimates, I can prioritise more accurately."
            )
        return (
            "I can help answer questions about this portfolio, but AI is currently disabled (set OPENAI_API_KEY). "
            "Ask about a specific asset or share energy/usage data for better prioritisation."
        )

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return (
            "AI recommendations are unavailable because the OpenAI client library isn't installed. "
            "Install dependencies from requirements.txt and try again."
        )

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI()

    portfolio_name = safe_str(portfolio.get("portfolio_name"))
    summary = portfolio_compact_summary(nodes)

    system = (
        "You are a helpful assistant for a property portfolio decarbonisation tool. "
        "You must base answers on the provided portfolio summary and user question. "
        "If data is missing, say what is missing and give a best-effort qualitative answer."
    )
    user = (
        f"Portfolio name: {portfolio_name}\n"
        f"Portfolio assets (paths):\n{summary}\n\n"
        f"Question: {question}\n"
    )

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()
