from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import streamlit as st


DEFAULT_PORTFOLIO_PATH = "portfolio.json"


@dataclass(frozen=True)
class AssetNode:
    node_id: str
    name: str
    type: str
    data: Dict[str, Any]
    parent_id: Optional[str]
    depth: int
    path: str


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def load_portfolio_from_bytes(raw_bytes: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    validate_portfolio(data)
    return data


def load_portfolio_from_path(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    validate_portfolio(data)
    return data


def validate_portfolio(portfolio: Dict[str, Any]) -> None:
    if not isinstance(portfolio, dict):
        raise ValueError("Portfolio must be a JSON object")
    if "portfolio_name" not in portfolio:
        raise ValueError("Portfolio must contain 'portfolio_name'")
    if "assets" not in portfolio or not isinstance(portfolio["assets"], list):
        raise ValueError("Portfolio must contain 'assets' as a list")


def iter_assets_tree(
    assets: List[Dict[str, Any]],
    *,
    parent_id: Optional[str],
    depth: int,
    parent_path: str,
    id_prefix: str,
) -> Iterable[AssetNode]:
    for idx, asset in enumerate(assets):
        if not isinstance(asset, dict):
            continue
        name = _safe_str(asset.get("name", f"Unnamed {idx}"))
        asset_type = _safe_str(asset.get("type", "asset"))
        node_id = f"{id_prefix}.{idx}"
        path = name if not parent_path else f"{parent_path} / {name}"
        yield AssetNode(
            node_id=node_id,
            name=name,
            type=asset_type,
            data=asset,
            parent_id=parent_id,
            depth=depth,
            path=path,
        )
        children = _as_list(asset.get("assets"))
        if children:
            yield from iter_assets_tree(
                children,
                parent_id=node_id,
                depth=depth + 1,
                parent_path=path,
                id_prefix=node_id,
            )


def index_portfolio(portfolio: Dict[str, Any]) -> Tuple[List[AssetNode], Dict[str, AssetNode]]:
    roots = _as_list(portfolio.get("assets"))
    nodes = list(
        iter_assets_tree(
            roots,
            parent_id=None,
            depth=0,
            parent_path=_safe_str(portfolio.get("portfolio_name", "Portfolio")),
            id_prefix="a",
        )
    )
    return nodes, {n.node_id: n for n in nodes}


def _carbon_signal(asset: Dict[str, Any]) -> str:
    asset_type = _safe_str(asset.get("type", ""))
    fuel = _safe_str(asset.get("fuel", "")).lower()

    if fuel in {"gas", "diesel", "petrol", "oil", "lpg"}:
        return "emits (combustion fuel)"
    if asset_type in {"energy_generation", "renewable_energy", "solar", "solar_panels"}:
        return "reduces (onsite generation)"
    if asset_type in {"natural_feature", "trees", "woodland", "wetlands", "soil", "grassland"}:
        return "sequesters (natural carbon)"
    if asset_type in {"lighting", "equipment", "infrastructure", "building", "room"}:
        return "consumes (likely electricity/heat)"
    if asset_type in {"energy_system", "hvac", "boiler"}:
        return "emits/consumes (heating system)"
    return "unknown"


def _heuristic_recommendations(asset: Dict[str, Any]) -> List[Dict[str, Any]]:
    asset_type = _safe_str(asset.get("type", ""))
    fuel = _safe_str(asset.get("fuel", "")).lower()
    name = _safe_str(asset.get("name", "asset"))

    recs: List[Dict[str, Any]] = []

    if fuel == "gas" or "boiler" in name.lower():
        recs.append(
            {
                "title": "Replace gas boiler with heat pump",
                "estimated_saving_tco2_per_year": 2.4,
                "explanation": "Switching from gas combustion to an efficient heat pump typically cuts operational emissions, especially with greener electricity.",
            }
        )
        recs.append(
            {
                "title": "Improve building/pipework insulation",
                "estimated_saving_tco2_per_year": 0.6,
                "explanation": "Reducing heat loss lowers heat demand regardless of heating technology.",
            }
        )

    if asset_type in {"lighting", "infrastructure"} or "light" in name.lower():
        recs.append(
            {
                "title": "Upgrade to LED + controls",
                "estimated_saving_tco2_per_year": 0.3,
                "explanation": "LEDs and occupancy/daylight controls reduce electricity consumption while maintaining lighting levels.",
            }
        )

    if asset_type in {"land", "natural_feature"}:
        recs.append(
            {
                "title": "Increase biodiversity planting",
                "estimated_saving_tco2_per_year": 0.5,
                "explanation": "Tree and hedgerow planting, soil improvements, and reduced mowing can increase sequestration over time.",
            }
        )

    if asset_type in {"building", "room"}:
        recs.append(
            {
                "title": "Add smart heating controls",
                "estimated_saving_tco2_per_year": 0.4,
                "explanation": "Better schedules, zoning, and setpoints often reduce wasted heating and improve comfort.",
            }
        )

    if asset_type in {"energy_generation", "renewable_energy"} or "solar" in name.lower():
        recs.append(
            {
                "title": "Verify inverter performance + monitoring",
                "estimated_saving_tco2_per_year": 0.1,
                "explanation": "Monitoring helps catch faults early and ensures the system delivers expected generation.",
            }
        )

    return recs[:5]


def _openai_client_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _llm_recommendations(portfolio: Dict[str, Any], asset: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not _openai_client_available():
        return _heuristic_recommendations(asset)

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return _heuristic_recommendations(asset)

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI()

    portfolio_name = _safe_str(portfolio.get("portfolio_name"))
    asset_json = json.dumps(asset, ensure_ascii=False)

    prompt = (
        "You are a decarbonisation advisor. Given a single asset within a property portfolio, "
        "suggest up to 5 practical emissions-reduction or sequestration improvements. "
        "Return ONLY valid JSON with this schema: {\"recommendations\": [ {\"title\": str, "
        "\"estimated_saving_tco2_per_year\": number, \"explanation\": str} ] }. "
        "Keep estimated_saving_tco2_per_year plausible and non-negative.\n\n"
        f"Portfolio name: {portfolio_name}\n"
        f"Asset JSON: {asset_json}\n"
    )

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You respond with strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )

    content = (resp.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(content)
        recs = parsed.get("recommendations", [])
        if isinstance(recs, list):
            cleaned: List[Dict[str, Any]] = []
            for item in recs[:5]:
                if not isinstance(item, dict):
                    continue
                cleaned.append(
                    {
                        "title": _safe_str(item.get("title")),
                        "estimated_saving_tco2_per_year": float(item.get("estimated_saving_tco2_per_year", 0) or 0),
                        "explanation": _safe_str(item.get("explanation")),
                    }
                )
            return cleaned
    except Exception:
        pass

    return _heuristic_recommendations(asset)


def _portfolio_compact_summary(nodes: List[AssetNode], max_items: int = 80) -> str:
    lines = []
    for n in nodes[:max_items]:
        lines.append(f"- {n.path} (type={n.type})")
    if len(nodes) > max_items:
        lines.append(f"- ... ({len(nodes) - max_items} more)")
    return "\n".join(lines)


def _llm_chat_answer(portfolio: Dict[str, Any], nodes: List[AssetNode], question: str) -> str:
    if not _openai_client_available():
        # Simple offline fallback: highlight obvious hotspots and suggest next steps.
        q = question.lower()
        if "most" in q and ("emit" in q or "emission" in q or "carbon" in q):
            for n in nodes:
                fuel = _safe_str(n.data.get("fuel", "")).lower()
                if fuel in {"gas", "diesel", "oil", "petrol", "lpg"} or "boiler" in n.name.lower():
                    return (
                        f"Likely highest-emitting asset: {n.path}. "
                        "Combustion heating systems tend to dominate operational emissions. "
                        "A high-impact upgrade is replacing it with a heat pump and improving insulation/controls."
                    )
            return "I can’t confidently identify the top emitter from the data provided; add fuel/energy details to assets for better answers."
        if "upgrade" in q or "first" in q or "priorit" in q:
            return (
                "Prioritise combustion heating (e.g., gas boilers), then large electricity consumers (lighting, HVAC), "
                "then add/expand onsite renewables and low-cost controls. If you add energy-use fields, I can rank these more precisely."
            )
        if "absorb" in q or "sequest" in q:
            return (
                "For land assets: increase tree/hedgerow cover where appropriate, improve soil carbon (reduced disturbance, compost/mulch), "
                "and consider wetland restoration where feasible."
            )
        return (
            "I can answer portfolio questions, but without an AI key my answers are heuristic. "
            "Try asking about a specific asset or add fuel/energy/area fields for more detail."
        )

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return "OpenAI client not installed; add 'openai' to requirements.txt."

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI()

    portfolio_name = _safe_str(portfolio.get("portfolio_name"))
    summary = _portfolio_compact_summary(nodes)

    system = (
        "You are a carbon optimisation assistant for a property portfolio. "
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


def _build_arborist_tree_data(assets: List[Dict[str, Any]], id_prefix: str) -> List[Dict[str, Any]]:
    tree: List[Dict[str, Any]] = []
    for idx, asset in enumerate(assets):
        if not isinstance(asset, dict):
            continue
        name = _safe_str(asset.get("name", f"Unnamed {idx}"))
        asset_type = _safe_str(asset.get("type", "asset"))
        node_id = f"{id_prefix}.{idx}"
        node: Dict[str, Any] = {
            "id": node_id,
            "name": f"{name} ({asset_type})",
        }
        children = _as_list(asset.get("assets"))
        if children:
            node["children"] = _build_arborist_tree_data(children, node_id)
        tree.append(node)
    return tree


def _portfolio_fingerprint(portfolio: Dict[str, Any]) -> str:
    try:
        return str(hash(json.dumps(portfolio, sort_keys=True, ensure_ascii=False)))
    except Exception:
        return str(id(portfolio))


st.set_page_config(layout="wide", initial_sidebar_state="expanded")

st.title("Portfolio Carbon Insight Tool")

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


@st.cache_data(show_spinner=False)
def _load_default_portfolio() -> Dict[str, Any]:
    if os.path.exists(DEFAULT_PORTFOLIO_PATH):
        return load_portfolio_from_path(DEFAULT_PORTFOLIO_PATH)
    # Minimal built-in fallback if the file isn't present
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
    # Reset selection when switching portfolios
    st.session_state.selected_node_id = nodes[0].node_id if nodes else ""

if "selected_node_id" not in st.session_state:
    st.session_state.selected_node_id = nodes[0].node_id if nodes else ""


with header_left:
    portfolio_name = _safe_str(portfolio.get("portfolio_name"))
    st.subheader(portfolio_name)
    if _openai_client_available():
        st.caption("AI: enabled")
    else:
        st.caption("AI: disabled (set OPENAI_API_KEY to enable)")


with st.sidebar:
    st.subheader("Asset Hierarchy")
    if not nodes:
        st.info("No assets found in this portfolio.")
    else:
        tree_data = _build_arborist_tree_data(_as_list(portfolio.get("assets")), "a")
        selected_id = st.session_state.selected_node_id or None

        try:
            from streamlit_arborist import tree_view  # type: ignore

            if "asset_tree_initialized" not in st.session_state:
                st.session_state.asset_tree_initialized = False

            # Only force the selection when we first render (or when portfolio changes).
            # Forcing it on every rerun can cause a visible "snap back" / flicker.
            selection_arg = selected_id if not st.session_state.asset_tree_initialized else None

            selected_node_data = tree_view(
                tree_data,
                selection=selection_arg,
                select_internal_nodes=True,
                open_by_default=True,
                height=600,
                key="asset_tree",
            )
            st.session_state.asset_tree_initialized = True

            # Prefer the returned value; fall back to the session_state value.
            candidate = selected_node_data
            if candidate is None:
                candidate = st.session_state.get("asset_tree")
            if isinstance(candidate, dict) and candidate.get("id") in node_by_id:
                st.session_state.selected_node_id = str(candidate["id"])
        except Exception:
            # Fallback to a simple radio list if the component isn't available.
            options = [n.node_id for n in nodes]
            labels = {n.node_id: ("   " * n.depth) + f"{n.name} ({n.type})" for n in nodes}
            selected = st.radio(
                "",
                options=options,
                format_func=lambda node_id: labels.get(node_id, node_id),
                index=options.index(st.session_state.selected_node_id)
                if st.session_state.selected_node_id in options
                else 0,
                label_visibility="collapsed",
            )
            st.session_state.selected_node_id = selected


st.subheader("Asset Detail + Recommendations")
selected_node = node_by_id.get(st.session_state.selected_node_id)
if not selected_node:
    st.info("Select an asset to view details.")
else:
    asset = selected_node.data
    st.markdown(f"**Path:** {selected_node.path}")
    st.markdown(f"**Type:** {selected_node.type}")
    st.markdown(f"**Carbon effect (qualitative):** {_carbon_signal(asset)}")

    with st.expander("Asset JSON", expanded=False):
        st.json(asset)

    st.markdown("### Recommendations")
    with st.spinner("Generating recommendations..."):
        recs = _llm_recommendations(portfolio, asset)
    if not recs:
        st.write("No recommendations for this asset.")
    else:
        for r in recs:
            title = _safe_str(r.get("title"))
            saving = r.get("estimated_saving_tco2_per_year", 0)
            expl = _safe_str(r.get("explanation"))
            st.markdown(f"- **{title}** — Estimated saving: {saving:.2f} tCO₂/year")
            if expl:
                st.caption(expl)

st.divider()

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
if question:
    st.session_state.chat_messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.write(question)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            answer = _llm_chat_answer(portfolio, nodes, question)
        st.write(answer)
    st.session_state.chat_messages.append({"role": "assistant", "content": answer})
