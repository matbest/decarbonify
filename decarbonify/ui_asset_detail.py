from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Dict, List

import streamlit as st

from .portfolio_index import AssetNode, iter_assets_tree
from .portfolio_io import ensure_asset_data_fields, ensure_asset_ids
from .portfolio_io import safe_str
from . import auth
from .portfolio_edit import (
    RemovedAsset,
    add_child_asset,
    add_sibling_asset_after,
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
    get_derived_value,
    get_manual_value,
    sum_emissions_produced_tco2e_per_year,
)
from .recommendations import carbon_signal, llm_recommendations


def render_asset_detail_and_recommendations(*, portfolio: Dict[str, Any], selected_node: AssetNode) -> None:
    st.subheader("Asset Detail + Recommendations")

    asset = selected_node.data

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

        # Force a sidebar remount so derived/manual totals in labels refresh.
        st.session_state.asset_tree_initialized = False
        st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1

    def _rec_id(rec: Dict[str, Any]) -> str:
        payload = {
            "title": safe_str(rec.get("title")),
            "description": safe_str(rec.get("description")),
            "saving": float(rec.get("estimated_saving_tco2_per_year", 0) or 0),
            "action": safe_str(rec.get("action")),
            "add_asset": rec.get("add_asset"),
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

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

    with st.expander("Data", expanded=False):
        fields = _asset_fields()
        # Ensure emissions exists as a minimum field.
        if EMISSIONS_KEY not in fields:
            entry = _ensure_field(fields, EMISSIONS_KEY)
            entry["label"] = "Emissions"
            entry["kind"] = "number"
            entry.setdefault("unit", "tCO2e/year")

        st.caption("Manual values override derived values. Derived values may come from LLM inference or lookups.")

        key_col, derived_col, manual_col = st.columns([0.42, 0.29, 0.29], gap="small")
        with key_col:
            st.markdown("**JSON key**")
        with derived_col:
            st.markdown("**Derived**")
        with manual_col:
            st.markdown("**Manual Override**")

        asset_id = safe_str(asset.get("_id"))
        keys = _sorted_field_keys(fields)

        for k in keys:
            _ensure_field(fields, k)
            kind = _field_kind(fields, k)
            derived_val = get_derived_value(asset, key=k)
            manual_val = get_manual_value(asset, key=k)

            c1, c2, c3 = st.columns([0.42, 0.29, 0.29], gap="small")
            with c1:
                st.write(k)
            with c2:
                text = _format_value(derived_val, kind=kind)
                # Grey out derived if manual exists.
                if manual_val is not None:
                    st.caption(text or "")
                else:
                    st.write(text or "")
            with c3:
                default = _format_value(manual_val, kind=kind)
                widget_key = f"manual_field_{asset_id}_{k}"
                st.text_input(
                    "",
                    value=default,
                    placeholder="(blank)",
                    label_visibility="collapsed",
                    key=widget_key,
                    on_change=_apply_manual_change,
                    kwargs={"field_key": k, "kind": kind, "widget_key": widget_key},
                )

        # Add row controls (below the table)
        add_left, add_right = st.columns([0.7, 0.3], gap="small")
        with add_left:
            new_key = st.text_input(
                "Add a row",
                value="",
                placeholder="e.g. capacity_kw, volume_liters",
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
                    st.session_state.asset_tree_initialized = False
                    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                    st.rerun()

    total_tco2e, contributing, visited, overrides_used = sum_emissions_produced_tco2e_per_year(asset)
    if contributing > 0:
        st.metric(
            "Estimated CO₂e produced (t/year)",
            f"{total_tco2e:.2f}",
            help=(
                f"Sum of selected asset + {visited - 1} descendants. "
                f"Values present for {contributing} of {visited} assets; overrides used for {overrides_used} assets."
            ),
        )
    else:
        st.info(emissions_field_help_text())

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

    with st.spinner("Generating recommendations..."):
        recs: List[Dict[str, Any]] = llm_recommendations(portfolio, asset)

    # Cache latest recommendations so the sidebar can compute "possible" savings.
    # Store in session_state (not the portfolio) to avoid changing portfolio fingerprint on every rerun.
    rec_cache: List[Dict[str, Any]] = []
    for r in recs or []:
        rid = _rec_id(r)
        try:
            saving = float(r.get("estimated_saving_tco2_per_year", 0) or 0)
        except Exception:
            saving = 0.0
        rec_cache.append(
            {
                "id": rid,
                "title": safe_str(r.get("title")),
                "action": safe_str(r.get("action") or "other").lower(),
                "saving_tco2_per_year": saving,
            }
        )
    asset_id_for_cache = safe_str(asset.get("_id"))
    if asset_id_for_cache:
        cache_map = st.session_state.get("recommendations_cache_by_asset_id")
        if not isinstance(cache_map, dict):
            cache_map = {}
            st.session_state["recommendations_cache_by_asset_id"] = cache_map
        cache_map[asset_id_for_cache] = rec_cache

    if recs:
        status_map = _rec_status_map()

        savings_possible_asset = 0.0
        done_rows: List[Dict[str, Any]] = []
        possible_rows: List[Dict[str, Any]] = []

        for r in recs:
            rid = _rec_id(r)
            title = safe_str(r.get("title"))
            try:
                saving = float(r.get("estimated_saving_tco2_per_year", 0) or 0)
            except Exception:
                saving = 0.0

            action = safe_str(r.get("action") or "other").lower()
            st0 = status_map.get(rid)
            if not isinstance(st0, dict):
                st0 = {}

            if saving != 0:
                savings_possible_asset += saving
                possible_rows.append({"title": title, "saving": saving, "action": action})

            if bool(st0.get("done")):
                done_rows.append({"title": title, "saving": saving, "action": action, "rec": r, "rid": rid})

        savings_done_portfolio = _portfolio_savings_done_total_tco2_per_year()

        with st.expander("Estimated savings", expanded=False):
            m1, m2 = st.columns(2, gap="small")
            with m1:
                st.metric("Estimated savings possible (this asset, t/year)", f"{savings_possible_asset:.2f}")
            with m2:
                st.metric("Savings done so far (t/year)", f"{savings_done_portfolio:.2f}")

            if possible_rows:
                st.markdown("**Savings opportunities (titles)**")
                for row in possible_rows:
                    st.write(f"- {row['title']} ({row['saving']:.2f} t/year, {row['action']})")

            if done_rows:
                st.markdown("**Savings done (ticked)**")
                for row in done_rows:
                    left, right = st.columns([0.88, 0.12], gap="small")
                    with left:
                        st.write(f"{row['title']} — {row['saving']:.2f} t/year")
                    with right:
                        st.button(
                            "Bin",
                            key=f"rec_bin_savings_{safe_str(asset.get('_id'))}_{row['rid']}",
                            help="Delete this saved saving (clears Done and undoes applied changes if any).",
                            on_click=_delete_rec_saving,
                            kwargs={"rec": row["rec"], "rec_key": row["rid"]},
                            use_container_width=True,
                        )

    st.markdown(f"**Path:** {selected_node.path}")
    st.markdown(f"**Type:** {selected_node.type}")
    st.markdown(f"**Carbon effect (qualitative):** {carbon_signal(asset)}")

    with st.expander("Asset JSON", expanded=False):
        st.json(asset)

    st.markdown("### Recommendations")
    if not recs:
        st.write("No recommendations for this asset.")
        return

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
                    st.warning("Could not add asset (parent not found).")
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

    # Table header
    h1, h2, h3, h4, h5 = st.columns([0.20, 0.44, 0.14, 0.10, 0.12], gap="small")
    with h1:
        st.markdown("**Name**")
    with h2:
        st.markdown("**Description**")
    with h3:
        st.markdown("**Saving**")
    with h4:
        st.markdown("**Type**")
    with h5:
        st.markdown("**Actions**")

    status_map = _rec_status_map()
    asset_id = safe_str(asset.get("_id"))
    for r in recs:
        rid = _rec_id(r)
        status = status_map.get(rid)
        if not isinstance(status, dict):
            status = {"done": False, "applied": False}
            status_map[rid] = status

        title = safe_str(r.get("title"))
        desc = safe_str(r.get("description"))
        saving = float(r.get("estimated_saving_tco2_per_year", 0) or 0)
        action = safe_str(r.get("action") or "other").lower()
        if action in {"add", "remove", "switch"}:
            if not desc:
                desc = f"Action: {action}"
        done_key = f"rec_done_{asset_id}_{rid}"

        c1, c2, c3, c4, c5 = st.columns([0.20, 0.44, 0.14, 0.10, 0.12], gap="small")
        with c1:
            st.write(title)
        with c2:
            st.write(desc)
        with c3:
            st.write(f"{saving:.2f}")
        with c4:
            st.write(action)
        with c5:
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
