from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Optional

import streamlit as st

from decarbonify import auth
from decarbonify.portfolio_index import index_portfolio
from decarbonify.portfolio_io import (
    as_list,
    ensure_asset_ids,
    load_portfolio_from_bytes,
    load_portfolio_from_path,
    safe_str,
)


# Backward-compat shim: some deployments may not yet have ensure_asset_data_fields.
try:
    from decarbonify.portfolio_io import ensure_asset_data_fields  # type: ignore
except Exception:  # pragma: no cover
    def ensure_asset_data_fields(portfolio: Dict[str, Any]) -> None:  # type: ignore
        def walk(assets: Any) -> None:
            if not isinstance(assets, list):
                return
            for asset in assets:
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


# Backward-compat shim: some deployments may not yet have ensure_asset_ontology_fields.
try:
    from decarbonify.portfolio_io import ensure_asset_ontology_fields  # type: ignore
except Exception:  # pragma: no cover
    def ensure_asset_ontology_fields(portfolio: Dict[str, Any]) -> None:  # type: ignore
        return
from decarbonify.portfolio_reorder import PortfolioReorderError, can_move_preorder, move_preorder
from decarbonify.recommendations import openai_client_available
from decarbonify.state_store import load_portfolio_state, save_portfolio_state
from decarbonify.ontology import CORE_TYPES, normalize_core_type
from decarbonify.portfolio_edit import add_child_asset, explain_disallowed_child_assets, remove_asset_snapshot
from decarbonify.ui_asset_detail import render_asset_detail_and_recommendations
from decarbonify.ui_chat import render_chat
from decarbonify.ui_sidebar import inject_sidebar_nowrap_css, render_asset_hierarchy_sidebar


DEFAULT_PORTFOLIO_PATH = "portfolio.json"


def _configure_openai_env_from_streamlit_secrets() -> None:
    """Populate OpenAI env vars from Streamlit secrets (local or Streamlit Cloud).

    The OpenAI Python client reads OPENAI_API_KEY from the environment by default.
    """

    if os.environ.get("OPENAI_API_KEY"):
        return

    try:
        key = None
        if "OPENAI_API_KEY" in st.secrets:
            key = str(st.secrets.get("OPENAI_API_KEY") or "").strip()
        else:
            openai_section = st.secrets.get("openai")
            if openai_section is not None and hasattr(openai_section, "get"):
                key = str(openai_section.get("api_key") or "").strip()

        # Fallback: some users paste these into the [google] section.
        if not key:
            google_section = st.secrets.get("google")
            if google_section is not None and hasattr(google_section, "get"):
                key = str(google_section.get("OPENAI_API_KEY") or "").strip()

        if key:
            os.environ["OPENAI_API_KEY"] = key

        if not os.environ.get("OPENAI_MODEL"):
            model = None
            if "OPENAI_MODEL" in st.secrets:
                model = str(st.secrets.get("OPENAI_MODEL") or "").strip()
            else:
                openai_section = st.secrets.get("openai")
                if openai_section is not None and hasattr(openai_section, "get"):
                    model = str(openai_section.get("model") or "").strip()

            if not model:
                google_section = st.secrets.get("google")
                if google_section is not None and hasattr(google_section, "get"):
                    model = str(google_section.get("OPENAI_MODEL") or "").strip()

            if model:
                os.environ["OPENAI_MODEL"] = model
    except Exception:
        # Secrets might not be configured; env vars may be used instead.
        return


def _portfolio_storage_key(*, source: str, uploaded_name: Optional[str]) -> str:
    if source == "Upload JSON" and uploaded_name:
        return f"upload::{uploaded_name}"
    return f"path::{DEFAULT_PORTFOLIO_PATH}"


def _portfolio_fingerprint(portfolio: Dict[str, Any]) -> str:
    try:
        return str(hash(json.dumps(portfolio, sort_keys=True, ensure_ascii=False)))
    except Exception:
        return str(id(portfolio))


def _deepcopy_jsonable(value: Any) -> Any:
    # Good enough for this app's JSON-shaped portfolio.
    return json.loads(json.dumps(value, ensure_ascii=False))


