from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st

from .chat import llm_chat_answer, llm_edit_selected_subtree
from .intake import (
    apply_intake_ops,
    llm_draft_intake_ops,
    merge_assets,
    suggest_duplicate_assets,
    summarize_ops_for_ui,
)
from .portfolio_index import AssetNode
from .portfolio_io import ensure_asset_ids, safe_str
from .portfolio_edit import add_child_asset, can_add_child


def render_chat(*, portfolio: Dict[str, Any], nodes: List[AssetNode], selected_node: AssetNode | None) -> None:
    st.subheader("Chat")

    with st.expander("Freeform intake (AI)", expanded=False):
        st.caption("Paste any text (listing, notes, PDF copy/paste). I’ll draft structured portfolio changes and questions.")

        if "intake_freeform_text" not in st.session_state:
            st.session_state.intake_freeform_text = ""
        if "intake_draft" not in st.session_state:
            st.session_state.intake_draft = None
        if "intake_ref_to_id" not in st.session_state:
            st.session_state.intake_ref_to_id = {}
        if "intake_applied_ops" not in st.session_state:
            st.session_state.intake_applied_ops = set()
        if "intake_attach_to_selected" not in st.session_state:
            st.session_state.intake_attach_to_selected = False

        def _op_hash(op: Dict[str, Any]) -> str:
            try:
                payload = json.dumps(op, sort_keys=True, ensure_ascii=False)
            except Exception:
                payload = str(op)
            return hashlib.sha1(payload.encode("utf-8")).hexdigest()

        st.checkbox(
            "Attach new items under currently selected asset",
            key="intake_attach_to_selected",
            help="Off (default) drafts new sites/land at the portfolio root. Turn on only when the text describes something that belongs inside the selected asset.",
        )

        st.text_area(
            "Input",
            key="intake_freeform_text",
            height=140,
            placeholder="e.g. 3-bed house, gas boiler, loft insulation, 12 LED downlights...",
        )

        col_a, col_b = st.columns([1, 1])
        with col_a:
            if st.button("AI: draft changes", key="intake_draft_btn"):
                with st.spinner("Drafting changes..."):
                    st.session_state.intake_ref_to_id = {}
                    st.session_state.intake_applied_ops = set()
                    st.session_state.intake_draft = llm_draft_intake_ops(
                        portfolio=portfolio,
                        nodes=nodes,
                        selected_node=selected_node if bool(st.session_state.get("intake_attach_to_selected")) else None,
                        freeform_text=safe_str(st.session_state.intake_freeform_text),
                    )
        with col_b:
            if st.button("Clear", key="intake_clear_btn"):
                st.session_state.intake_draft = None
                st.session_state.intake_freeform_text = ""
                st.session_state.intake_ref_to_id = {}
                st.session_state.intake_applied_ops = set()

        draft = st.session_state.get("intake_draft")
        if isinstance(draft, dict):
            status = safe_str(draft.get("status") or "").strip()
            notes = safe_str(draft.get("notes") or "").strip()
            ops = draft.get("ops") if isinstance(draft.get("ops"), list) else []
            questions = draft.get("open_questions") if isinstance(draft.get("open_questions"), list) else []
            assumptions = draft.get("assumptions") if isinstance(draft.get("assumptions"), list) else []

            if status:
                st.write(f"Status: {status}")
            if notes:
                st.write(notes)

            if ops:
                st.markdown("**Proposed changes**")

                def _op_ready(op: Dict[str, Any], ref_map: Dict[str, str]) -> bool:
                    op_name = safe_str(op.get("op")).strip().lower()
                    if op_name == "add_asset":
                        parent_id = safe_str(op.get("parent_id")).strip()
                        parent_ref = safe_str(op.get("parent_ref")).strip()
                        if parent_id:
                            return True
                        if parent_ref:
                            return parent_ref in ref_map
                        return True
                    if op_name in {"apply_template", "update_asset"}:
                        asset_id = safe_str(op.get("asset_id")).strip()
                        asset_ref = safe_str(op.get("asset_ref")).strip()
                        return bool(asset_id) or (asset_ref in ref_map)
                    return True

                ref_map = st.session_state.get("intake_ref_to_id")
                if not isinstance(ref_map, dict):
                    ref_map = {}
                    st.session_state.intake_ref_to_id = ref_map

                applied_ops = st.session_state.get("intake_applied_ops")
                if not isinstance(applied_ops, set):
                    applied_ops = set()
                    st.session_state.intake_applied_ops = applied_ops

                op_lines = summarize_ops_for_ui(ops=ops, nodes=nodes)
                for idx, (op, line) in enumerate(zip(ops, op_lines)):
                    ready = _op_ready(op, ref_map)
                    op_key = _op_hash(op)
                    already_applied = op_key in applied_ops
                    c1, c2 = st.columns([6, 1])
                    with c1:
                        st.write(f"- {line}")
                    with c2:
                        if st.button(
                            "Applied" if already_applied else "Apply",
                            key=f"intake_apply_one::{idx}",
                            disabled=(already_applied or (not ready)),
                            help=("Apply earlier ops first" if not ready else ""),
                        ):
                            with st.spinner("Applying op..."):
                                summary = apply_intake_ops(
                                    portfolio=portfolio,
                                    nodes=nodes,
                                    selected_node=selected_node,
                                    ops=[op],
                                    existing_ref_to_id=ref_map,
                                )
                                st.session_state.intake_ref_to_id = summary.ref_to_id

                                if summary.results and summary.results[0].ok:
                                    applied_ops.add(op_key)

                                if summary.last_added_asset_id:
                                    st.session_state.selected_node_id = summary.last_added_asset_id
                                st.session_state.asset_tree_initialized = False
                                st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1

                                st.session_state.intake_apply_summary = {
                                    "ok": summary.ok_count,
                                    "total": len(summary.results),
                                    "ref_to_id": summary.ref_to_id,
                                    "failures": [r.message for r in summary.results if not r.ok][:8],
                                }

                                st.rerun()

                if st.button("Apply all", key="intake_apply_btn"):
                    with st.spinner("Applying changes..."):
                        summary = apply_intake_ops(
                            portfolio=portfolio,
                            nodes=nodes,
                            selected_node=selected_node,
                            ops=ops,
                            existing_ref_to_id=ref_map,
                        )

                        # Mark successful ops as applied so their buttons stay disabled.
                        for r in summary.results:
                            if r.ok and isinstance(r.op, dict):
                                applied_ops.add(_op_hash(r.op))

                        if summary.last_added_asset_id:
                            st.session_state.selected_node_id = summary.last_added_asset_id
                        st.session_state.asset_tree_initialized = False
                        st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                        st.session_state.intake_ref_to_id = summary.ref_to_id

                        st.session_state.intake_apply_summary = {
                            "ok": summary.ok_count,
                            "total": len(summary.results),
                            "ref_to_id": summary.ref_to_id,
                            "failures": [r.message for r in summary.results if not r.ok][:8],
                        }

                        st.rerun()

            if questions:
                st.markdown("**Open questions**")
                for q in questions:
                    qq = safe_str(q).strip()
                    if qq:
                        st.write(f"- {qq}")

            if assumptions:
                st.markdown("**Assumptions**")
                for a in assumptions:
                    aa = safe_str(a).strip()
                    if aa:
                        st.write(f"- {aa}")

            applied = st.session_state.get("intake_apply_summary")
            if isinstance(applied, dict) and applied.get("total"):
                ok_n = int(applied.get("ok") or 0)
                total_n = int(applied.get("total") or 0)
                if ok_n == total_n:
                    st.success(f"Applied {ok_n}/{total_n} ops")
                else:
                    st.warning(f"Applied {ok_n}/{total_n} ops")
                    for msg in applied.get("failures") or []:
                        mm = safe_str(msg).strip()
                        if mm:
                            st.write(f"- {mm}")

        # Duplicate suggestions (user-confirmed merge)
        try:
            dups = suggest_duplicate_assets(nodes=nodes)
        except Exception:
            dups = []
        if dups:
            st.divider()
            st.markdown("**Possible duplicates**")
            st.caption("These are name-based matches only. Merge only if you’re sure.")

            for d in dups:
                a_id = safe_str(d.get("a_id")).strip()
                b_id = safe_str(d.get("b_id")).strip()
                a_path = safe_str(d.get("a_path")).strip()
                b_path = safe_str(d.get("b_path")).strip()
                score = float(d.get("score") or 0.0)

                if not a_id or not b_id:
                    continue

                st.write(f"- {a_path}  ↔  {b_path}  (match={score:.2f})")

                col1, col2 = st.columns([1, 1])
                with col1:
                    if st.button("Merge left → right", key=f"dup_merge_lr::{a_id}::{b_id}"):
                        ok, msg = merge_assets(portfolio=portfolio, source_id=a_id, target_id=b_id)
                        if ok:
                            st.session_state.selected_node_id = b_id
                            st.session_state.asset_tree_initialized = False
                            st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                            st.rerun()
                        else:
                            st.error(msg)
                with col2:
                    if st.button("Merge right → left", key=f"dup_merge_rl::{a_id}::{b_id}"):
                        ok, msg = merge_assets(portfolio=portfolio, source_id=b_id, target_id=a_id)
                        if ok:
                            st.session_state.selected_node_id = a_id
                            st.session_state.asset_tree_initialized = False
                            st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                            st.rerun()
                        else:
                            st.error(msg)

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

    if "chat_add_flow" not in st.session_state:
        st.session_state.chat_add_flow = {"active": False, "original": ""}

    def _looks_like_building_word(text: str) -> bool:
        t = (text or "").lower()
        return bool(
            re.search(
                r"\b(building|bilding|buidling|buliding|bulding|builidng|buiilding|house|home|bungalow|cottage|flat|apartment)\b",
                t,
            )
        )

    def _looks_like_add_verb(text: str) -> bool:
        t = (text or "").lower()
        return bool(re.search(r"\b(add|create|make|new|build)\b", t))

    def _extract_building_children(text: str) -> List[str]:
        """Best-effort extraction of child place subtypes mentioned in the text."""

        t = (text or "").lower()
        # Focus on the clause likely listing spaces.
        m = re.search(r"\b(?:it has|with|that has|which has|includes|containing)\b([\s\S]+)$", t)
        clause = m.group(1) if m else t
        # Normalize separators.
        clause = clause.replace("&", " and ")
        parts = [p.strip() for p in re.split(r"[,;\n]|\band\b", clause) if p.strip()]
        joined = " ".join(parts)

        found: List[str] = []

        def add_once(x: str) -> None:
            if x not in found:
                found.append(x)

        if re.search(r"\bkitchen\b", joined):
            add_once("kitchen")
        if re.search(r"\bhall\b", joined):
            add_once("hall")
        if re.search(r"\bgarage\b", joined):
            add_once("garage")
        if re.search(r"\b(toilet|loo|wc|restroom|bathroom)\b", joined):
            add_once("toilet")
        return found

    def _extract_building_name(text: str) -> str:
        t = (text or "").strip()
        if not t:
            return ""

        mq = re.search(r"\"([^\"]+)\"", t)
        if mq and mq.group(1).strip():
            return mq.group(1).strip()

        def _clean_name(s: str) -> str:
            s0 = (s or "").strip().strip(" .!?:;\"'“”‘’`")
            # Stop at common trailing clauses.
            s0 = re.split(
                r"\s*(?:,|;|\bwith\b|\bit has\b|\bthat has\b|\bwhich has\b|\bincludes\b|\bcontaining\b)\b",
                s0,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            s0 = s0.strip().strip(" .!?:;\"'“”‘’`")
            # Normalize casing only if user used all-lowercase.
            if s0 and s0 == s0.lower():
                s0 = " ".join(w.capitalize() for w in s0.split())
            return s0

        m = re.search(
            r"\b(?:called|calle|named|name|call)\b\s+(?:it\s+)?(?:the\s+)?(.+?)(?:(?:,|;)|\bwith\b|\bit has\b|\bthat has\b|\bwhich has\b|\bincludes\b|\bcontaining\b|$)",
            t,
            flags=re.IGNORECASE,
        )
        if m and (m.group(1) or "").strip():
            return _clean_name(m.group(1) or "")

        m2 = re.search(
            r"\b(?:add|create|make|build)\b\s+(?:me\s+)?(?:a|an|the)?\s*(?:new\s+)?(?:building|bilding|buidling|buliding|bulding)\s+(.+?)(?:(?:,|;)|\bwith\b|\bit has\b|\bthat has\b|\bwhich has\b|\bincludes\b|\bcontaining\b|$)",
            t,
            flags=re.IGNORECASE,
        )
        if m2 and (m2.group(1) or "").strip():
            return _clean_name(m2.group(1) or "")

        # Handle "add a house ..." and similar.
        m3 = re.search(
            r"\b(?:add|create|make|build)\b\s+(?:me\s+)?(?:a|an|the)?\s*(?:new\s+)?(?:house|home|bungalow|cottage|flat|apartment)\s+(.+?)(?:(?:,|;)|\bwith\b|\bit has\b|\bthat has\b|\bwhich has\b|\bincludes\b|\bcontaining\b|$)",
            t,
            flags=re.IGNORECASE,
        )
        if m3 and (m3.group(1) or "").strip():
            return _clean_name(m3.group(1) or "")

        return ""

    def _extract_equipment_intents(text: str) -> List[str]:
        """Extract simple equipment add intents to apply after building creation."""

        t = (text or "").lower()
        intents: List[str] = []
        if re.search(r"\bheat\s*pump\b", t):
            intents.append("add a heat pump")
        return intents

    def _create_building_now(
        *,
        parent_id: str,
        parent_path: str,
        name: str,
        children: List[str],
        location: str = "",
        description: str = "",
    ) -> Tuple[bool, str]:
        new_id = uuid.uuid4().hex
        new_asset: Dict[str, Any] = {
            "_id": new_id,
            "name": name,
            "core_type": "place",
            "subtype": "building",
        }
        if location.strip():
            new_asset["location"] = location.strip()
        if description.strip():
            new_asset["description"] = description.strip()

        # Apply the building place template if present.
        try:
            from .asset_types import apply_asset_type_template, load_asset_type

            td = load_asset_type("place_building")
            if isinstance(td, dict):
                apply_asset_type_template(asset=new_asset, type_def=td, portfolio=portfolio)
        except Exception:
            pass

        ok = False
        pid = safe_str(parent_id).strip()
        if pid:
            try:
                ok = bool(add_child_asset(portfolio, parent_id=pid, child_asset=new_asset))
            except Exception:
                ok = False
        else:
            roots = portfolio.get("assets")
            if not isinstance(roots, list):
                roots = []
                portfolio["assets"] = roots
            roots.append(new_asset)
            ok = True
        if not ok:
            return False, ""

        # Add mentioned spaces as children under the new building.
        child_subtypes = [safe_str(x).strip().lower() for x in (children or []) if safe_str(x).strip()]
        template_by_subtype = {
            "hall": "place_room",
            "kitchen": "place_room",
            "garage": "place_room",
            "toilet": "place_room",
        }
        for subtype in child_subtypes:
            label = "Main Hall" if subtype == "hall" else ("Garage" if subtype == "garage" else subtype.capitalize())
            child_asset: Dict[str, Any] = {
                "_id": uuid.uuid4().hex,
                "name": label,
                "core_type": "place",
                "subtype": "room",
            }
            try:
                from .asset_types import apply_asset_type_template, load_asset_type

                tid = template_by_subtype.get(subtype, "")
                td = load_asset_type(tid) if tid else None
                if isinstance(td, dict):
                    apply_asset_type_template(asset=child_asset, type_def=td, portfolio=portfolio)
            except Exception:
                pass

            try:
                add_child_asset(portfolio, parent_id=new_id, child_asset=child_asset)
            except Exception:
                pass

        ensure_asset_ids(portfolio, id_key="_id")
        try:
            from .portfolio_io import ensure_asset_data_fields, ensure_asset_ontology_fields

            ensure_asset_data_fields(portfolio)
            ensure_asset_ontology_fields(portfolio)
        except Exception:
            pass

        st.session_state.selected_node_id = new_id
        st.session_state.asset_tree_initialized = False
        st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
        return True, new_id

    def _start_building_flow(user_text: str) -> str:
        """Initialize a multi-turn building creation flow and return the assistant prompt."""

        extracted_name = _extract_building_name(user_text)
        extracted_children = _extract_building_children(user_text)
        pending_add = _extract_equipment_intents(user_text)

        # If we have enough information and the current selection can accept a building, create immediately.
        if extracted_name and selected_node is not None:
            try:
                building_stub = {"name": extracted_name, "core_type": "place", "subtype": "building"}
                if can_add_child(parent_asset=selected_node.data, child_asset=building_stub):
                    parent_id = safe_str(selected_node.data.get("_id")).strip()
                    if parent_id:
                        ok, _new_id = _create_building_now(
                            parent_id=parent_id,
                            parent_path=safe_str(selected_node.path),
                            name=extracted_name,
                            children=extracted_children,
                        )
                        if ok:
                            return (
                                f"Added building '{extracted_name}' under {safe_str(selected_node.path)}."
                                + (f" Included: {', '.join(extracted_children)}." if extracted_children else "")
                            )
            except Exception:
                pass

        st.session_state.chat_building_flow = {
            "active": True,
            "step": "name",
            "name": extracted_name,
            "children": extracted_children,
            "parent_id": "",
            "parent_path": "",
            "location": "",
            "description": "",
            "candidates": [],
            "pending_add": pending_add,
        }

        if safe_str(st.session_state.chat_building_flow.get("name")).strip():
            st.session_state.chat_building_flow["step"] = "parent"
            return _prompt_for_parent()
        return "OK — what should the new building be called? (Type 'cancel' to stop.)"

    def _eligible_parent_candidates() -> List[Tuple[str, str]]:
        """Return list of (node_id, path) that can accept a building child."""

        building_stub = {"name": "New building", "core_type": "place", "subtype": "building"}
        candidates: List[Tuple[str, str]] = []
        for n in nodes:
            try:
                if can_add_child(parent_asset=n.data, child_asset=building_stub):
                    candidates.append((n.node_id, safe_str(n.path)))
            except Exception:
                continue

        # Prefer sites/land-like containers first (best-effort).
        def score(item: Tuple[str, str]) -> Tuple[int, str]:
            node_id, path = item
            nd = next((x for x in nodes if x.node_id == node_id), None)
            subtype = safe_str(nd.data.get("subtype")).strip().lower() if nd else ""
            core_type = safe_str(nd.data.get("core_type")).strip().lower() if nd else ""
            pri = 5
            if core_type == "place" and subtype == "site":
                pri = 0
            elif core_type == "place" and subtype in ("land", "field", "grounds"):
                pri = 1
            elif core_type == "place" and subtype == "building":
                pri = 2
            return (pri, path)

        candidates.sort(key=score)
        return candidates

    def _prompt_for_parent() -> str:
        flow = st.session_state.get("chat_building_flow")
        if not isinstance(flow, dict):
            return "Which site should it go under?"

        # If current selection can accept a building, default to it.
        if selected_node is not None:
            try:
                building_stub = {"name": "New building", "core_type": "place", "subtype": "building"}
                if can_add_child(parent_asset=selected_node.data, child_asset=building_stub):
                    pid = safe_str(selected_node.data.get("_id")).strip()
                    if pid:
                        flow["parent_id"] = pid
                        flow["parent_path"] = safe_str(selected_node.path)
                        flow["step"] = "location"
                        return (
                            f"Adding under the currently selected asset: {safe_str(selected_node.path)}.\n\n"
                            "Where is this building located? (optional — reply with a town/city, or 'skip')"
                        )
            except Exception:
                pass

        candidates = _eligible_parent_candidates()
        flow["candidates"] = candidates

        # If we can confidently pick a parent, do it automatically to avoid nesting a new
        # building under an incompatible selection (e.g. a heat pump).
        if candidates:
            node_id, path = candidates[0]
            flow["parent_id"] = safe_str(node_id)
            flow["parent_path"] = safe_str(path)
            flow["step"] = "location"
            return (
                f"Adding under: {safe_str(path)}.\n\n"
                "Where is this building located? (optional — reply with a town/city, or 'skip')"
            )

        # Fall back to portfolio root.
        flow["parent_id"] = ""
        flow["parent_path"] = safe_str(portfolio.get("portfolio_name") or "Portfolio")
        flow["step"] = "location"
        return (
            "Adding at the portfolio root (top level).\n\n"
            "Where is this building located? (optional — reply with a town/city, or 'skip')"
        )

    def _parse_parent_choice(user_text: str) -> Optional[Tuple[str, str]]:
        flow = st.session_state.get("chat_building_flow")
        if not isinstance(flow, dict):
            return None
        candidates = flow.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            candidates = _eligible_parent_candidates()
            flow["candidates"] = candidates

        s = (user_text or "").strip()
        if not s:
            return None

        m = re.search(r"\b(\d{1,3})\b", s)
        if m:
            try:
                idx = int(m.group(1))
                if 1 <= idx <= len(candidates):
                    node_id, path = candidates[idx - 1]
                    return (node_id, path)
            except Exception:
                pass

        s_low = s.lower()
        hits = [(nid, p) for (nid, p) in candidates if s_low in (p or "").lower()]
        if len(hits) == 1:
            return hits[0]
        return None

    def _continue_building_flow(user_text: str) -> str:
        flow = st.session_state.get("chat_building_flow")
        if not isinstance(flow, dict) or not bool(flow.get("active")):
            return llm_chat_answer(portfolio, nodes, user_text)

        s = (user_text or "").strip()
        if s.lower() in ("cancel", "stop", "exit", "quit"):
            st.session_state.pop("chat_building_flow", None)
            return "OK — cancelled building creation."

        step = safe_str(flow.get("step")).strip() or "name"

        # Capture equipment mentions at any step so the user can say
        # "it has a heat pump" during the building wizard.
        pending_add = flow.get("pending_add") if isinstance(flow.get("pending_add"), list) else []
        for intent in _extract_equipment_intents(s):
            if intent not in pending_add:
                pending_add.append(intent)
        flow["pending_add"] = pending_add

        if step == "name":
            # Allow the user to paste the whole sentence; extract only the name.
            extracted = _extract_building_name(s)
            if not extracted:
                # If the user gave details (e.g. "it has a heat pump"), don't treat that as the name.
                if re.search(r"\b(has|have|got|with|includes|including)\b", s, flags=re.IGNORECASE):
                    return "What should the new building be called? (e.g. \"Call it Rawel House\") (Type 'cancel' to stop.)"
            name = extracted or s.strip(" .!?:;\"'“”‘’`")
            if not name:
                return "What should the new building be called? (Type 'cancel' to stop.)"
            flow["name"] = name
            # Also pick up any mentioned children in this response.
            existing = flow.get("children") if isinstance(flow.get("children"), list) else []
            for c in _extract_building_children(s):
                if c not in existing:
                    existing.append(c)
            flow["children"] = existing
            flow["step"] = "parent"
            return _prompt_for_parent()

        if step == "parent":
            choice = _parse_parent_choice(s)
            if choice is None:
                return _prompt_for_parent()
            node_id, path = choice
            flow["parent_id"] = node_id
            flow["parent_path"] = path
            flow["step"] = "location"
            return "Where is this building located? (optional — reply with a town/city, or 'skip')"

        if step == "location":
            if s.lower() != "skip":
                flow["location"] = s
            flow["step"] = "description"
            return "Give me a short description for the building (optional — or 'skip')."

        if step == "description":
            if s.lower() != "skip":
                flow["description"] = s

            name = safe_str(flow.get("name")).strip() or "New building"
            parent_id = safe_str(flow.get("parent_id")).strip()
            parent_path = safe_str(flow.get("parent_path")).strip() or parent_id

            children = flow.get("children") if isinstance(flow.get("children"), list) else []
            ok, _new_id = _create_building_now(
                parent_id=parent_id,
                parent_path=parent_path,
                name=name,
                children=[safe_str(x) for x in children],
                location=safe_str(flow.get("location")),
                description=safe_str(flow.get("description")),
            )

            # Apply any captured equipment intents under the new building.
            if ok and _new_id:
                try:
                    from .portfolio_index import index_portfolio

                    _nodes2, by_id2 = index_portfolio(portfolio)
                    new_node = by_id2.get(_new_id)
                    if new_node is not None:
                        for intent in (flow.get("pending_add") if isinstance(flow.get("pending_add"), list) else []):
                            llm_edit_selected_subtree(
                                portfolio=portfolio,
                                selected_node=new_node,
                                user_message=safe_str(intent) or "",
                            )
                except Exception:
                    pass

            st.session_state.pop("chat_building_flow", None)

            if not ok:
                return f"I couldn't add '{name}' under {parent_path}. Try selecting the parent in the sidebar and retry."

            extra = ""
            if isinstance(flow.get("pending_add"), list) and flow.get("pending_add"):
                extra = " Added: " + ", ".join([safe_str(x) for x in flow.get("pending_add") if safe_str(x).strip()]) + "."
            return f"Added building '{name}' under {parent_path}." + extra

        return "What's the building name?"

    def _looks_like_add_building_intent(text: str) -> bool:
        return _looks_like_add_verb(text) and _looks_like_building_word(text)

    def _looks_like_add_asset_intent(text: str) -> bool:
        t = (text or "").strip().lower()
        if not t:
            return False
        # Avoid triggering on advisory questions.
        if re.search(r"\bshould i\b", t):
            return False

        # Includes common verbs and is intentionally broad; building intent is handled separately first.
        if bool(re.search(r"\b(add|create|make|install|put|place|fit|build)\b", t)):
            return True

        # Also treat descriptive phrasing as an add intent when it clearly names an addable asset.
        mentions_addable = bool(
            re.search(
                r"\b(heat pump|boiler|solar|pv|photovoltaic|battery|floodlight|floodlights|lighting|lights|fridge|refrigerator|freezer|oven)\b",
                t,
            )
        )
        has_like = bool(re.search(r"\b(also\s+)?(has|have|got|with|includes|including|contain|containing)\b", t))
        return bool(mentions_addable and has_like)

    # Render messages first so the input stays below.
    messages_box = _scroll_container(height=560)
    with messages_box:
        for msg in st.session_state.chat_messages:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])

    prompt = "Ask about the portfolio"
    with st.form(key="chat_send_form", clear_on_submit=True):
        question = st.text_input("", placeholder=prompt, label_visibility="collapsed")
        submitted = st.form_submit_button("Send", use_container_width=True)

    if submitted and (question or "").strip():
        question = (question or "").strip()
        st.session_state.chat_messages.append({"role": "user", "content": question})

        # Intercept building creation flow.
        flow = st.session_state.get("chat_building_flow")
        if isinstance(flow, dict) and bool(flow.get("active")):
            answer = _continue_building_flow(question)
            # _continue_building_flow may rerun on success.
            st.session_state.chat_messages.append({"role": "assistant", "content": answer})
            st.rerun()

        if _looks_like_add_building_intent(question):
            answer = _start_building_flow(question)
            st.session_state.chat_messages.append({"role": "assistant", "content": answer})
            st.rerun()

        # Generic edit mode: add assets under the currently selected node.
        add_flow = st.session_state.get("chat_add_flow")
        if not isinstance(add_flow, dict):
            add_flow = {"active": False, "original": ""}
            st.session_state.chat_add_flow = add_flow

        is_followup = bool(add_flow.get("active")) and not _looks_like_add_asset_intent(question)
        is_new_add = _looks_like_add_asset_intent(question)

        if is_new_add or is_followup:
            if selected_node is None:
                answer = "Select a parent asset in the sidebar, then say e.g. 'add a heat pump'."
                st.session_state.chat_messages.append({"role": "assistant", "content": answer})
                st.rerun()

            user_message = question
            if is_followup:
                original = safe_str(add_flow.get("original")).strip()
                user_message = (original + "\n\nFollow-up details: " + question).strip() if original else question
            else:
                # New add attempt.
                add_flow["active"] = False
                add_flow["original"] = question

            with st.spinner("Adding asset..."):
                reply, applied, added_asset_id = llm_edit_selected_subtree(
                    portfolio=portfolio,
                    selected_node=selected_node,
                    user_message=user_message,
                )

            st.session_state.chat_messages.append({"role": "assistant", "content": reply})

            if applied and safe_str(added_asset_id).strip():
                st.session_state.selected_node_id = safe_str(added_asset_id).strip()
                st.session_state.asset_tree_initialized = False
                st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                add_flow["active"] = False
                add_flow["original"] = ""
            else:
                # If we didn't apply changes, keep the flow active so the user's next message can answer clarifications.
                add_flow["active"] = True
            st.rerun()

        with st.spinner("Thinking..."):
            answer = llm_chat_answer(portfolio, nodes, question)

        st.session_state.chat_messages.append({"role": "assistant", "content": answer})
        st.rerun()
