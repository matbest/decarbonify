from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Mapping, Optional, Tuple

import streamlit as st

from .drive_store import download_json_file, find_or_create_folder, refresh_access_token, upload_json_file
from .emissions import USER_OVERRIDE_FIELD
from .portfolio_io import as_list
from .portfolio_io import safe_str


_DRIVE_TOKEN_KEY = "drive_access_token"
_DRIVE_TOKEN_EXP_KEY = "drive_access_token_exp"


def _slug(s: str) -> str:
    out = []
    for ch in (s or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in {"-", "_"}:
            out.append(ch)
        else:
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "portfolio"


def portfolio_state_filename(*, portfolio_key: str, portfolio_name: str) -> str:
    base = f"{portfolio_key}::{portfolio_name}"
    h = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    return f"decarbonify_{_slug(portfolio_name)}_{h}.json"


def _get_access_token(*, cfg: Dict[str, Any], refresh_token: str) -> str:
    now = int(time.time())
    token = safe_str(st.session_state.get(_DRIVE_TOKEN_KEY))
    exp = int(st.session_state.get(_DRIVE_TOKEN_EXP_KEY, 0) or 0)
    if token and exp > now + 15:
        return token

    new_token, new_exp = refresh_access_token(cfg=cfg, refresh_token=refresh_token)
    st.session_state[_DRIVE_TOKEN_KEY] = new_token
    st.session_state[_DRIVE_TOKEN_EXP_KEY] = int(new_exp)
    return new_token


def drive_enabled(cfg: Mapping[str, Any]) -> bool:
    raw = cfg.get("drive_enabled")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def drive_folder_name(cfg: Mapping[str, Any]) -> str:
    return (safe_str(cfg.get("drive_folder")) or "Decarbonify").strip() or "Decarbonify"


def load_portfolio_state(
    *,
    cfg: Dict[str, Any],
    refresh_token: str,
    user_key: str,
    portfolio_key: str,
    portfolio_name: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Load a saved portfolio state (portfolio + overrides) from Google Drive.

        Returns: (portfolio_dict_or_none, status_message)
            - status_message is empty on success, otherwise a short reason
    """

    if not drive_enabled(cfg):
        return None, "Drive persistence disabled"
    if not refresh_token:
        return None, "No refresh token (re-login to grant Drive access)"

    try:
        access_token = _get_access_token(cfg=cfg, refresh_token=refresh_token)
        folder_id = find_or_create_folder(access_token=access_token, folder_name=drive_folder_name(cfg))
        filename = portfolio_state_filename(portfolio_key=portfolio_key, portfolio_name=portfolio_name)
        doc = download_json_file(access_token=access_token, folder_id=folder_id, file_name=filename)
        if not doc:
            return None, "No saved Drive state found"

        if isinstance(doc.get("portfolio"), dict):
            portfolio = dict(doc.get("portfolio"))
            legacy_overrides = doc.get("emissions_overrides")
        else:
            # Back-compat: treat file as raw portfolio.
            portfolio = dict(doc)
            legacy_overrides = {}

        # Back-compat: migrate legacy overrides mapping into the portfolio JSON.
        if isinstance(legacy_overrides, dict) and legacy_overrides:
            overrides_clean: Dict[str, float] = {}
            for k, v in legacy_overrides.items():
                if not isinstance(k, str) or not k:
                    continue
                try:
                    overrides_clean[k] = float(v)
                except Exception:
                    continue

            def apply(assets: Any) -> None:
                for a in as_list(assets):
                    if not isinstance(a, dict):
                        continue
                    aid = safe_str(a.get("_id"))
                    if aid and aid in overrides_clean:
                        a[USER_OVERRIDE_FIELD] = overrides_clean[aid]
                    apply(a.get("assets"))

            apply(portfolio.get("assets"))

        # Optionally validate that the file belongs to this user.
        owner = safe_str(doc.get("user"))
        if owner and owner != user_key:
            return None, "Saved file belongs to a different user"

        return portfolio, ""
    except Exception as exc:
        return None, safe_str(exc) or "Drive load failed"


def save_portfolio_state(
    *,
    cfg: Dict[str, Any],
    refresh_token: str,
    user_key: str,
    portfolio_key: str,
    portfolio_name: str,
    portfolio: Dict[str, Any],
    emissions_overrides: Mapping[str, Any],
) -> Tuple[bool, str]:
    """Save portfolio state (portfolio + overrides) to Google Drive (visible file).

    Returns: (ok, message)
    """

    if not drive_enabled(cfg):
        return False, "Drive persistence disabled"
    if not refresh_token:
        return False, "No refresh token (re-login to grant Drive access)"

    try:
        access_token = _get_access_token(cfg=cfg, refresh_token=refresh_token)
        folder_name = drive_folder_name(cfg)
        folder_id = find_or_create_folder(access_token=access_token, folder_name=folder_name)
        filename = portfolio_state_filename(portfolio_key=portfolio_key, portfolio_name=portfolio_name)

        doc: Dict[str, Any] = {
            "user": user_key,
            "portfolio_key": portfolio_key,
            "portfolio_name": portfolio_name,
            "saved_at": int(time.time()),
            "portfolio": portfolio,
        }
        meta = upload_json_file(access_token=access_token, folder_id=folder_id, file_name=filename, obj=doc)
        modified = safe_str(meta.get("modifiedTime"))
        web = safe_str(meta.get("webViewLink"))
        fid = safe_str(meta.get("id"))
        parts = [f"{folder_name}/{filename}"]
        if modified:
            parts.append(f"modifiedTime={modified}")
        if fid:
            parts.append(f"id={fid}")
        msg = "Saved to Google Drive: " + " | ".join(parts)
        if web:
            # Put the URL on its own line so Streamlit is more likely to auto-link it.
            msg += "\n" + web
        return True, msg
    except Exception as exc:
        return False, safe_str(exc) or "Drive save failed"