@st.cache_data(show_spinner=False)
def _load_default_portfolio() -> Dict[str, Any]:
    if os.path.exists(DEFAULT_PORTFOLIO_PATH):
        return load_portfolio_from_path(DEFAULT_PORTFOLIO_PATH)

    return {
        "portfolio_name": "Example Portfolio",
        "assets": [
            {
                "name": "Heelands Site",
                "core_type": "place",
                "subtype": "site",
                "current_role": "passive",
                "location": "Heelands",
                "quantity": 1,
                "attributes": {},
                "assets": [
                    {
                        "name": "Heelands Meeting Centre",
                        "core_type": "place",
                        "subtype": "building",
                        "current_role": "passive",
                        "location": "Heelands Site",
                        "quantity": 1,
                        "attributes": {},
                        "assets": [
                            {
                                "name": "Kitchen",
                                "core_type": "place",
                                "subtype": "kitchen",
                                "current_role": "passive",
                                "location": "Heelands Meeting Centre",
                                "quantity": 1,
                                "attributes": {},
                            },
                            {
                                "name": "Gas Boiler",
                                "core_type": "energy_system",
                                "subtype": "boiler",
                                "current_role": "converter",
                                "location": "Heelands Meeting Centre",
                                "quantity": 1,
                                "attributes": {"fuel": "gas"},
                                "fuel": "gas",
                            },
                        ],
                    },
                    {
                        "name": "Solar Panels",
                        "core_type": "energy_system",
                        "subtype": "solar_pv",
                        "current_role": "producer",
                        "location": "Heelands Site",
                        "quantity": 1,
                        "attributes": {},
                    },
                ],
            },
            {
                "name": "Football Field",
                "core_type": "place",
                "subtype": "land",
                "current_role": "passive",
                "location": "",
                "quantity": 1,
                "attributes": {},
                "assets": [
                    {
                        "name": "Floodlights",
                        "core_type": "asset",
                        "subtype": "lighting",
                        "current_role": "consumer",
                        "location": "Football Field",
                        "quantity": None,
                        "attributes": {},
                    }
                ],
            },
        ],
    }


st.set_page_config(layout="wide", initial_sidebar_state="collapsed")
inject_sidebar_nowrap_css()
_configure_openai_env_from_streamlit_secrets()

# Reduce the default top/bottom padding so more content fits above the fold.
st.markdown(
    """
<style>
div.block-container { padding-top: 0.75rem; padding-bottom: 1rem; }

/* Mobile: a bit tighter padding and let sidebar labels wrap */
@media (max-width: 768px) {
  div.block-container { padding-left: 0.75rem; padding-right: 0.75rem; }
  section[data-testid="stSidebar"] .stRadio [role="radiogroup"] label,
  section[data-testid="stSidebar"] .stRadio [role="radiogroup"] label p,
  section[data-testid="stSidebar"] .stRadio [role="radiogroup"] label div {
      white-space: normal;
      overflow: visible;
      text-overflow: clip;
  }
}
</style>
""",
    unsafe_allow_html=True,
)

# Login gate
user_email = auth.require_login(app_name="Decarbonify")
st.session_state.auth_user_email = user_email

google_cfg = auth.google_config()
refresh_token = auth.current_refresh_token()

# Load controls live in the sidebar to reduce top-of-page whitespace.
with st.sidebar:
    with st.expander("Load portfolio", expanded=False):
        source = st.radio(
            "Source",
            ["Use default portfolio.json", "Upload JSON"],
            horizontal=False,
            label_visibility="collapsed",
        )

        uploaded = None
        if source == "Upload JSON":
            uploaded = st.file_uploader("Portfolio JSON", type=["json"], accept_multiple_files=False)

        st.caption("Optional: set OPENAI_API_KEY for AI recommendations.")


try:
    if source == "Upload JSON" and uploaded is not None:
        loaded_portfolio = load_portfolio_from_bytes(uploaded.getvalue())
    else:
        loaded_portfolio = _load_default_portfolio()
except Exception as exc:
    st.error(str(exc))
    st.stop()

