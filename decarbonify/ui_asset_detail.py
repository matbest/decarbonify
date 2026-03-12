from __future__ import annotations

import html
import json
import uuid
from typing import Any, Dict, List

import streamlit as st

from .portfolio_index import AssetNode, iter_assets_tree
from .portfolio_io import as_list, ensure_asset_ids


# Backward-compat shim: some deployments may not yet have ensure_asset_data_fields.
try:
    from .portfolio_io import ensure_asset_data_fields  # type: ignore
except Exception:  # pragma: no cover
    def ensure_asset_data_fields(portfolio: Dict[str, Any]) -> None:  # type: ignore
        def walk(assets: Any) -> None:
            for asset in as_list(assets):
                if not isinstance(asset, dict):
                    continue
                fields = asset.get("data_fields")
                if not isinstance(fields, dict):
                    fields = {}
                    asset["data_fields"] = fields
                key = "emissions_tco2e_per_year"
                entry = fields.get(key)
                if not isinstance(entry, dict):
                    entry = {"label": "Emissions", "kind": "number", "unit": "tCO2e/year"}
                    fields[key] = entry
                derived = entry.get("derived")
                if not isinstance(derived, dict):
                    derived = {}
                    entry["derived"] = derived
                derived.setdefault("value", None)
                manual = entry.get("manual")
                if not isinstance(manual, dict):
                    manual = {}
                    entry["manual"] = manual
                manual.setdefault("value", None)
                walk(asset.get("assets"))

        walk(portfolio.get("assets"))
from .portfolio_io import safe_str
from . import auth
from .portfolio_edit import (
    RemovedAsset,
    add_child_asset,
    add_sibling_asset_after,
    can_add_child_type,
    explain_disallowed_child,
    find_asset_ref,
    mark_asset_active,
    mark_asset_retired,
    normalize_asset_type,
    remove_asset_by_id,
    remove_asset_snapshot,
    restore_removed_asset,
)
from .emissions import (
    DATA_FIELDS_KEY,
    EMISSIONS_KEY,
    emissions_field_help_text,
    get_derived_value,
    get_manual_value,
    iter_asset_and_descendants,
    sum_emissions_produced_tco2e_per_year,
)
from .llm_emissions import estimate_emissions_tco2e_per_year, suggest_emissions_inputs
from .recommendations import (
    carbon_signal,
    heuristic_recommendations,
    llm_recommendations,
    openai_client_available,
    recommendation_id,
)


