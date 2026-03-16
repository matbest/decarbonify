from __future__ import annotations

import html
import json
import uuid
from typing import Any, Dict, List

import streamlit as st

from .portfolio_index import AssetNode, iter_assets_tree
from .portfolio_io import as_list, ensure_asset_data_fields, ensure_asset_ids, ensure_asset_ontology_fields, safe_str
from . import auth
from .portfolio_edit import (
    RemovedAsset,
    add_child_asset,
    add_sibling_asset_after,
    find_asset_ref,
    explain_disallowed_child_assets,
    mark_asset_active,
    mark_asset_retired,
    remove_asset_by_id,
    remove_asset_snapshot,
    restore_removed_asset,
)
from .emissions import (
    DATA_FIELDS_KEY,
    EMISSIONS_KEY,
    emissions_field_help_text,
    effective_emissions_tco2e_per_year,
    format_emissions_per_year,
    get_derived_value,
    get_manual_value,
    iter_asset_and_descendants,
    sum_emissions_produced_tco2e_per_year,
)
from .recommendations import (
    carbon_signal,
    extract_recommendation_items,
    generate_recommendations_bundle,
    heuristic_recommendations,
    normalize_recommendations_for_display,
    openai_client_available,
    recommendation_id,
)


def render_asset_detail_and_recommendations(*, portfolio: Dict[str, Any], selected_node: AssetNode) -> None:
    ensure_asset_ontology_fields(portfolio)

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

    def _invalidate_sidebar_recommendation_totals_cache() -> None:
        # Sidebar caches subtree recommendation totals keyed by portfolio_fp.
        # Recommendation status edits are in-memory and may not change portfolio_fp,
        # so clear the cache to force recompute.
        st.session_state.pop("sb_subtree_totals_fp", None)
        st.session_state.pop("sb_subtree_totals", None)

    def _regenerate_recommendations_for_subtree() -> None:
        if not asset_id:
            return

        with st.spinner("Regenerating recommendations..."):
            for a in iter_asset_and_descendants(asset):
                if not isinstance(a, dict):
                    continue
                # Second pass: store a structured bundle per asset.
                a["recommendations"] = generate_recommendations_bundle(portfolio, a)
                # Clean up legacy field to avoid confusion.
                a.pop("llm_recommendations", None)

        # Refresh the tree/UI without autosaving.
        _invalidate_sidebar_recommendation_totals_cache()
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

        recs0 = extract_recommendation_items(root_asset)
        if not recs0:
            recs0 = heuristic_recommendations(root_asset)

        for r in recs0:
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

        active_type_id_for_header = safe_str(asset.get("asset_type_id")).strip()
        type_label_for_header = ""
        core_type = ""
        subtype = ""
        current_role = ""
        if active_type_id_for_header:
            # Read these values from the assigned asset type (single source of truth).
            from .asset_types import load_asset_type

            td0 = load_asset_type(active_type_id_for_header)
            if isinstance(td0, dict):
                type_label_for_header = safe_str(td0.get("label")) or active_type_id_for_header
                core_type = safe_str(td0.get("core_type"))
                subtype = safe_str(td0.get("subtype"))
                current_role = safe_str(td0.get("current_role"))
            else:
                type_label_for_header = active_type_id_for_header
        else:
            # If templates own ontology, lack of a template means these are unassigned.
            type_label_for_header = "Unassigned"
            core_type = "Unassigned"
            subtype = "Unassigned"
            current_role = "Unassigned"

        location = safe_str(asset.get("location"))
        quantity_v = asset.get("quantity")
        quantity_s = ""
        if isinstance(quantity_v, (int, float)):
            quantity_s = str(quantity_v)
        elif quantity_v is not None:
            quantity_s = safe_str(quantity_v).strip()

        st.markdown(
            " "
            + f"<div class='dc-asset-type'><strong>Asset type:</strong> {html.escape(type_label_for_header)}</div>"
            + f"<div class='dc-asset-type'><strong>Core type:</strong> {html.escape(core_type or 'asset')}</div>"
            + (f"<div class='dc-asset-type'><strong>Subtype:</strong> {html.escape(subtype)}</div>" if subtype else "")
            + (f"<div class='dc-asset-type'><strong>Current role:</strong> {html.escape(current_role)}</div>" if current_role else "")
            + (f"<div class='dc-asset-type'><strong>Location:</strong> {html.escape(location)}</div>" if location else "")
            + (f"<div class='dc-asset-type'><strong>Quantity:</strong> {html.escape(quantity_s)}</div>" if quantity_s else ""),
            unsafe_allow_html=True,
        )

        st.markdown(f"**Carbon effect (qualitative):** {carbon_signal(asset)}")

        desc = safe_str(asset.get("description"))
        if desc:
            st.markdown(desc)
        else:
            st.caption("No description available for this asset.")

        eff = effective_emissions_tco2e_per_year(asset)
        if eff is not None:
            st.metric(
                "Emissions (this asset)",
                format_emissions_per_year(float(eff), unit="auto"),
                help=f"Equivalent: {float(eff) * 1000:.0f} kgCO₂e/yr.",
            )

        total_tco2e, contributing, visited, overrides_used = sum_emissions_produced_tco2e_per_year(asset)
        if contributing > 0:
            st.metric(
                "Total CO₂e for this asset + children (t/year)",
                format_emissions_per_year(float(total_tco2e), unit="auto"),
                help=(
                    f"Sum of selected asset + {visited - 1} descendants. "
                    f"Values present for {contributing} of {visited} assets; overrides used for {overrides_used} assets."
                    f"\nEquivalent: {total_tco2e * 1000:.0f} kgCO₂e/yr."
                ),
            )
        else:
            st.info(emissions_field_help_text())

        done_s, possible_s = _subtree_savings_tco2_per_year(asset)
        g1, g2 = st.columns(2, gap="small")
        with g1:
            st.metric("Actualised gains (t/year)", f"{done_s:.2f}")
        with g2:
            st.metric("Potential gains (t/year)", f"{possible_s:.2f}")
    with right_panel:
        st.markdown("**Data**")

        with st.expander("Energy", expanded=False):
            st.caption("Used for sidebar icons and basic energy semantics (e.g. a fridge is a consumer of electricity).")

            if not asset_id:
                st.info("This asset is missing an _id, so edits can't be applied safely.")
            else:
                from .ontology import ENERGY_ROLES, normalize_energy_role

                # Ensure attributes dict exists.
                attrs = asset.get("attributes")
                if not isinstance(attrs, dict):
                    attrs = {}
                    asset["attributes"] = attrs

                role_options = [""] + list(ENERGY_ROLES)
                role_current = normalize_energy_role(safe_str(asset.get("current_role")))
                role_index = role_options.index(role_current) if role_current in role_options else 0
                new_role = st.selectbox(
                    "Current role",
                    role_options,
                    index=role_index,
                    key=f"energy_role::{asset_id}",
                    help="Set to 'consumer' for appliances. Leave blank if not applicable.",
                )

                energy_type_options = ["", "electricity", "gas", "oil", "heat"]
                energy_type_current = safe_str(attrs.get("energy_type")).strip().lower().replace("-", "_")
                if not energy_type_current:
                    # Back-compat: older data stored this as attributes.carrier.
                    energy_type_current = safe_str(attrs.get("carrier")).strip().lower().replace("-", "_")
                energy_type_index = energy_type_options.index(energy_type_current) if energy_type_current in energy_type_options else 0
                new_energy_type = st.selectbox(
                    "Energy type",
                    energy_type_options,
                    index=energy_type_index,
                    key=f"energy_type::{asset_id}",
                    help="Used to infer whether a consumer is electric (plug) vs gas/oil (barrel).",
                )

                changed = False
                role_to_store = safe_str(new_role)
                if normalize_energy_role(safe_str(asset.get("current_role"))) != normalize_energy_role(role_to_store):
                    asset["current_role"] = normalize_energy_role(role_to_store)
                    changed = True

                energy_type_to_store = safe_str(new_energy_type).strip().lower().replace("-", "_")
                if energy_type_to_store:
                    if safe_str(attrs.get("energy_type")).strip().lower().replace("-", "_") != energy_type_to_store:
                        attrs["energy_type"] = energy_type_to_store
                        changed = True
                else:
                    if "energy_type" in attrs:
                        attrs.pop("energy_type", None)
                        changed = True

                if changed:
                    st.session_state.asset_tree_initialized = False
                    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1

        with st.expander("Asset type", expanded=False):
            st.caption(
                "Apply a reusable asset-type template (inputs + formulas). "
                "Fill inputs (manual values override), optionally ask AI for suggestions, then compute outputs into derived fields."
            )

            if not asset_id:
                st.warning("This asset is missing an _id.")
            else:
                from .asset_types import (
                    apply_asset_type_template,
                    compute_asset_type_outputs,
                    list_asset_type_summaries,
                    load_asset_type,
                    persist_computed_outputs,
                )
                from .llm_asset_types import suggest_asset_type_id, suggest_asset_type_input_values

                portfolio_defaults = portfolio.get("defaults") if isinstance(portfolio, dict) else None
                portfolio_defaults = portfolio_defaults if isinstance(portfolio_defaults, dict) else {}

                summaries = list_asset_type_summaries()
                ids = ["(none)"] + [s.id for s in summaries]

                current_type_id = safe_str(asset.get("asset_type_id")).strip()

                def _fmt(tid: str) -> str:
                    if tid == "(none)":
                        return "(none)"
                    for s in summaries:
                        if s.id == tid:
                            return f"{s.label} ({s.id})"
                    return tid

                default_index = 0
                if current_type_id:
                    try:
                        default_index = ids.index(current_type_id)
                    except Exception:
                        default_index = 0

                select_key = f"asset_type_select::{asset_id}"
                pending_key = f"asset_type_select_pending::{asset_id}"
                if pending_key in st.session_state:
                    pending_value = safe_str(st.session_state.get(pending_key)).strip()
                    st.session_state.pop(pending_key, None)
                    if pending_value and pending_value in set(ids):
                        st.session_state[select_key] = pending_value

                selected_type_id = st.selectbox(
                    "Template",
                    ids,
                    index=default_index,
                    format_func=_fmt,
                    key=select_key,
                )

                # Auto-apply selection changes (no explicit Apply button).
                # We also prevent clearing an already-assigned type (no Clear type).
                if selected_type_id == "(none)":
                    if current_type_id:
                        # Revert to current value on next rerun; clearing is intentionally disabled.
                        st.session_state[pending_key] = current_type_id
                        st.rerun()
                else:
                    if selected_type_id != current_type_id:
                        td = load_asset_type(selected_type_id)
                        if not isinstance(td, dict):
                            st.error("Could not load that asset type definition.")
                            st.session_state[pending_key] = current_type_id or "(none)"
                            st.rerun()
                        else:
                            apply_asset_type_template(asset=asset, type_def=td, portfolio=portfolio)
                            st.session_state.asset_tree_initialized = False
                            st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                            st.rerun()

                pick_cols = st.columns([0.34, 0.66], gap="small")
                with pick_cols[0]:
                    if st.button(
                        "AI: pick template",
                        use_container_width=True,
                        disabled=not openai_client_available() or (len(summaries) == 0),
                        key=f"asset_type_ai_pick::{asset_id}",
                        help=None if openai_client_available() else "Requires AI (set OPENAI_API_KEY).",
                    ):
                        with st.spinner("Choosing template..."):
                            templ = [{"id": s.id, "label": s.label, "description": s.description} for s in summaries]
                            suggested_id, reply = suggest_asset_type_id(portfolio=portfolio, asset=asset, templates=templ)

                        # Heuristic fallback for common place/room naming.
                        # This keeps the UX smooth for assets like "Main Hall" when templates were consolidated.
                        if not suggested_id:
                            allowed_ids = {s.id for s in summaries}
                            name_l = safe_str(asset.get("name")).strip().lower()
                            core_type_l = safe_str(asset.get("core_type")).strip().lower()
                            subtype_l = safe_str(asset.get("subtype")).strip().lower()

                            is_placeish = (core_type_l == "place") or (subtype_l in {"room", "building", "site"})
                            room_words = {
                                "hall",
                                "hallway",
                                "corridor",
                                "lobby",
                                "kitchen",
                                "toilet",
                                "wc",
                                "bath",
                                "bathroom",
                                "garage",
                                "office",
                                "bedroom",
                                "living",
                                "lounge",
                                "meeting",
                            }
                            if is_placeish and "place_room" in allowed_ids:
                                if subtype_l == "room":
                                    suggested_id = "place_room"
                                    reply = safe_str(reply).rstrip() + "\n\nFallback: mapped place subtype 'room' to template 'place_room'."
                                elif any(w in name_l for w in room_words):
                                    suggested_id = "place_room"
                                    reply = safe_str(reply).rstrip() + "\n\nFallback: mapped room-like place name to template 'place_room'."

                        asset["llm_asset_type_pick_reply"] = reply
                        asset["llm_asset_type_pick_suggested"] = suggested_id

                        if suggested_id:
                            # Apply immediately so the rest of the UI updates without a second click.
                            td = load_asset_type(suggested_id)
                            if not isinstance(td, dict):
                                st.error("AI suggested a template that could not be loaded.")
                            else:
                                apply_asset_type_template(asset=asset, type_def=td, portfolio=portfolio)
                                st.session_state.asset_tree_initialized = False
                                st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1

                                # Can't modify a widget key after it has been instantiated.
                                # Store pending selection and apply it before the selectbox is created on rerun.
                                st.session_state[f"asset_type_select_pending::{asset_id}"] = suggested_id
                                st.rerun()
                        else:
                            st.warning("AI could not find a suitable template.")

                with pick_cols[1]:
                    pick_reply = safe_str(asset.get("llm_asset_type_pick_reply"))
                    if pick_reply:
                        st.text_area(
                            "AI template notes",
                            value=pick_reply,
                            height=100,
                            disabled=True,
                            key=f"asset_type_ai_pick_notes::{asset_id}",
                        )

                active_type_id = safe_str(asset.get("asset_type_id")).strip()
                type_def = load_asset_type(active_type_id) if active_type_id else None
                if not isinstance(type_def, dict):
                    if active_type_id:
                        st.warning(f"Asset type '{active_type_id}' not found.")
                else:
                    desc = safe_str(type_def.get("description"))
                    if desc:
                        st.caption(desc)

                    # Auto compute outputs whenever inputs change.
                    # Streamlit reruns on Enter/commit; we use change detection to recompute.
                    auto_compute = True

                    # Render input widgets
                    inputs = type_def.get("inputs")
                    inputs_list: List[Dict[str, Any]] = []
                    if isinstance(inputs, list):
                        inputs_list = [x for x in inputs if isinstance(x, dict)]

                    fields = asset.get(DATA_FIELDS_KEY)
                    if not isinstance(fields, dict):
                        fields = {}
                        asset[DATA_FIELDS_KEY] = fields

                    missing_for_ai: List[str] = []
                    inputs_changed = False

                    if inputs_list:
                        st.markdown("**Inputs**")
                    for f in inputs_list:
                        k = safe_str(f.get("key")).strip()
                        if not k:
                            continue
                        label = safe_str(f.get("label")) or k
                        kind = safe_str(f.get("kind")) or "string"
                        unit = safe_str(f.get("unit"))
                        help_text = safe_str(f.get("help"))

                        entry = fields.get(k)
                        if not isinstance(entry, dict):
                            entry = {}
                            fields[k] = entry
                        entry.setdefault("label", label)
                        entry.setdefault("kind", kind)
                        if unit:
                            entry.setdefault("unit", unit)
                        if help_text:
                            entry.setdefault("question", help_text)

                        manual = entry.get("manual")
                        if not isinstance(manual, dict):
                            manual = {}
                            entry["manual"] = manual
                        manual_val = manual.get("value")

                        derived = entry.get("derived")
                        if not isinstance(derived, dict):
                            derived = {}
                            entry["derived"] = derived
                        derived_val = derived.get("value")
                        derived_source = safe_str(derived.get("source")).strip().lower()

                        # If this field is provided by portfolio defaults and not overridden, don't ask for it.
                        if manual_val is None and derived_val is None and k in portfolio_defaults:
                            derived["value"] = portfolio_defaults.get(k)
                            derived["source"] = "portfolio_default"
                            derived_val = derived.get("value")
                            derived_source = "portfolio_default"

                        if manual_val is None and derived_source == "portfolio_default" and k in portfolio_defaults:
                            val = portfolio_defaults.get(k)
                            show = f"Using portfolio default: {val}"
                            if unit:
                                show = show + f" {unit}"
                            st.caption(f"{label}: {show}")
                            continue

                        effective_val = manual_val if manual_val is not None else derived_val
                        if effective_val is None and k in portfolio_defaults:
                            st.caption(f"Will use portfolio default: {portfolio_defaults.get(k)}")
                        elif effective_val is None:
                            missing_for_ai.append(k)

                        row_l, row_r = st.columns([0.55, 0.45], gap="small")
                        with row_l:
                            st.write(label)
                            if unit:
                                st.caption(unit)
                            if help_text:
                                st.caption(help_text)
                            if manual_val is None and derived_val is not None:
                                st.caption(f"Using suggestion/default: {derived_val}")
                        with row_r:
                            widget_key = f"type_input_{asset_id}_{k}"
                            if kind == "number":
                                prev_val = manual_val
                                default = "" if manual_val is None else str(manual_val)
                                # Streamlit widget state persists across reruns. If an external update (e.g. AI intake)
                                # sets manual_val but the widget previously existed as an empty string, the empty
                                # widget state would otherwise overwrite and clear the manual value on render.
                                if manual_val is not None and st.session_state.get(widget_key) in {"", None}:
                                    st.session_state[widget_key] = default
                                raw = st.text_input("", value=default, key=widget_key, label_visibility="collapsed")
                                s = (raw or "").strip()
                                if s == "":
                                    manual["value"] = None
                                else:
                                    try:
                                        manual["value"] = float(s.replace(",", ""))
                                    except Exception:
                                        st.warning(f"Invalid number for {k}.")
                                        # Don't trigger auto-compute when input is invalid.
                                        prev_val = manual.get("value")
                                if prev_val != manual.get("value"):
                                    inputs_changed = True
                            elif kind == "boolean":
                                prev_val = manual_val
                                manual["value"] = bool(
                                    st.checkbox("", value=bool(manual_val), key=widget_key, label_visibility="collapsed")
                                )
                                if prev_val != manual.get("value"):
                                    inputs_changed = True
                            else:
                                prev_val = manual_val
                                default = "" if manual_val is None else str(manual_val)
                                if manual_val is not None and st.session_state.get(widget_key) in {"", None}:
                                    st.session_state[widget_key] = default
                                manual["value"] = st.text_input("", value=default, key=widget_key, label_visibility="collapsed")
                                if prev_val != manual.get("value"):
                                    inputs_changed = True

                    # AI suggestions
                    ai_disabled = not openai_client_available()
                    if st.button(
                        "AI: suggest missing values",
                        use_container_width=True,
                        disabled=ai_disabled or (not missing_for_ai),
                        key=f"asset_type_ai_suggest::{asset_id}",
                        help=None if not ai_disabled else "Requires AI (set OPENAI_API_KEY).",
                    ):
                        with st.spinner("Asking AI..."):
                            suggested, reply = suggest_asset_type_input_values(
                                portfolio=portfolio,
                                asset=asset,
                                type_def=type_def,
                                only_missing_keys=missing_for_ai,
                            )
                        asset["llm_asset_type_inputs_reply"] = reply

                        for k, v in (suggested or {}).items():
                            ks = safe_str(k).strip()
                            if not ks:
                                continue
                            entry = fields.get(ks)
                            if not isinstance(entry, dict):
                                continue
                            manual = entry.get("manual")
                            if isinstance(manual, dict) and manual.get("value") is not None:
                                continue
                            derived = entry.get("derived")
                            if not isinstance(derived, dict):
                                derived = {}
                                entry["derived"] = derived
                            derived["value"] = v
                            derived["source"] = "ai"
                            derived["notes"] = "AI suggestion (may be wrong). Review and override manually if needed."

                        # Trigger auto-compute on next rerun so outputs reflect AI suggestions.
                        st.session_state[f"asset_type_auto_compute_pending::{asset_id}"] = True

                        st.session_state.asset_tree_initialized = False
                        st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                        st.rerun()

                    # Auto compute (runs after inputs render).
                    pending_auto = bool(st.session_state.pop(f"asset_type_auto_compute_pending::{asset_id}", False))
                    if auto_compute and (inputs_changed or pending_auto):
                        computed, missing, errors = compute_asset_type_outputs(asset=asset, type_def=type_def, portfolio=portfolio)
                        st.session_state[f"asset_type_auto_compute_last::{asset_id}"] = {
                            "missing": missing,
                            "errors": errors,
                            "computed_keys": sorted(list(computed.keys())),
                        }
                        if computed:
                            changed = persist_computed_outputs(asset=asset, type_def=type_def, computed=computed)
                            if changed:
                                st.session_state.asset_tree_initialized = False
                                st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                                st.rerun()

                    last = st.session_state.get(f"asset_type_auto_compute_last::{asset_id}")
                    if isinstance(last, dict):
                        missing = last.get("missing") if isinstance(last.get("missing"), list) else []
                        errors = last.get("errors") if isinstance(last.get("errors"), list) else []
                        if errors:
                            st.caption("Missing/invalid inputs for outputs: " + "; ".join([safe_str(e) for e in errors if safe_str(e)]))
                        elif missing:
                            st.caption("Missing inputs for outputs: " + ", ".join([safe_str(m) for m in missing if safe_str(m)]))

                    reply = safe_str(asset.get("llm_asset_type_inputs_reply"))
                    if reply:
                        st.text_area(
                            "AI notes",
                            value=reply,
                            height=120,
                            disabled=True,
                            key=f"asset_type_ai_notes::{asset_id}",
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
        _invalidate_sidebar_recommendation_totals_cache()
        _refresh_only()

    # --- Bottom panel: full-width recommendations ---
    recs = extract_recommendation_items(asset)
    if not recs:
        recs = heuristic_recommendations(asset)
    
    # Ground savings/calculation to current baseline so edits to inputs update the displayed savings.
    recs = normalize_recommendations_for_display(asset, recs)

    st.markdown("**Recommendations**")

    regen_disabled = not bool(asset_id) or (not openai_client_available())
    if st.button(
        "Regenerate recommendations",
        disabled=regen_disabled,
        key=f"regen_recs_btn::{asset_id}",
        help=None if not regen_disabled else "Requires AI (set OPENAI_API_KEY).",
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
        _invalidate_sidebar_recommendation_totals_cache()
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

        _invalidate_sidebar_recommendation_totals_cache()

        action = safe_str(rec.get("action") or "other").lower()
        status["action"] = action
        status["title"] = safe_str(rec.get("title"))
        status["description"] = safe_str(rec.get("description"))
        status["calculation"] = safe_str(rec.get("calculation"))
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
                        "core_type": "asset",
                        "subtype": "",
                    }
                add_payload = dict(add_asset)
                add_payload.setdefault("_id", uuid.uuid4().hex)

                # Normalize the payload onto the ontology schema.
                from .ontology import infer_core_type_and_subtype, normalize_core_type

                if not safe_str(add_payload.get("core_type")).strip():
                    inferred_core, inferred_sub = infer_core_type_and_subtype(legacy_type=safe_str(add_payload.get("type")))
                    add_payload["core_type"] = inferred_core
                    if not safe_str(add_payload.get("subtype")).strip() and inferred_sub:
                        add_payload["subtype"] = inferred_sub
                    else:
                        add_payload.setdefault("subtype", safe_str(add_payload.get("subtype")).strip())
                else:
                    add_payload["core_type"] = normalize_core_type(safe_str(add_payload.get("core_type")))
                    add_payload["subtype"] = safe_str(add_payload.get("subtype")).strip()
                add_payload.pop("type", None)

                # Enforce parent→child type relationships for structural adds.
                if action == "add":
                    disallowed = explain_disallowed_child_assets(parent_asset=asset, child_asset=add_payload)
                    if disallowed:
                        st.warning(disallowed)
                        status["applied"] = False
                        _refresh_only()
                        return
                else:
                    # switch adds a sibling, so validate against the parent's type.
                    parent_asset: Dict[str, Any] = {"core_type": "asset", "subtype": ""}
                    here_ref = find_asset_ref(portfolio, asset_id=safe_str(asset.get("_id")))
                    if here_ref and here_ref.parent_id:
                        p_ref = find_asset_ref(portfolio, asset_id=here_ref.parent_id)
                        if p_ref and isinstance(p_ref.asset, dict):
                            parent_asset = p_ref.asset
                    disallowed = explain_disallowed_child_assets(parent_asset=parent_asset, child_asset=add_payload)
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
                    ensure_asset_ontology_fields(portfolio)
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
                        "calculation": safe_str(st0.get("calculation")),
                        "assumptions": st0.get("assumptions") if isinstance(st0.get("assumptions"), list) else None,
                        "assumed_reduction_fraction": st0.get("assumed_reduction_fraction"),
                        "baseline_source": safe_str(st0.get("baseline_source")),
                        "baseline_tco2e_per_year": st0.get("baseline_tco2e_per_year"),
                        "estimated_saving_tco2_per_year": float(st0.get("saving_tco2_per_year", 0) or 0),
                        "action": safe_str(st0.get("action")) or "other",
                        "add_asset": st0.get("add_asset") if isinstance(st0.get("add_asset"), dict) else None,
                    }
                )
        
        # Re-normalize after merging in saved items so old 0-savings entries can be grounded too.
        recs = normalize_recommendations_for_display(asset, recs)

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
            calc = safe_str(r.get("calculation"))
            assumptions_raw = r.get("assumptions")
            assumptions: List[str] = []
            if isinstance(assumptions_raw, list):
                for a in assumptions_raw[:3]:
                    s = safe_str(a).strip()
                    if s:
                        assumptions.append(s)
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
                if calc:
                    st.caption(f"Calculation: {calc}")
                if assumptions:
                    st.caption("Assumptions: " + " ".join([f"• {a}" for a in assumptions]))
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