portfolio_name_loaded = safe_str(loaded_portfolio.get("portfolio_name")) or "Portfolio"
storage_key = _portfolio_storage_key(source=source, uploaded_name=(uploaded.name if uploaded is not None else None))
st.session_state.portfolio_storage_key = storage_key
st.session_state.portfolio_storage_name = portfolio_name_loaded


# Keep an editable in-memory portfolio (no persistence) so we can reorder.
if (
    "portfolio" not in st.session_state
    or st.session_state.get("portfolio_source") != source
    or (source == "Upload JSON" and uploaded is not None and st.session_state.get("uploaded_name") != uploaded.name)
):
    # Prefer a previously-saved Drive state for this user/portfolio.
    restored_portfolio, _restore_msg = load_portfolio_state(
        cfg=google_cfg,
        refresh_token=refresh_token,
        user_key=user_email,
        portfolio_key=storage_key,
        portfolio_name=portfolio_name_loaded,
    )
    if restored_portfolio is not None:
        st.session_state.portfolio = _deepcopy_jsonable(restored_portfolio)
    else:
        if _restore_msg and _restore_msg != "No saved Drive state found":
            warn_key = "drive_restore_schema_warning_shown"
            if not st.session_state.get(warn_key):
                st.session_state[warn_key] = True
                st.warning(_restore_msg)
        st.session_state.portfolio = _deepcopy_jsonable(loaded_portfolio)
    st.session_state.portfolio_source = source
    st.session_state.uploaded_name = uploaded.name if uploaded is not None else None
    st.session_state.asset_tree_initialized = False
    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1


portfolio: Dict[str, Any] = st.session_state.portfolio


def _refresh_only() -> None:
    st.session_state.asset_tree_initialized = False
    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
    st.rerun()

# Ensure stable ids exist for all assets (idempotent).
ensure_asset_ids(portfolio, id_key="_id")
# Ensure data_fields schema exists for all assets (idempotent).
ensure_asset_data_fields(portfolio)
# Ensure optional ontology fields exist for all assets (idempotent).
ensure_asset_ontology_fields(portfolio)

nodes, node_by_id = index_portfolio(portfolio)

current_fp = _portfolio_fingerprint(portfolio)
if st.session_state.get("portfolio_fp") != current_fp:
    st.session_state.portfolio_fp = current_fp
    st.session_state.asset_tree_initialized = False
    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1

if "selected_node_id" not in st.session_state:
    st.session_state.selected_node_id = nodes[0].node_id if nodes else ""
elif st.session_state.selected_node_id and st.session_state.selected_node_id not in node_by_id:
    st.session_state.selected_node_id = nodes[0].node_id if nodes else ""


with st.sidebar:
    st.markdown("## Portfolio Carbon Insight Tool")

    portfolio_name = safe_str(portfolio.get("portfolio_name"))
    if portfolio_name:
        st.markdown(f"### {portfolio_name}")
    if openai_client_available():
        st.caption("AI: enabled")
    else:
        st.caption("AI: disabled (set OPENAI_API_KEY to enable)")

    tree_key = f"asset_tree_{int(st.session_state.get('asset_tree_nonce', 0))}"

    selected_node_id, _changed = render_asset_hierarchy_sidebar(
        portfolio=portfolio,
        nodes=nodes,
        node_by_id=node_by_id,
        selected_node_id=str(st.session_state.selected_node_id),
        tree_key=tree_key,
    )
    st.session_state.selected_node_id = selected_node_id

    if st.session_state.selected_node_id:
        can_up = can_move_preorder(portfolio, node_id=st.session_state.selected_node_id, direction=-1)
        can_down = can_move_preorder(portfolio, node_id=st.session_state.selected_node_id, direction=1)
        up_col, down_col = st.columns(2, gap="small")
        with up_col:
            if st.button("Up", use_container_width=True, disabled=not can_up):
                try:
                    move_preorder(portfolio, node_id=st.session_state.selected_node_id, direction=-1)

                    st.caption("Reorder applied locally. Click 'Save to Drive' to persist.")

                    st.session_state.asset_tree_initialized = False
                    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                    st.rerun()
                except PortfolioReorderError as exc:
                    st.error(str(exc))

        with down_col:
            if st.button("Down", use_container_width=True, disabled=not can_down):
                try:
                    move_preorder(portfolio, node_id=st.session_state.selected_node_id, direction=1)

                    st.caption("Reorder applied locally. Click 'Save to Drive' to persist.")

                    st.session_state.asset_tree_initialized = False
                    st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
                    st.rerun()
                except PortfolioReorderError as exc:
                    st.error(str(exc))

    # Explicit save control (in addition to auto-save on changes)
    if st.button("Save to Drive", use_container_width=True):
        ok, msg = save_portfolio_state(
            cfg=google_cfg,
            refresh_token=refresh_token,
            user_key=user_email,
            portfolio_key=st.session_state.get("portfolio_storage_key", storage_key),
            portfolio_name=safe_str(st.session_state.get("portfolio_storage_name", portfolio_name_loaded)),
            portfolio=portfolio,
        )
        if ok:
            st.success(msg)
        else:
            st.warning("Not saved to Drive: " + msg)


