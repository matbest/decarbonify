from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

import streamlit as st

from .portfolio_index import AssetNode, iter_assets_tree
from .portfolio_io import as_list, ensure_asset_data_fields, ensure_asset_ids
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
from .recommendations import (
    carbon_signal,
    heuristic_recommendations,
    llm_recommendations,
    openai_client_available,
    recommendation_id,
)


def render_asset_detail_and_recommendations(*, portfolio: Dict[str, Any], selected_node: AssetNode) -> None:
    st.subheader("Asset Detail")

    asset = selected_node.data
    asset_id = safe_str(asset.get("_id"))

    top_left, top_right = st.columns([0.70, 0.30], gap="small")
    with top_left:
        st.caption("Regenerates recommendations for this asset and its children. Done items stay visible.")
    with top_right:
        if st.button(
            "Regenerate recommendations",
            use_container_width=True,
            disabled=not bool(asset_id),
            key=f"regen_recs_btn::{asset_id}",
        ):
            if not openai_client_available():
                st.warning("AI is disabled (missing OPENAI_API_KEY).")
            else:
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
        st.markdown(f"**Type:** {selected_node.type}")
        st.markdown(f"**Carbon effect (qualitative):** {carbon_signal(asset)}")

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

        with st.expander("Add child asset", expanded=False):
            if not asset_id:
                st.warning("This asset is missing an _id, so children can't be added here yet.")
            else:
                name_key = f"add_child_name::{asset_id}"
                type_key = f"add_child_type::{asset_id}"
                desc_key = f"add_child_desc::{asset_id}"

                child_name = st.text_input("Name", key=name_key)
                child_type = st.text_input("Type", value="asset", key=type_key)
                child_desc = st.text_input("Description (optional)", key=desc_key)

                if st.button(
                    "Add child",
                    type="primary",
                    disabled=not (child_name or "").strip(),
                    key=f"add_child_btn::{asset_id}",
                ):
                    new_asset: Dict[str, Any] = {
                        "name": (child_name or "").strip(),
                        "type": (child_type or "asset").strip() or "asset",
                    }
                    if (child_desc or "").strip():
                        new_asset["description"] = child_desc.strip()

                    disallowed = explain_disallowed_child(
                        parent_type=safe_str(asset.get("type")),
                        child_type=safe_str(new_asset.get("type")),
                    )
                    if disallowed:
                        st.error(disallowed)
                    else:
                        ok_add = add_child_asset(
                            portfolio,
                            parent_id=asset_id,
                            child_asset=new_asset,
                        )
                        if not ok_add:
                            st.error("Couldn't add the child asset.")
                        else:
                            ensure_asset_ids(portfolio, id_key="_id")
                            ensure_asset_data_fields(portfolio)
                            for k in (name_key, type_key, desc_key):
                                st.session_state.pop(k, None)
                            _refresh_only()

        with st.expander("Rename asset", expanded=False):
            if not asset_id:
                st.warning("This asset is missing an _id, so it can't be renamed safely.")
            else:
                rename_key = f"rename_name::{asset_id}"
                new_name = st.text_input("New name", value=safe_str(asset.get("name")), key=rename_key)
                if st.button(
                    "Rename",
                    type="primary",
                    disabled=not (new_name or "").strip(),
                    key=f"rename_btn::{asset_id}",
                ):
                    asset["name"] = (new_name or "").strip()
                    st.session_state.pop(rename_key, None)
                    _refresh_only()

        with st.expander("Edit asset type", expanded=False):
            st.caption("Changing type is validated against the parent and existing children.")
            if not asset_id:
                st.warning("This asset is missing an _id, so it can't be edited safely.")
            else:
                type_edit_key = f"edit_type::{asset_id}"
                proposed_raw = st.text_input(
                    "Type",
                    value=safe_str(asset.get("type")) or "asset",
                    key=type_edit_key,
                )
                if st.button(
                    "Update type",
                    type="primary",
                    disabled=not (proposed_raw or "").strip(),
                    key=f"edit_type_btn::{asset_id}",
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
                            st.error(explain_disallowed_child(parent_type=parent_type, child_type=proposed) or "Type not allowed here.")
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
                        _refresh_only()

        with st.expander("Delete asset", expanded=False):
            st.caption("Deletes this asset and all of its children.")
            if not asset_id:
                st.warning("This asset is missing an _id, so it can't be deleted safely.")
            else:
                confirm_key = f"delete_confirm::{asset_id}"
                confirmed = bool(st.checkbox("I understand this cannot be undone", key=confirm_key))
                if st.button(
                    "Delete asset",
                    type="primary",
                    disabled=not confirmed,
                    key=f"delete_btn::{asset_id}",
                ):
                    snap = remove_asset_snapshot(portfolio, asset_id=asset_id)
                    if not snap:
                        st.error("Couldn't delete the asset (not found).")
                    else:
                        # After deletion, select parent (or first node on next run).
                        st.session_state.selected_node_id = safe_str(snap.parent_id) or ""
                        st.session_state.pop(confirm_key, None)
                        _refresh_only()

    with right_panel:
        st.markdown("**Data**")
        st.caption("Manual values override derived values.")

        fields = _asset_fields()
        # Ensure emissions exists as a minimum field.
        if EMISSIONS_KEY not in fields:
            entry = _ensure_field(fields, EMISSIONS_KEY)
            entry["label"] = "Emissions"
            entry["kind"] = "number"
            entry.setdefault("unit", "tCO2e/year")

        header_left, header_right = st.columns([0.42, 0.58], gap="small")
        with header_left:
            st.markdown("**Field**")
        with header_right:
            st.markdown("**Derived / Input**")

        asset_id = safe_str(asset.get("_id"))
        keys = _sorted_field_keys(fields)
        for k in keys:
            entry = _ensure_field(fields, k)
            kind = _field_kind(fields, k)
            label = safe_str(entry.get("label")) or k
            unit = safe_str(entry.get("unit"))

            derived_val = get_derived_value(asset, key=k)
            manual_val = get_manual_value(asset, key=k)

            row_left, row_right = st.columns([0.42, 0.58], gap="small")
            with row_left:
                st.write(label)
                if unit:
                    st.caption(unit)
                if label != k:
                    st.caption(k)
            with row_right:
                derived_text = _format_value(derived_val, kind=kind)
                if derived_text:
                    st.caption(f"Derived: {derived_text}")
                widget_key = f"manual_field_{asset_id}_{k}"
                default = _format_value(manual_val, kind=kind)
                st.text_input(
                    "",
                    value=default,
                    placeholder="(blank)",
                    label_visibility="collapsed",
                    key=widget_key,
                    on_change=_apply_manual_change,
                    kwargs={"field_key": k, "kind": kind, "widget_key": widget_key},
                )

        add_left, add_right = st.columns([0.7, 0.3], gap="small")
        with add_left:
            new_key = st.text_input(
                "Add a row",
                value="",
                placeholder="e.g. electricity_kwh_used, electricity_kwh_generated, oil_liters",
                label_visibility="collapsed",
                key=f"add_field_key_{safe_str(asset.get('_id'))}",
            )
        with add_right:
            if st.button("Add", use_container_width=True, disabled=not (new_key or "").strip()):
                k_new = (new_key or "").strip()
                if k_new in fields:
                    st.warning("That key already exists.")
                else:
                    _ensure_field(fields, k_new)
                    st.success("Row added.")
                    st.rerun()

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
        # Table header
        h1, h2, h3, h4, h5, h6 = st.columns([0.18, 0.40, 0.13, 0.09, 0.10, 0.10], gap="small")
        with h1:
            st.caption("Name")
        with h2:
            st.caption("Description")
        with h3:
            st.caption("Saving")
        with h4:
            st.caption("Type")
        with h5:
            st.caption("Ignore")
        with h6:
            st.caption("Actions")

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

            c1, c2, c3, c4, c5, c6 = st.columns([0.18, 0.40, 0.13, 0.09, 0.10, 0.10], gap="small")
            with c1:
                st.write(title)
            with c2:
                st.caption(desc)
            with c3:
                st.write(f"{saving:.2f}")
            with c4:
                st.write(action)
            with c5:
                st.checkbox(
                    "Ignore",
                    value=bool(status.get("ignored")),
                    key=ignore_key,
                    label_visibility="collapsed",
                    on_change=_apply_rec_ignore,
                    kwargs={"rec": r, "rec_key": rid},
                )
            with c6:
                b_left, b_right = st.columns([0.55, 0.45], gap="small")
                with b_left:
                    st.checkbox(
                        "Done",
                        value=bool(status.get("done")),
                        key=done_key,
                        label_visibility="collapsed",
                        on_change=_apply_rec_action,
                        kwargs={"rec": r, "rec_key": rid},
                    )
                with b_right:
                    if bool(status.get("done")) or bool(status.get("applied")):
                        st.button(
                            "Bin",
                            key=f"rec_bin_{asset_id}_{rid}",
                            help="Delete this saved saving (clears Done and undoes applied changes if any).",
                            on_click=_delete_rec_saving,
                            kwargs={"rec": r, "rec_key": rid},
                            use_container_width=True,
                        )
