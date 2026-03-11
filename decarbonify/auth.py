from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import streamlit as st


_GOOGLE_USER_KEY = "google_user"
_OAUTH_REDIRECTING_KEY = "oauth_redirecting"
_AUTH_COOKIE_NAME = "decarbonify_auth"
_COOKIE_MANAGER_KEY = "decarbonify_cookie_manager"
_LAST_OAUTH_CODE_KEY = "last_oauth_code"
_COOKIE_BOOTSTRAP_KEY = "cookie_bootstrapped"
_LOGOUT_SUPPRESS_RESTORE_UNTIL_KEY = "logout_suppress_restore_until"

_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    message: str = ""


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _get_query_params() -> Dict[str, List[str]]:
    # Streamlit has two APIs depending on version.
    if hasattr(st, "query_params"):
        raw = dict(st.query_params)  # type: ignore[attr-defined]
        out: Dict[str, List[str]] = {}
        for k, v in raw.items():
            if v is None:
                out[str(k)] = []
            elif isinstance(v, list):
                out[str(k)] = [str(x) for x in v]
            else:
                out[str(k)] = [str(v)]
        return out
    return {k: [str(x) for x in v] for k, v in st.experimental_get_query_params().items()}


def _clear_query_params() -> None:
    if hasattr(st, "query_params"):
        st.query_params.clear()  # type: ignore[attr-defined]
        return
    st.experimental_set_query_params()


def _get_google_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}

    # 1) Streamlit Secrets (preferred on Streamlit Community Cloud)
    try:
        secrets_cfg = st.secrets.get("google", {})
        if isinstance(secrets_cfg, Mapping):
            cfg.update(dict(secrets_cfg))
    except Exception:
        pass

    # 2) Environment variables (useful for local dev without a secrets file)
    # Note: allowed lists can be comma-separated.
    env_map = {
        "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
        "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
        "redirect_uri": os.environ.get("GOOGLE_REDIRECT_URI"),
        "state_secret": os.environ.get("GOOGLE_STATE_SECRET"),
    }
    for k, v in env_map.items():
        if v:
            cfg[k] = v

    if "allowed_domains" not in cfg:
        raw = os.environ.get("GOOGLE_ALLOWED_DOMAINS")
        if raw:
            cfg["allowed_domains"] = [x.strip() for x in raw.split(",") if x.strip()]

    if "allowed_emails" not in cfg:
        raw = os.environ.get("GOOGLE_ALLOWED_EMAILS")
        if raw:
            cfg["allowed_emails"] = [x.strip() for x in raw.split(",") if x.strip()]

    return cfg


def google_config() -> Dict[str, Any]:
    """Public accessor for the merged Google config (secrets + env)."""

    return _get_google_config()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def _state_signing_key(cfg: Dict[str, Any]) -> str:
    # Prefer a dedicated state secret; fall back to client_secret.
    key = _safe_str(cfg.get("state_secret")) or _safe_str(cfg.get("client_secret"))
    if not key:
        raise RuntimeError("Missing google.client_secret (or google.state_secret) for OAuth state signing")
    return key


def _cookie_signing_key(cfg: Dict[str, Any]) -> str:
    # Use the same key material as OAuth state signing by default.
    return _state_signing_key(cfg)


def _cookie_ttl_days(cfg: Dict[str, Any]) -> int:
    raw = cfg.get("session_days")
    try:
        days = int(raw)
    except Exception:
        days = 14
    return max(1, min(days, 90))