selected_node = node_by_id.get(st.session_state.selected_node_id)


with st.sidebar:
    st.markdown("### Defaults")

    defaults = portfolio.get("defaults") if isinstance(portfolio, dict) else None
    if not isinstance(defaults, dict):
        defaults = {}
        portfolio["defaults"] = defaults

    with st.expander("View defaults", expanded=False):
        st.caption(
            "These portfolio-level values are used by asset-type formulas when an input is missing on an asset. "
            "You can override per-asset by filling a manual value for the same key."
        )

        ci_elec = st.number_input(
            "Electricity carbon intensity (kgCO2e/kWh)",
            min_value=0.0,
            value=float(defaults.get("carbon_intensity_of_electricity", 0.2) or 0.2),
            step=0.01,
            key="defaults_ci_electricity",
        )
        defaults["carbon_intensity_of_electricity"] = float(ci_elec)

        ci_gas = st.number_input(
            "Gas carbon intensity (kgCO2e/kWh)",
            min_value=0.0,
            value=float(defaults.get("gas_kgco2e_per_kwh", 0.184) or 0.184),
            step=0.001,
            key="defaults_ci_gas",
        )
        defaults["gas_kgco2e_per_kwh"] = float(ci_gas)

        ci_oil = st.number_input(
            "Heating oil carbon intensity (kgCO2e/kWh)",
            min_value=0.0,
            value=float(defaults.get("heating_oil_kgco2e_per_kwh", 0.249) or 0.249),
            step=0.001,
            key="defaults_ci_heating_oil",
        )
        defaults["heating_oil_kgco2e_per_kwh"] = float(ci_oil)

    st.divider()
    st.markdown("### Selected asset")

    if not selected_node:
        st.caption("Select an asset to enable actions.")
    else:
        asset = selected_node.data
        asset_id = safe_str(asset.get("_id"))
        asset_name = safe_str(asset.get("name")) or "Asset"

        st.caption(asset_name)

        with st.expander("Add child", expanded=False):
            if not asset_id:
                st.warning("This asset is missing an _id, so children can't be added here yet.")
            else:
                name_key = f"sb_add_child_name::{asset_id}"
                ct_key = f"sb_add_child_core_type::{asset_id}"
                st_key = f"sb_add_child_subtype::{asset_id}"
                desc_key = f"sb_add_child_desc::{asset_id}"

                child_name = st.text_input("Name", key=name_key, label_visibility="collapsed", placeholder="Name")
                ct_options = ["asset"] + [t for t in CORE_TYPES if t != "asset"]
                child_core_type = st.selectbox(
                    "Core type",
                    ct_options,
                    index=0,
                    key=ct_key,
                    label_visibility="collapsed",
                )
                child_subtype = st.text_input(
                    "Subtype",
                    value="",
                    key=st_key,
                    label_visibility="collapsed",
                    placeholder="Subtype (optional)",
                )
                child_desc = st.text_input(
                    "Description (optional)",
                    key=desc_key,
                    label_visibility="collapsed",
                    placeholder="Description (optional)",
                )

                if st.button(
                    "Add",
                    disabled=not (child_name or "").strip(),
                    key=f"sb_add_child_btn::{asset_id}",
                ):
                    new_asset: Dict[str, Any] = {
                        "name": (child_name or "").strip(),
                        "core_type": normalize_core_type(child_core_type),
                        "subtype": (child_subtype or "").strip(),
                    }
                    if (child_desc or "").strip():
                        new_asset["description"] = child_desc.strip()

                    disallowed = explain_disallowed_child_assets(parent_asset=asset, child_asset=new_asset)
                    if disallowed:
                        st.error(disallowed)
                    else:
                        ok_add = add_child_asset(portfolio, parent_id=asset_id, child_asset=new_asset)
                        if not ok_add:
                            st.error("Couldn't add the child asset.")
                        else:
                            ensure_asset_ids(portfolio, id_key="_id")
                            ensure_asset_data_fields(portfolio)
                            ensure_asset_ontology_fields(portfolio)
                            for k in (name_key, ct_key, st_key, desc_key):
                                st.session_state.pop(k, None)
                            _refresh_only()

        with st.expander("Delete", expanded=False):
            if not asset_id:
                st.warning("This asset is missing an _id, so it can't be deleted safely.")
            else:
                confirm_key = f"sb_delete_confirm::{asset_id}"
                confirmed = bool(
                    st.checkbox(
                        "Confirm delete",
                        key=confirm_key,
                        help="This cannot be undone.",
                    )
                )
                if st.button(
                    "Delete",
                    type="primary",
                    disabled=not confirmed,
                    key=f"sb_delete_btn::{asset_id}",
                ):
                    snap = remove_asset_snapshot(portfolio, asset_id=asset_id)
                    if not snap:
                        st.error("Couldn't delete the asset (not found).")
                    else:
                        st.session_state.selected_node_id = safe_str(snap.parent_id) or ""
                        st.session_state.pop(confirm_key, None)
                        _refresh_only()

    st.divider()
    # Bottom area: signed-in info + logout
    email = auth.current_user()
    if email:
        profile = auth.current_user_profile() or {}
        display = safe_str(profile.get("name")) or email
        st.caption(f"Signed in as: {display}")
        if st.button("Logout"):
            auth.logout()
            st.rerun()

