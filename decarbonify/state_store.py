from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Mapping, Optional, Tuple

import streamlit as st

from .drive_store import download_json_file, find_or_create_folder, refresh_access_token, upload_json_file
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
) -> Optional[Tuple[Dict[str, Any], Dict[str, float]]]:
    """Load a saved portfolio state (portfolio + overrides) from Google Drive.

    Returns None if not found or not available.
    """

    if not drive_enabled(cfg):
        return None
    if not refresh_token:
        return None

    try:
        access_token = _get_access_token(cfg=cfg, refresh_token=refresh_token)
        folder_id = find_or_create_folder(access_token=access_token, folder_name=drive_folder_name(cfg))
        filename = portfolio_state_filename(portfolio_key=portfolio_key, portfolio_name=portfolio_name)
        doc = download_json_file(access_token=access_token, folder_id=folder_id, file_name=filename)
        if not doc:
            return None

        if isinstance(doc.get("portfolio"), dict):
            portfolio = doc.get("portfolio")
            overrides = doc.get("emissions_overrides")
        else:
            # Back-compat: treat file as raw portfolio.
            portfolio = doc
            overrides = {}

        cleaned_overrides: Dict[str, float] = {}
        if isinstance(overrides, dict):
            for k, v in overrides.items():
                if not isinstance(k, str) or not k:
                    continue
                try:
                    cleaned_overrides[k] = float(v)
                except Exception:
                    continue

        # Optionally validate that the file belongs to this user.
        owner = safe_str(doc.get("user"))
        if owner and owner != user_key:
            return None

        return dict(portfolio), cleaned_overrides
    except Exception:
        return None


def save_portfolio_state(
    *,
    cfg: Dict[str, Any],
    refresh_token: str,
    user_key: str,
    portfolio_key: str,
    portfolio_name: str,
    portfolio: Dict[str, Any],
    emissions_overrides: Mapping[str, Any],
) -> None:
    """Save portfolio state (portfolio + overrides) to Google Drive (visible file)."""

    if not drive_enabled(cfg):
        return
    if not refresh_token:
        return

    try:
        access_token = _get_access_token(cfg=cfg, refresh_token=refresh_token)
        folder_id = find_or_create_folder(access_token=access_token, folder_name=drive_folder_name(cfg))
        filename = portfolio_state_filename(portfolio_key=portfolio_key, portfolio_name=portfolio_name)

        overrides_clean: Dict[str, float] = {}
        for k, v in dict(emissions_overrides).items():
            if not isinstance(k, str) or not k:
                continue
            try:
                overrides_clean[k] = float(v)
            except Exception:
                continue

        doc: Dict[str, Any] = {
            "user": user_key,
            "portfolio_key": portfolio_key,
            "portfolio_name": portfolio_name,
            "saved_at": int(time.time()),
            "portfolio": portfolio,
            "emissions_overrides": overrides_clean,
        }
        upload_json_file(access_token=access_token, folder_id=folder_id, file_name=filename, obj=doc)
    except Exception:
        return