def _b64url_json(payload: Dict[str, Any]) -> str:
    return _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _sign_b64(payload_b64: str, *, key: str) -> str:
    return hmac.new(key.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()


def _create_auth_cookie_value(profile: Dict[str, Any], *, cfg: Dict[str, Any]) -> str:
    now = int(time.time())
    exp = now + _cookie_ttl_days(cfg) * 86400
    payload = {
        "email": _safe_str(profile.get("email")).lower(),
        "name": _safe_str(profile.get("name")),
        "picture": _safe_str(profile.get("picture")),
        "rt": _safe_str(profile.get("refresh_token")),
        "iat": now,
        "exp": exp,
    }
    payload_b64 = _b64url_json(payload)
    sig = _sign_b64(payload_b64, key=_cookie_signing_key(cfg))
    return f"{payload_b64}.{sig}"


def _parse_auth_cookie_value(value: str, *, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        payload_b64, sig = (value or "").split(".", 1)
        expected = _sign_b64(payload_b64, key=_cookie_signing_key(cfg))
        if not secrets.compare_digest(expected, sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        exp = int(payload.get("exp", 0) or 0)
        if exp <= int(time.time()):
            return None
        email = _safe_str(payload.get("email")).lower()
        if not email:
            return None
        return {
            "email": email,
            "name": _safe_str(payload.get("name")),
            "picture": _safe_str(payload.get("picture")),
            "refresh_token": _safe_str(payload.get("rt")),
        }
    except Exception:
        return None


def _cookie_manager():
    try:
        import extra_streamlit_components as stx  # type: ignore

        return stx.CookieManager(key=_COOKIE_MANAGER_KEY)
    except Exception:
        return None


def _restore_user_from_cookie(*, cfg: Dict[str, Any]) -> bool:
    cm = _cookie_manager()
    if cm is None:
        return False
    try:
        value = cm.get(_AUTH_COOKIE_NAME)
    except Exception:
        value = None
    if not isinstance(value, str) or not value:
        return False
    profile = _parse_auth_cookie_value(value, cfg=cfg)
    if not profile:
        return False
    if not _allowed_email(_safe_str(profile.get("email"))):
        return False
    st.session_state[_GOOGLE_USER_KEY] = profile
    return True


def _persist_user_cookie(profile: Dict[str, Any], *, cfg: Dict[str, Any]) -> None:
    cm = _cookie_manager()
    if cm is None:
        return
    try:
        value = _create_auth_cookie_value(profile, cfg=cfg)
        expires_at = _dt.datetime.utcnow() + _dt.timedelta(days=_cookie_ttl_days(cfg))
        cm.set(_AUTH_COOKIE_NAME, value, expires_at=expires_at)
    except Exception:
        # Best-effort; auth still works without persistence.
        return


def _clear_user_cookie(*, cfg: Dict[str, Any]) -> None:
    cm = _cookie_manager()
    if cm is None:
        return
    try:
        # Some cookie managers/browsers behave better with an explicit expiry in the past.
        expires_at = _dt.datetime.utcnow() - _dt.timedelta(days=2)
        try:
            cm.set(_AUTH_COOKIE_NAME, "", expires_at=expires_at)
        except Exception:
            pass

        cm.delete(_AUTH_COOKIE_NAME)
    except Exception:
        return


def _create_oauth_state(*, cfg: Dict[str, Any]) -> str:
    payload = {"n": secrets.token_urlsafe(16), "ts": int(time.time())}
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    key = _state_signing_key(cfg).encode("utf-8")
    sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def _verify_oauth_state(state: str, *, cfg: Dict[str, Any], max_age_seconds: int = 600) -> bool:
    try:
        payload_b64, sig = (state or "").split(".", 1)
        key = _state_signing_key(cfg).encode("utf-8")
        expected = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(expected, sig):
            return False
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if not isinstance(payload, dict):
            return False
        ts = int(payload.get("ts", 0) or 0)
        now = int(time.time())
        return ts > 0 and (now - ts) <= max_age_seconds
    except Exception:
        return False


def _allowed_email(email: str) -> bool:
    cfg = _get_google_config()
    allowed_emails = cfg.get("allowed_emails")
    allowed_domains = cfg.get("allowed_domains")

    email = (email or "").strip().lower()
    if not email:
        return False

    if isinstance(allowed_emails, list) and allowed_emails:
        allowed = {str(x).strip().lower() for x in allowed_emails}
        return email in allowed

    if isinstance(allowed_domains, list) and allowed_domains:
        domain = email.split("@", 1)[-1]
        allowed = {str(x).strip().lower().lstrip("@") for x in allowed_domains}
        return domain in allowed

    # Default: allow any Google account.
    return True


def is_authenticated() -> bool:
    return bool(st.session_state.get(_GOOGLE_USER_KEY))


def current_user() -> Optional[str]:
    user = st.session_state.get(_GOOGLE_USER_KEY)
    if isinstance(user, dict):
        return _safe_str(user.get("email")) or None
    return None


def current_user_profile() -> Optional[Dict[str, Any]]:
    user = st.session_state.get(_GOOGLE_USER_KEY)
    return user if isinstance(user, dict) else None


def current_refresh_token() -> str:
    profile = current_user_profile() or {}
    return _safe_str(profile.get("refresh_token"))


def logout() -> None:
    cfg = _get_google_config()
    # Prevent immediate re-auth via cookie restore during the next rerun(s).
    st.session_state[_LOGOUT_SUPPRESS_RESTORE_UNTIL_KEY] = int(time.time()) + 20
    _clear_user_cookie(cfg=cfg)
    for key in [
        _GOOGLE_USER_KEY,
        _OAUTH_REDIRECTING_KEY,
        _LAST_OAUTH_CODE_KEY,
        _COOKIE_BOOTSTRAP_KEY,
        "selected_node_id",
        "asset_tree_initialized",
        "asset_tree_last_key",
        "asset_tree_nonce",
        "chat_messages",
        "auth_user_email",
        "portfolio",
        "portfolio_source",
        "uploaded_name",
        "portfolio_fp",
        "emissions_overrides",
        "emissions_overrides_user",
        "emissions_overrides_loaded",
        "portfolio_storage_key",
        "portfolio_storage_name",
        "drive_access_token",
        "drive_access_token_exp",
    ]:
        if key in st.session_state:
            del st.session_state[key]
    _clear_query_params()


def _http_json(url: str, *, method: str, data: Optional[Dict[str, str]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    encoded: Optional[bytes] = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    if data is not None:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req = urllib.request.Request(url, data=encoded, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        # Try to parse Google's JSON error structure.
        try:
            parsed = json.loads(body) if body else {}
            if isinstance(parsed, dict):
                err = _safe_str(parsed.get("error"))
                desc = _safe_str(parsed.get("error_description"))
                detail = (err + (": " + desc if desc else "")).strip() or body
                raise RuntimeError(f"HTTP {exc.code} from OAuth endpoint: {detail}") from None
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} from OAuth endpoint: {body or exc.reason}") from None
    except Exception as exc:
        raise RuntimeError(f"HTTP request failed: {exc}") from exc

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    raise RuntimeError("Unexpected response from OAuth endpoint")


def _build_google_auth_url(*, state: str, prompt: str) -> str:
    cfg = _get_google_config()
    client_id = _safe_str(cfg.get("client_id"))
    redirect_uri = _safe_str(cfg.get("redirect_uri"))
    if not client_id or not redirect_uri:
        raise RuntimeError("Missing google.client_id or google.redirect_uri in Streamlit Secrets")

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": f"openid email profile {_DRIVE_SCOPE}",
        "state": state,
        "prompt": prompt,
        "access_type": "offline",
        "include_granted_scopes": "true",
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _exchange_code_for_token(code: str) -> Dict[str, Any]:
    cfg = _get_google_config()
    client_id = _safe_str(cfg.get("client_id"))
    client_secret = _safe_str(cfg.get("client_secret"))
    redirect_uri = _safe_str(cfg.get("redirect_uri"))
    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError("Missing google.client_id/client_secret/redirect_uri in Streamlit Secrets")

    return _http_json(
        GOOGLE_TOKEN_URL,
        method="POST",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )


def _fetch_userinfo(access_token: str) -> Dict[str, Any]:
    return _http_json(
        GOOGLE_USERINFO_URL,
        method="GET",
        headers={"Authorization": f"Bearer {access_token}"},
        data=None,
    )


def require_login(*, app_name: str = "Decarbonify") -> str:
    """Gate the app behind Google OAuth.

    If authenticated, returns the user's email. Otherwise renders a Sign in with Google button and stops.
    """

    cfg = _get_google_config()
    if not _safe_str(cfg.get("client_id")) or not _safe_str(cfg.get("client_secret")) or not _safe_str(cfg.get("redirect_uri")):
        st.error("Google auth is not configured. Set Streamlit Secrets: google.client_id, google.client_secret, google.redirect_uri")
        st.stop()

    user_email = current_user()
    if user_email:
        # Best-effort: refresh persistence so a missed cookie write can't
        # cause logouts across restarts.
        profile = current_user_profile()
        if isinstance(profile, dict) and profile.get("email"):
            _persist_user_cookie(profile, cfg=cfg)
        return user_email

    # If the user just clicked logout, don't immediately restore from cookie.
    suppress_until = int(st.session_state.get(_LOGOUT_SUPPRESS_RESTORE_UNTIL_KEY, 0) or 0)
    if suppress_until > int(time.time()):
        _clear_user_cookie(cfg=cfg)
        # Continue to sign-in UI below.

    qp = _get_query_params()
    code = (qp.get("code") or [""])[0]
    state = (qp.get("state") or [""])[0]
    oauth_error = (qp.get("error") or [""])[0]

    if oauth_error:
        if _OAUTH_REDIRECTING_KEY in st.session_state:
            del st.session_state[_OAUTH_REDIRECTING_KEY]
        st.error(f"Google sign-in failed: {oauth_error}")
        _clear_query_params()
        st.stop()

    # Cookie manager bootstrap: on a fresh session (e.g. server restart),
    # the CookieManager component may need one rerun before previously-set
    # cookies are available to Python. Trigger a one-time rerun so we can
    # restore auth without forcing the user to click anything.
    if (
        suppress_until <= int(time.time())
        and not code
        and not current_user()
        and not st.session_state.get(_COOKIE_BOOTSTRAP_KEY)
    ):
        if _cookie_manager() is not None:
            st.session_state[_COOKIE_BOOTSTRAP_KEY] = True
            st.rerun()

    # Callback handling
    if code:
        if _OAUTH_REDIRECTING_KEY in st.session_state:
            del st.session_state[_OAUTH_REDIRECTING_KEY]

        # Guard against duplicate processing: Streamlit may rerun the script during component init,
        # and Google auth codes are single-use.
        if st.session_state.get(_LAST_OAUTH_CODE_KEY) == code:
            _clear_query_params()
            st.stop()
        st.session_state[_LAST_OAUTH_CODE_KEY] = code

        if not _verify_oauth_state(state or "", cfg=cfg):
            st.error("Invalid OAuth state. Please try signing in again.")
            _clear_query_params()
            if _LAST_OAUTH_CODE_KEY in st.session_state:
                del st.session_state[_LAST_OAUTH_CODE_KEY]
            st.stop()

        try:
            token = _exchange_code_for_token(code)
            access_token = _safe_str(token.get("access_token"))
            if not access_token:
                raise RuntimeError("No access_token returned")
            refresh_token = _safe_str(token.get("refresh_token"))
            info = _fetch_userinfo(access_token)
            email = _safe_str(info.get("email")).lower()
            if not email:
                raise RuntimeError("No email returned from Google")
            if not _allowed_email(email):
                st.error("This Google account is not allowed to access this app.")
                _clear_query_params()
                st.stop()

            st.session_state[_GOOGLE_USER_KEY] = {
                "email": email,
                "name": _safe_str(info.get("name")),
                "picture": _safe_str(info.get("picture")),
                # May be empty unless prompt=consent and user grants offline access.
                "refresh_token": refresh_token,
            }
            _persist_user_cookie(st.session_state[_GOOGLE_USER_KEY], cfg=cfg)
            _clear_query_params()
            if _LAST_OAUTH_CODE_KEY in st.session_state:
                del st.session_state[_LAST_OAUTH_CODE_KEY]
            st.rerun()
        except Exception as exc:
            if _OAUTH_REDIRECTING_KEY in st.session_state:
                del st.session_state[_OAUTH_REDIRECTING_KEY]
            if _LAST_OAUTH_CODE_KEY in st.session_state:
                del st.session_state[_LAST_OAUTH_CODE_KEY]
            st.error(f"Google sign-in failed: {exc}")
            _clear_query_params()
            st.stop()

    # Restore from a signed cookie to survive full page refresh.
    # Do this only when we're not in the middle of an OAuth callback.
    if suppress_until <= int(time.time()) and _restore_user_from_cookie(cfg=cfg):
        user_email = current_user()
        if user_email:
            return user_email

    # Start login
    st.subheader("Sign in")
    st.caption(f"Access to {app_name} is restricted.")

    state_token = _create_oauth_state(cfg=cfg)
    try:
        # Ask for consent only if we don't have a refresh token yet.
        prompt = "consent" if not current_refresh_token() else "select_account"
        auth_url = _build_google_auth_url(state=state_token, prompt=prompt)
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    if st.session_state.get(_OAUTH_REDIRECTING_KEY):
        st.info("Redirecting to Google sign-in…")
        escaped = auth_url.replace("'", "%27")
        st.markdown(
            f"<meta http-equiv='refresh' content='0; url={escaped}'>",
            unsafe_allow_html=True,
        )
        st.stop()

    if st.button("Sign in with Google", use_container_width=True, type="primary"):
        st.session_state[_OAUTH_REDIRECTING_KEY] = True
        st.rerun()

    # Optional hints
    if isinstance(cfg.get("allowed_domains"), list) and cfg.get("allowed_domains"):
        st.caption("Allowed domains: " + ", ".join([str(x) for x in cfg.get("allowed_domains")]))
    if isinstance(cfg.get("allowed_emails"), list) and cfg.get("allowed_emails"):
        st.caption("Allowed emails list is configured.")

    st.stop()


def render_logout_sidebar() -> None:
    email = current_user()
    if not email:
        return

    profile = current_user_profile() or {}
    display = _safe_str(profile.get("name")) or email

    st.caption(f"Signed in as: {display}")
    if st.button("Logout", use_container_width=True):
        logout()
        st.rerun()