def render_asset_detail_and_recommendations(*, portfolio: Dict[str, Any], selected_node: AssetNode) -> None:
    asset = selected_node.data
    asset_id = safe_str(asset.get("_id"))

    asset_name = safe_str(asset.get("name")) or "Asset"

    st.markdown(
        """
<style>
/* Keep the asset title/type on one line so the ✎ doesn't wrap underneath. */
.dc-asset-title { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 0; }
.dc-asset-type { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
</style>
""",
        unsafe_allow_html=True,
    )
    name_line, name_edit = st.columns([0.92, 0.08], gap="small")
    with name_line:
        st.markdown(f"<h2 class='dc-asset-title'>{html.escape(asset_name)}</h2>", unsafe_allow_html=True)
    with name_edit:
        rename_toggle_key = f"name_editor_open::{asset_id or selected_node.node_id}"
        if rename_toggle_key not in st.session_state:
            st.session_state[rename_toggle_key] = False
        if st.button(
            "✎",
            key=f"name_edit_btn::{asset_id or selected_node.node_id}",
            help="Rename asset",
            disabled=not bool(asset_id),
        ):
            st.session_state[rename_toggle_key] = not bool(st.session_state.get(rename_toggle_key))

    if bool(st.session_state.get(f"name_editor_open::{asset_id or selected_node.node_id}")):
        if not asset_id:
            st.warning("This asset is missing an _id, so it can't be renamed safely.")
        else:
            rename_key = f"rename_name::{asset_id}"
            new_name = st.text_input(
                "New name",
                value=safe_str(asset.get("name")),
                key=rename_key,
                label_visibility="collapsed",
                placeholder="New name",
            )

            save_col, cancel_col = st.columns([0.20, 0.20], gap="small")
            with save_col:
                if st.button(
                    "Save",
                    type="primary",
                    disabled=not (new_name or "").strip(),
                    key=f"rename_save_btn::{asset_id}",
                ):
                    asset["name"] = (new_name or "").strip()
                    st.session_state.pop(rename_key, None)
                    st.session_state[rename_toggle_key] = False
                    st.session_state.asset_tree_initialized = False
                    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                    st.rerun()
            with cancel_col:
                if st.button("Cancel", key=f"rename_cancel_btn::{asset_id}"):
                    st.session_state.pop(rename_key, None)
                    st.session_state[rename_toggle_key] = False

    def _regenerate_recommendations_for_subtree() -> None:
        if not asset_id:
            return
        if not openai_client_available():
            st.warning("AI is disabled (missing OPENAI_API_KEY).")
            return

        with st.spinner("Regenerating recommendations..."):
            for a in iter_asset_and_descendants(asset):
                if not isinstance(a, dict):
                    continue
                a["llm_recommendations"] = llm_recommendations(portfolio, a)

        # Refresh the tree/UI without autosaving.
        st.session_state.asset_tree_initialized = False
        st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
        st.rerun()

    def _asset_fields() -> Dict[str, Any]:
        fields = asset.get(DATA_FIELDS_KEY)
        if not isinstance(fields, dict):
            fields = {}
            asset[DATA_FIELDS_KEY] = fields
        return fields

    def _sorted_field_keys(fields: Dict[str, Any]) -> List[str]:
        keys = [k for k in fields.keys() if isinstance(k, str) and k]
        keys.sort()
        if EMISSIONS_KEY in keys:
            keys.remove(EMISSIONS_KEY)
            keys.insert(0, EMISSIONS_KEY)
        return keys

    def _field_kind(fields: Dict[str, Any], key: str) -> str:
        entry = fields.get(key)
        if isinstance(entry, dict):
            kind = entry.get("kind")
            if isinstance(kind, str) and kind:
                return kind
        return "string"

    def _ensure_field(fields: Dict[str, Any], key: str) -> Dict[str, Any]:
        entry = fields.get(key)
        if not isinstance(entry, dict):
            entry = {}
            fields[key] = entry
        entry.setdefault("label", key)
        entry.setdefault("kind", "string")
        derived = entry.get("derived")
        if not isinstance(derived, dict):
            derived = {}
            entry["derived"] = derived
        derived.setdefault("value", None)
        manual = entry.get("manual")
        if not isinstance(manual, dict):
            manual = {}
            entry["manual"] = manual
        manual.setdefault("value", None)
        return entry

    def _format_value(value: Any, *, kind: str) -> str:
        if value is None:
            return ""
        if kind == "number":
            try:
                return str(float(value))
            except Exception:
                return str(value)
        return str(value)

    def _parse_manual(raw: str, *, kind: str) -> Any:
        s = (raw or "").strip()
        if s == "":
            return None
        if kind == "number":
            return float(s.replace(",", ""))
        return s

    def _apply_manual_change(*, field_key: str, kind: str, widget_key: str) -> None:
        raw = st.session_state.get(widget_key, "")
        try:
            parsed = _parse_manual(str(raw), kind=kind)
        except Exception:
            # Ignore invalid edits until user fixes the input.
            return

        fields = _asset_fields()
        entry = _ensure_field(fields, field_key)
        manual = entry.get("manual")
        if not isinstance(manual, dict):
            manual = {}
            entry["manual"] = manual
        manual["value"] = parsed

    def _rec_status_map() -> Dict[str, Any]:
        """Read-only status map.

        IMPORTANT: must not mutate the portfolio during render, otherwise the
        sidebar tree may remount and lose selection.
        """

        m = asset.get("recommendation_status")
        return m if isinstance(m, dict) else {}

    def _ensure_rec_status_map() -> Dict[str, Any]:
        m = asset.get("recommendation_status")
        if not isinstance(m, dict):
            m = {}
            asset["recommendation_status"] = m
        return m

    def _portfolio_savings_done_total_tco2_per_year() -> float:
        roots = portfolio.get("assets")
        if not isinstance(roots, list):
            return 0.0
        total = 0.0
        for node in iter_assets_tree(roots, parent_id=None, depth=0, parent_path="", id_key="_id"):
            a = node.data
            m = a.get("recommendation_status")
            if not isinstance(m, dict):
                continue
            for st0 in m.values():
                if not isinstance(st0, dict):
                    continue
                if bool(st0.get("ignored")):
                    continue
                if not bool(st0.get("done")):
                    continue
                try:
                    total += float(st0.get("saving_tco2_per_year", 0) or 0)
                except Exception:
                    continue
        return float(total)

    def _refresh_only() -> None:
        st.session_state.asset_tree_initialized = False
        st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
        st.rerun()

    def _subtree_savings_tco2_per_year(root_asset: Dict[str, Any]) -> tuple[float, float]:
        done = 0.0
        possible = 0.0

        status_map = root_asset.get("recommendation_status")
        status_map = status_map if isinstance(status_map, dict) else {}

        for r in heuristic_recommendations(root_asset):
            rid = recommendation_id(r)
            st0 = status_map.get(rid)
            if isinstance(st0, dict) and bool(st0.get("ignored")):
                continue
            try:
                saving = float(r.get("estimated_saving_tco2_per_year", 0) or 0)
            except Exception:
                saving = 0.0
            possible += saving
            if isinstance(st0, dict) and bool(st0.get("done")):
                done += saving

        for child in as_list(root_asset.get("assets")):
            if not isinstance(child, dict):
                continue
            c_done, c_possible = _subtree_savings_tco2_per_year(child)
            done += c_done
            possible += c_possible

        return float(done), float(possible)

    # --- Top info panel: two vertical sections ---
    left_panel, right_panel = st.columns([0.55, 0.45], gap="large")

    with left_panel:
        st.markdown(f"**Path:** {selected_node.path}")
        type_line, type_edit = st.columns([0.92, 0.08], gap="small")
        with type_line:
            st.markdown(
                f"<div class='dc-asset-type'><strong>Type:</strong> {html.escape(str(selected_node.type))}</div>",
                unsafe_allow_html=True,
            )
        with type_edit:
            toggle_key = f"type_editor_open::{asset_id or selected_node.node_id}"
            if toggle_key not in st.session_state:
                st.session_state[toggle_key] = False
            if st.button("✎", key=f"type_edit_btn::{asset_id or selected_node.node_id}", help="Edit type"):
                st.session_state[toggle_key] = not bool(st.session_state.get(toggle_key))
        st.markdown(f"**Carbon effect (qualitative):** {carbon_signal(asset)}")

        if bool(st.session_state.get(f"type_editor_open::{asset_id or selected_node.node_id}")):
            if not asset_id:
                st.warning("This asset is missing an _id, so it can't be edited safely.")
            else:
                type_edit_key = f"edit_type::{asset_id}"
                proposed_raw = st.text_input(
                    "Type",
                    value=safe_str(asset.get("type")) or "asset",
                    key=type_edit_key,
                    label_visibility="collapsed",
                    placeholder="Type",
                )
                save_col, cancel_col = st.columns([0.20, 0.20], gap="small")
                with save_col:
                    if st.button(
                        "Save",
                        type="primary",
                        disabled=not (proposed_raw or "").strip(),
                        key=f"edit_type_save_btn::{asset_id}",
                    ):
                        proposed = normalize_asset_type(proposed_raw)

                        ok = True

                        # Validate against parent relationship (if any).
                        here_ref = find_asset_ref(portfolio, asset_id=asset_id)
                        parent_type = "asset"
                        if here_ref and here_ref.parent_id:
                            p_ref = find_asset_ref(portfolio, asset_id=here_ref.parent_id)
                            if p_ref and isinstance(p_ref.asset, dict):
                                parent_type = safe_str(p_ref.asset.get("type")) or parent_type
                            if not can_add_child_type(parent_type=parent_type, child_type=proposed):
                                st.error(
                                    explain_disallowed_child(parent_type=parent_type, child_type=proposed)
                                    or "Type not allowed here."
                                )
                                ok = False

                        # Validate that the proposed type can contain existing children.
                        if ok:
                            for ch in as_list(asset.get("assets")):
                                if not isinstance(ch, dict):
                                    continue
                                ch_type = safe_str(ch.get("type")) or "asset"
                                if not can_add_child_type(parent_type=proposed, child_type=ch_type):
                                    ch_name = safe_str(ch.get("name")) or "child asset"
                                    msg = explain_disallowed_child(parent_type=proposed, child_type=ch_type) or "Child type not allowed."
                                    st.error(
                                        f"Can't change this asset to type '{proposed}' because it contains '{ch_name}' (type='{ch_type}'). {msg}"
                                    )
                                    ok = False
                                    break

                        if ok:
                            asset["type"] = proposed
                            st.session_state.pop(type_edit_key, None)
                            st.session_state[toggle_key] = False
                            _refresh_only()
                with cancel_col:
                    if st.button("Cancel", key=f"edit_type_cancel_btn::{asset_id}"):
                        st.session_state.pop(type_edit_key, None)
                        st.session_state[toggle_key] = False

        desc = safe_str(asset.get("description"))
        if desc:
            st.markdown(desc)
        else:
            st.caption("No description available for this asset.")

        total_tco2e, contributing, visited, overrides_used = sum_emissions_produced_tco2e_per_year(asset)
        if contributing > 0:
            st.metric(
                "Total CO₂e for this asset + children (t/year)",
                f"{total_tco2e:.2f}",
                help=(
                    f"Sum of selected asset + {visited - 1} descendants. "
                    f"Values present for {contributing} of {visited} assets; overrides used for {overrides_used} assets."
                ),
            )
        else:
            st.info(emissions_field_help_text())

        done_s, possible_s = _subtree_savings_tco2_per_year(asset)
        g1, g2 = st.columns(2, gap="small")
        with g1:
            st.metric("Potential gains (t/year)", f"{possible_s:.2f}")
        with g2:
            st.metric("Actualised gains (t/year)", f"{done_s:.2f}")
    with right_panel:
        st.markdown("**Data**")

        with st.expander("AI: emissions estimate", expanded=False):
            st.caption("AI can ask for the specific inputs it needs for this asset, then estimate annual tCO2e (positive=emits, negative=sequesters).")

            if not asset_id:
                st.warning("This asset is missing an _id.")
            elif not openai_client_available():
                st.warning("AI is disabled (missing OPENAI_API_KEY).")
            else:
                ai_cols = st.columns([0.55, 0.45], gap="small")
                with ai_cols[0]:
                    if st.button(
                        "Ask AI what inputs are needed",
                        use_container_width=True,
                        key=f"ai_inputs_btn::{asset_id}",
                    ):
                        fields_suggested, reply = suggest_emissions_inputs(portfolio=portfolio, asset=asset, max_fields=3)
                        asset["llm_emissions_inputs"] = fields_suggested
                        asset["llm_emissions_inputs_reply"] = reply
                        st.rerun()
                with ai_cols[1]:
                    if st.button(
                        "Estimate tCO2e/year",
                        use_container_width=True,
                        key=f"ai_estimate_btn::{asset_id}",
                    ):
                        with st.spinner("Estimating..."):
                            value, notes, missing, equation_latex = estimate_emissions_tco2e_per_year(portfolio=portfolio, asset=asset)

                        asset["llm_emissions_estimate_notes"] = notes
                        asset["llm_emissions_estimate_missing"] = missing
                        asset["llm_emissions_estimate_equation_latex"] = equation_latex

                        if value is None:
                            st.warning("Not enough data to estimate yet. See missing inputs below.")

                            # Auto-create missing input fields (up to 3 total) so the user can fill them immediately.
                            missing_keys: List[str] = []
                            if isinstance(missing, list):
                                for k in missing:
                                    ks = safe_str(k)
                                    if ks:
                                        missing_keys.append(ks)

                            if missing_keys:
                                existing_raw = asset.get("llm_emissions_inputs")
                                existing_list: List[Dict[str, Any]] = []
                                if isinstance(existing_raw, list):
                                    existing_list = [x for x in existing_raw if isinstance(x, dict)]

                                existing_keys = {safe_str(x.get("key")) for x in existing_list if safe_str(x.get("key"))}
                                remaining = 3 - len(existing_keys)

                                fields = asset.get(DATA_FIELDS_KEY)
                                if not isinstance(fields, dict):
                                    fields = {}
                                    asset[DATA_FIELDS_KEY] = fields

                                def _guess_kind(k0: str) -> str:
                                    k1 = (k0 or "").lower()
                                    if k1 in {"fuel", "tariff", "supplier"}:
                                        return "string"
                                    if k1.startswith("is_") or k1.startswith("has_"):
                                        return "boolean"
                                    if any(k1.endswith(suf) for suf in [
                                        "_kwh",
                                        "_kw",
                                        "_w",
                                        "_watts",
                                        "_hours",
                                        "_hours_per_day",
                                        "_count",
                                        "_number",
                                        "_qty",
                                        "_quantity",
                                        "_m2",
                                        "_sqm",
                                        "_acres",
                                        "_km",
                                        "_miles",
                                        "_litres",
                                        "_gallons",
                                    ]):
                                        return "number"
                                    return "string"

                                def _guess_unit(k0: str) -> str:
                                    k1 = (k0 or "").lower()
                                    if k1.endswith("_kwh"):
                                        return "kWh"
                                    if k1.endswith("_kw"):
                                        return "kW"
                                    if k1.endswith("_w") or k1.endswith("_watts"):
                                        return "W"
                                    if "hours" in k1:
                                        return "hours"
                                    if k1.endswith("_m2") or k1.endswith("_sqm"):
                                        return "m²"
                                    if k1.endswith("_acres"):
                                        return "acres"
                                    if k1.endswith("_km"):
                                        return "km"
                                    if k1.endswith("_miles"):
                                        return "miles"
                                    if k1.endswith("_litres"):
                                        return "litres"
                                    if k1.endswith("_gallons"):
                                        return "gallons"
                                    return ""

                                for k in missing_keys:
                                    if remaining <= 0:
                                        break
                                    if k in existing_keys:
                                        continue

                                    # Auto-fill electricity carbon intensity from defaults rather than asking the user.
                                    if k.lower() == "carbon_intensity_of_electricity":
                                        assumed = None
                                        try:
                                            import os

                                            assumed = float(os.environ.get("DEFAULT_GRID_INTENSITY_KGCO2E_PER_KWH", "0.20") or 0.20)
                                        except Exception:
                                            assumed = 0.20

                                        entry = fields.get(k)
                                        if not isinstance(entry, dict):
                                            entry = {}
                                            fields[k] = entry
                                        entry.setdefault("label", "Carbon intensity of electricity")
                                        entry.setdefault("kind", "number")
                                        entry.setdefault("unit", "kgCO2e/kWh")
                                        entry.setdefault(
                                            "question",
                                            "Auto-filled from DEFAULT_GRID_INTENSITY_KGCO2E_PER_KWH; override if you know a better local value.",
                                        )
                                        derived = entry.get("derived")
                                        if not isinstance(derived, dict):
                                            derived = {}
                                            entry["derived"] = derived
                                        if derived.get("value") is None:
                                            derived["value"] = float(assumed)
                                        derived.setdefault("notes", "Assumed default grid intensity.")
                                        manual = entry.get("manual")
                                        if not isinstance(manual, dict):
                                            manual = {}
                                            entry["manual"] = manual
                                        manual.setdefault("value", None)
                                        existing_keys.add(k)
                                        continue

                                    kind = _guess_kind(k)
                                    label = (k or "").replace("_", " ").strip().title() or k
                                    unit = _guess_unit(k)
                                    if k.lower() == "fuel":
                                        question = "What fuel/energy source does this use? (electricity, gas, diesel, etc.)"
                                    else:
                                        question = f"Enter {label.lower()}."

                                    existing_list.append(
                                        {
                                            "key": k,
                                            "label": label,
                                            "kind": kind,
                                            "unit": unit,
                                            "question": question,
                                        }
                                    )
                                    existing_keys.add(k)
                                    remaining -= 1

                                    # Also create the underlying data_fields entry so it persists in JSON on save.
                                    entry = fields.get(k)
                                    if not isinstance(entry, dict):
                                        entry = {}
                                        fields[k] = entry
                                    entry.setdefault("label", label)
                                    entry.setdefault("kind", kind)
                                    if unit:
                                        entry.setdefault("unit", unit)
                                    if question:
                                        entry.setdefault("question", question)
                                    manual = entry.get("manual")
                                    if not isinstance(manual, dict):
                                        manual = {}
                                        entry["manual"] = manual
                                    manual.setdefault("value", None)
                                    derived = entry.get("derived")
                                    if not isinstance(derived, dict):
                                        derived = {}
                                        entry["derived"] = derived
                                    derived.setdefault("value", None)

                                asset["llm_emissions_inputs"] = existing_list
                        else:
                            # Write into derived emissions value (manual still overrides).
                            fields = asset.get(DATA_FIELDS_KEY)
                            if not isinstance(fields, dict):
                                fields = {}
                                asset[DATA_FIELDS_KEY] = fields
                            entry = fields.get(EMISSIONS_KEY)
                            if not isinstance(entry, dict):
                                entry = {"label": "Emissions", "kind": "number", "unit": "tCO2e/year"}
                                fields[EMISSIONS_KEY] = entry
                            derived = entry.get("derived")
                            if not isinstance(derived, dict):
                                derived = {}
                                entry["derived"] = derived
                            derived["value"] = float(value)
                            derived["notes"] = notes
                            if equation_latex:
                                derived["equation_latex"] = equation_latex
                            st.success(f"Estimated emissions: {float(value):.2f} tCO2e/year")

                        # Refresh the tree/UI without autosaving.
                        st.session_state.asset_tree_initialized = False
                        st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                        st.rerun()

                reply = safe_str(asset.get("llm_emissions_inputs_reply"))
                if reply:
                    st.caption(reply)

                suggested = asset.get("llm_emissions_inputs")
                suggested_list: List[Dict[str, Any]] = []
                if isinstance(suggested, list):
                    suggested_list = [x for x in suggested if isinstance(x, dict)]

                if suggested_list:
                    st.markdown("**Inputs to fill**")
                    # Render per-suggested input widgets that write into data_fields.manual.value
                    fields = asset.get(DATA_FIELDS_KEY)
                    if not isinstance(fields, dict):
                        fields = {}
                        asset[DATA_FIELDS_KEY] = fields

                    for f in suggested_list:
                        key = safe_str(f.get("key"))
                        if not key:
                            continue
                        label = safe_str(f.get("label")) or key
                        kind = safe_str(f.get("kind")) or "string"
                        unit = safe_str(f.get("unit"))
                        question = safe_str(f.get("question"))

                        entry = fields.get(key)
                        if not isinstance(entry, dict):
                            entry = {}
                            fields[key] = entry
                        entry.setdefault("label", label)
                        entry.setdefault("kind", kind)
                        if unit:
                            entry.setdefault("unit", unit)
                        if question:
                            entry["question"] = question

                        manual = entry.get("manual")
                        if not isinstance(manual, dict):
                            manual = {}
                            entry["manual"] = manual
                        manual_val = manual.get("value")

                        row_l, row_r = st.columns([0.52, 0.48], gap="small")
                        with row_l:
                            st.write(label)
                            if unit:
                                st.caption(unit)
                            if question:
                                st.caption(question)
                        with row_r:
                            widget_key = f"ai_input_{asset_id}_{key}"
                            if kind == "number":
                                default = "" if manual_val is None else str(manual_val)
                                raw = st.text_input("", value=default, key=widget_key, label_visibility="collapsed")
                                s = (raw or "").strip()
                                if s == "":
                                    manual["value"] = None
                                else:
                                    try:
                                        manual["value"] = float(s.replace(",", ""))
                                    except Exception:
                                        st.warning(f"Invalid number for {key}.")
                            elif kind == "boolean":
                                manual["value"] = bool(st.checkbox("", value=bool(manual_val), key=widget_key, label_visibility="collapsed"))
                            else:
                                default = "" if manual_val is None else str(manual_val)
                                manual["value"] = st.text_input("", value=default, key=widget_key, label_visibility="collapsed")

                missing = asset.get("llm_emissions_estimate_missing")
                if isinstance(missing, list) and missing:
                    miss_str = ", ".join(safe_str(x) for x in missing if safe_str(x))
                    if miss_str:
                        st.warning(f"Missing inputs: {miss_str}")

                    # If the estimate needs one more input, allow asking a follow-up question.
                    remaining = 3 - len(suggested_list)
                    if remaining > 0:
                        if st.button(
                            "Ask AI for missing inputs",
                            use_container_width=True,
                            key=f"ai_inputs_missing_btn::{asset_id}",
                        ):
                            fields_new, reply2 = suggest_emissions_inputs(
                                portfolio=portfolio,
                                asset=asset,
                                max_fields=remaining,
                                focus_missing_keys=[safe_str(x) for x in missing if safe_str(x)],
                            )

                            existing_raw = asset.get("llm_emissions_inputs")
                            existing_list: List[Dict[str, Any]] = []
                            if isinstance(existing_raw, list):
                                existing_list = [x for x in existing_raw if isinstance(x, dict)]
                            existing_keys = {safe_str(x.get("key")) for x in existing_list if safe_str(x.get("key"))}

                            for nf in fields_new:
                                if not isinstance(nf, dict):
                                    continue
                                k = safe_str(nf.get("key"))
                                if not k or k in existing_keys:
                                    continue
                                existing_list.append(nf)
                                existing_keys.add(k)

                            asset["llm_emissions_inputs"] = existing_list
                            asset["llm_emissions_inputs_reply"] = reply2
                            st.rerun()

                # Show equation (LaTeX) + explain calculation/assumptions in a small text box.
                # Prefer the latest estimator notes, otherwise fall back to the derived notes (if present).
                notes = safe_str(asset.get("llm_emissions_estimate_notes"))
                equation = safe_str(asset.get("llm_emissions_estimate_equation_latex"))
                if not notes:
                    try:
                        df = asset.get(DATA_FIELDS_KEY)
                        if isinstance(df, dict):
                            e = df.get(EMISSIONS_KEY)
                            if isinstance(e, dict):
                                d = e.get("derived")
                                if isinstance(d, dict):
                                    notes = safe_str(d.get("notes"))
                                    if not equation:
                                        equation = safe_str(d.get("equation_latex"))
                    except Exception:
                        notes = notes

                if equation:
                    st.markdown("**Equation**")
                    st.latex(equation)

                if notes:
                    st.text_area(
                        "How this was calculated",
                        value=notes,
                        height=120,
                        disabled=True,
                        key=f"ai_emissions_notes::{asset_id}",
                    )

    def _delete_rec_saving(*, rec: Dict[str, Any], rec_key: str) -> None:
        """Delete a saved saving: clears Done, undoes applied changes, and removes saved status."""
        widget_key = f"rec_done_{safe_str(asset.get('_id'))}_{rec_key}"
        st.session_state[widget_key] = False

        status_map = _ensure_rec_status_map()
        status = status_map.get(rec_key)
        if not isinstance(status, dict):
            status_map.pop(rec_key, None)
            _refresh_only()
            return

        action = safe_str(rec.get("action") or status.get("action") or "other").lower()
        already_applied = bool(status.get("applied"))

        if already_applied and action in {"add", "switch"}:
            added_id = safe_str(status.get("added_asset_id"))
            if added_id:
                remove_asset_by_id(portfolio, asset_id=added_id)
            if action == "switch":
                mark_asset_active(asset)
                st.session_state.selected_node_id = safe_str(asset.get("_id"))
            status["applied"] = False

        if already_applied and action == "remove":
            snap_raw = status.get("removed_snapshot")
            if isinstance(snap_raw, dict) and isinstance(snap_raw.get("asset"), dict):
                snap = RemovedAsset(
                    asset=dict(snap_raw.get("asset")),
                    parent_id=safe_str(snap_raw.get("parent_id")) or None,
                    index_in_parent=int(snap_raw.get("index_in_parent", 0) or 0),
                )
                restored = restore_removed_asset(portfolio, removed=snap)
                if restored:
                    ensure_asset_ids(portfolio, id_key="_id")
                    ensure_asset_data_fields(portfolio)
                    st.session_state.selected_node_id = safe_str(snap.asset.get("_id"))
            status["applied"] = False

        status_map.pop(rec_key, None)
        _refresh_only()

    # --- Bottom panel: full-width recommendations ---
    base = asset.get("llm_recommendations")
    if isinstance(base, list):
        recs = [r for r in base if isinstance(r, dict)]
    else:
        recs = heuristic_recommendations(asset)

    st.markdown("**Recommendations**")

    if st.button(
        "Regenerate recommendations",
        disabled=not bool(asset_id),
        key=f"regen_recs_btn::{asset_id}",
    ):
        _regenerate_recommendations_for_subtree()

    if not recs:
        st.write("No recommendations for this asset.")
        return

    def _scroll_container(*, height: int):
        try:
            return st.container(height=height)
        except TypeError:
            return st.container()

    recs_box = _scroll_container(height=360)

    def _apply_rec_ignore(*, rec: Dict[str, Any], rec_key: str) -> None:
        widget_key = f"rec_ignore_{safe_str(asset.get('_id'))}_{rec_key}"
        ignored = bool(st.session_state.get(widget_key))
        status_map = _ensure_rec_status_map()
        status = status_map.get(rec_key)
        if not isinstance(status, dict):
            status = {}
            status_map[rec_key] = status
        status["ignored"] = ignored

        # If ignored, also untick Done in UI state (doesn't undo structure; user can unignore if needed).
        done_widget_key = f"rec_done_{safe_str(asset.get('_id'))}_{rec_key}"
        if ignored:
            st.session_state[done_widget_key] = False
            status["done"] = False
            status["applied"] = bool(status.get("applied"))

        st.rerun()

    def _apply_rec_action(*, rec: Dict[str, Any], rec_key: str) -> None:
        # Called when the checkbox changes.
        widget_key = f"rec_done_{safe_str(asset.get('_id'))}_{rec_key}"
        done = bool(st.session_state.get(widget_key))

        status_map = _ensure_rec_status_map()
        status = status_map.get(rec_key)
        if not isinstance(status, dict):
            status = {}
            status_map[rec_key] = status

        status["done"] = done

        action = safe_str(rec.get("action") or "other").lower()
        status["action"] = action
        status["title"] = safe_str(rec.get("title"))
        try:
            status["saving_tco2_per_year"] = float(rec.get("estimated_saving_tco2_per_year", 0) or 0)
        except Exception:
            status["saving_tco2_per_year"] = 0.0
        already_applied = bool(status.get("applied"))

        # Apply structural change only on transition to done=True.
        if done and (not already_applied) and action in {"add", "remove", "switch"}:
            if action in {"add", "switch"}:
                add_asset = rec.get("add_asset")
                if not isinstance(add_asset, dict) or not safe_str(add_asset.get("name")):
                    add_asset = {
                        "name": safe_str(rec.get("title")) or "New asset",
                        "type": "asset",
                    }
                add_payload = dict(add_asset)
                add_payload.setdefault("_id", uuid.uuid4().hex)

                # Enforce parent→child type relationships for structural adds.
                if action == "add":
                    disallowed = explain_disallowed_child(
                        parent_type=safe_str(asset.get("type")),
                        child_type=safe_str(add_payload.get("type")),
                    )
                    if disallowed:
                        st.warning(disallowed)
                        status["applied"] = False
                        _refresh_only()
                        return
                else:
                    # switch adds a sibling, so validate against the parent's type.
                    parent_type = "asset"
                    here_ref = find_asset_ref(portfolio, asset_id=safe_str(asset.get("_id")))
                    if here_ref and here_ref.parent_id:
                        p_ref = find_asset_ref(portfolio, asset_id=here_ref.parent_id)
                        if p_ref and isinstance(p_ref.asset, dict):
                            parent_type = safe_str(p_ref.asset.get("type")) or parent_type
                    disallowed = explain_disallowed_child(
                        parent_type=parent_type,
                        child_type=safe_str(add_payload.get("type")),
                    )
                    if disallowed:
                        st.warning(disallowed)
                        status["applied"] = False
                        _refresh_only()
                        return

                if action == "add":
                    ok_add = add_child_asset(
                        portfolio,
                        parent_id=safe_str(asset.get("_id")),
                        child_asset=add_payload,
                    )
                    if ok_add:
                        status["added_asset_id"] = safe_str(add_payload.get("_id"))
                        status["added_parent_id"] = safe_str(asset.get("_id"))
                else:
                    # switch: add at the same level as the selected asset, and retire the selected asset.
                    ok_add = add_sibling_asset_after(
                        portfolio,
                        sibling_of_id=safe_str(asset.get("_id")),
                        new_asset=add_payload,
                    )
                    if ok_add:
                        mark_asset_retired(asset)
                        status["added_asset_id"] = safe_str(add_payload.get("_id"))
                        status["switched_from_id"] = safe_str(asset.get("_id"))
                if ok_add:
                    ensure_asset_ids(portfolio, id_key="_id")
                    ensure_asset_data_fields(portfolio)
                    status["applied"] = True
                    if action == "switch":
                        st.session_state.selected_node_id = safe_str(add_payload.get("_id"))
                else:
                    st.warning("Could not add asset.")
                    status["applied"] = False

            if action == "remove":
                snap = remove_asset_snapshot(portfolio, asset_id=safe_str(asset.get("_id")))
                removed_parent_id = snap.parent_id if snap else None
                if snap:
                    status["removed_snapshot"] = {
                        "asset": snap.asset,
                        "parent_id": snap.parent_id,
                        "index_in_parent": snap.index_in_parent,
                    }
                status["applied"] = True
                # After removal, select parent (or first node on next run).
                st.session_state.selected_node_id = removed_parent_id or ""

            _refresh_only()

        # For non-structural changes, still persist the done state best-effort.
        if (not done) and already_applied and action in {"add", "switch"}:
            added_id = safe_str(status.get("added_asset_id"))
            if added_id:
                remove_asset_by_id(portfolio, asset_id=added_id)
            if action == "switch":
                mark_asset_active(asset)
                st.session_state.selected_node_id = safe_str(asset.get("_id"))
            status["applied"] = False
            status["added_asset_id"] = ""
            _refresh_only()

        if (not done) and already_applied and action == "remove":
            snap_raw = status.get("removed_snapshot")
            if isinstance(snap_raw, dict) and isinstance(snap_raw.get("asset"), dict):
                snap = RemovedAsset(
                    asset=dict(snap_raw.get("asset")),
                    parent_id=safe_str(snap_raw.get("parent_id")) or None,
                    index_in_parent=int(snap_raw.get("index_in_parent", 0) or 0),
                )
                restored = restore_removed_asset(portfolio, removed=snap)
                if restored:
                    ensure_asset_ids(portfolio, id_key="_id")
                    ensure_asset_data_fields(portfolio)
                    st.session_state.selected_node_id = safe_str(snap.asset.get("_id"))
            status["applied"] = False
            _refresh_only()

        if action not in {"add", "remove", "switch"}:
            # No persistence here; user saves explicitly from the sidebar.
            pass

    with recs_box:
        status_map = _rec_status_map()
        asset_id = safe_str(asset.get("_id"))

        # Keep any Done/Applied items visible even if regeneration didn't include them.
        base_ids: set[str] = set()
        for r0 in recs:
            if not isinstance(r0, dict):
                continue
            rid0 = recommendation_id(r0)
            r0.setdefault("__rid", rid0)
            base_ids.add(rid0)

        if isinstance(status_map, dict):
            for rid0, st0 in status_map.items():
                rid0_s = safe_str(rid0)
                if not rid0_s or rid0_s in base_ids:
                    continue
                if not isinstance(st0, dict):
                    continue
                if not (bool(st0.get("done")) or bool(st0.get("applied"))):
                    continue
                recs.append(
                    {
                        "__rid": rid0_s,
                        "title": safe_str(st0.get("title")) or "Saved recommendation",
                        "description": safe_str(st0.get("description")),
                        "estimated_saving_tco2_per_year": float(st0.get("saving_tco2_per_year", 0) or 0),
                        "action": safe_str(st0.get("action")) or "other",
                        "add_asset": st0.get("add_asset") if isinstance(st0.get("add_asset"), dict) else None,
                    }
                )

        # Table header
        h1, h2, h3, h4, h5 = st.columns([0.56, 0.12, 0.10, 0.11, 0.11], gap="small")
        with h1:
            st.caption("Recommendation")
        with h2:
            st.caption("Type")
        with h3:
            st.caption("Saving")
        with h4:
            st.caption("Ignore")
        with h5:
            st.caption("Done")

        for r in recs:
            rid = safe_str(r.get("__rid")) or recommendation_id(r)
            status = status_map.get(rid) if isinstance(status_map.get(rid), dict) else {}

            title = safe_str(r.get("title"))
            desc = safe_str(r.get("description"))
            saving = float(r.get("estimated_saving_tco2_per_year", 0) or 0)
            action = safe_str(r.get("action") or "other").lower()
            if action in {"add", "remove", "switch"}:
                if not desc:
                    desc = f"Action: {action}"
            done_key = f"rec_done_{asset_id}_{rid}"
            ignore_key = f"rec_ignore_{asset_id}_{rid}"

            c1, c2, c3, c4, c5 = st.columns([0.56, 0.12, 0.10, 0.11, 0.11], gap="small")
            with c1:
                st.markdown(f"**{title}**")
                if desc:
                    st.caption(desc)
            with c2:
                st.write(action)
            with c3:
                st.write(f"{saving:.2f}")
            with c4:
                st.checkbox(
                    "Ignore",
                    value=bool(status.get("ignored")),
                    key=ignore_key,
                    label_visibility="collapsed",
                    on_change=_apply_rec_ignore,
                    kwargs={"rec": r, "rec_key": rid},
                )
            with c5:
                st.checkbox(
                    "Done",
                    value=bool(status.get("done")),
                    key=done_key,
                    label_visibility="collapsed",
                    on_change=_apply_rec_action,
                    kwargs={"rec": r, "rec_key": rid},
                )

                if bool(status.get("done")) or bool(status.get("applied")):
                    st.button(
                        "Bin",
                        key=f"rec_bin_{asset_id}_{rid}",
                        help="Delete this saved saving (clears Done and undoes applied changes if any).",
                        on_click=_delete_rec_saving,
                        kwargs={"rec": r, "rec_key": rid},
                        use_container_width=True,
                    )

            st.divider()