if not selected_node:
    st.subheader("Asset Detail")
    st.info("Select an asset to view details.")

    with st.expander("Add root asset", expanded=False):
        root_name = st.text_input("Name", key="add_root_name")
        ct_options = ["asset"] + [t for t in CORE_TYPES if t != "asset"]
        root_core_type = st.selectbox("Core type", ct_options, index=0, key="add_root_core_type")
        root_subtype = st.text_input("Subtype", value="", key="add_root_subtype")
        root_desc = st.text_input("Description (optional)", key="add_root_desc")
        if st.button("Add root", type="primary", disabled=not (root_name or "").strip()):
            roots = portfolio.get("assets")
            if not isinstance(roots, list):
                roots = []
                portfolio["assets"] = roots

            new_id = str(uuid.uuid4())
            new_asset: Dict[str, Any] = {
                "_id": new_id,
                "name": (root_name or "").strip(),
                "core_type": normalize_core_type(root_core_type),
                "subtype": (root_subtype or "").strip(),
            }
            if (root_desc or "").strip():
                new_asset["description"] = root_desc.strip()
            roots.append(new_asset)
            ensure_asset_ids(portfolio, id_key="_id")
            ensure_asset_data_fields(portfolio)
            ensure_asset_ontology_fields(portfolio)
            ensure_asset_ontology_fields(portfolio)

            st.session_state.selected_node_id = new_id
            st.session_state.asset_tree_initialized = False
            st.session_state.asset_tree_nonce = int(st.session_state.get("asset_tree_nonce", 0)) + 1
            for k in ("add_root_name", "add_root_core_type", "add_root_subtype", "add_root_desc"):
                st.session_state.pop(k, None)
            st.rerun()
else:
    render_asset_detail_and_recommendations(portfolio=portfolio, selected_node=selected_node)

st.divider()
render_chat(portfolio=portfolio, nodes=nodes, selected_node=selected_node)
