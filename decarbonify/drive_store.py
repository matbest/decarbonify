from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, Optional, Tuple


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"

_APP_FOLDER_PROP_KEY = "decarbonify_app_folder"
_APP_FOLDER_PROP_VALUE = "1"


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _http_json(
    url: str,
    *,
    method: str,
    access_token: Optional[str] = None,
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    request_headers = {"Accept": "application/json"}
    if access_token:
        request_headers["Authorization"] = f"Bearer {access_token}"
    if headers:
        request_headers.update(headers)

    req = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        # Try parse structured error
        try:
            parsed = json.loads(body) if body else {}
            if isinstance(parsed, dict) and "error" in parsed:
                raise RuntimeError(f"HTTP {exc.code}: {parsed.get('error')}") from None
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {body or exc.reason}") from None

    try:
        parsed = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"Unexpected response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Unexpected response")
    return parsed


def _http_bytes(
    url: str,
    *,
    method: str,
    access_token: str,
) -> bytes:
    req = urllib.request.Request(
        url,
        data=None,
        method=method,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {exc.code}: {body or exc.reason}") from None


def _http_no_content(
    url: str,
    *,
    method: str,
    access_token: str,
) -> None:
    req = urllib.request.Request(
        url,
        data=None,
        method=method,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25):
            return
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {exc.code}: {body or exc.reason}") from None


def refresh_access_token(*, cfg: Dict[str, Any], refresh_token: str) -> Tuple[str, int]:
    client_id = _safe_str(cfg.get("client_id"))
    client_secret = _safe_str(cfg.get("client_secret"))
    if not client_id or not client_secret:
        raise RuntimeError("Missing google.client_id/client_secret")
    if not refresh_token:
        raise RuntimeError("Missing refresh token")

    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")

    resp = _http_json(
        GOOGLE_TOKEN_URL,
        method="POST",
        access_token=None,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    token = _safe_str(resp.get("access_token"))
    expires_in = int(resp.get("expires_in", 3600) or 3600)
    if not token:
        raise RuntimeError("No access_token from refresh")
    return token, int(time.time()) + max(30, expires_in - 30)


def find_or_create_folder(*, access_token: str, folder_name: str) -> str:
    folder_name = (folder_name or "Decarbonify").strip() or "Decarbonify"

    def _list(q: str, *, order_by: str = "createdTime") -> list[dict]:
        params = urllib.parse.urlencode(
            {
                "q": q,
                "spaces": "drive",
                "fields": "files(id,name,createdTime,appProperties)",
                "orderBy": order_by,
                "pageSize": 10,
            }
        )
        resp = _http_json(f"{DRIVE_FILES_URL}?{params}", method="GET", access_token=access_token)
        files = resp.get("files")
        return files if isinstance(files, list) else []

    # 1) Prefer a folder explicitly marked as the app folder (prevents duplicates).
    q_marked = " and ".join(
        [
            "mimeType = 'application/vnd.google-apps.folder'",
            "'root' in parents",
            "trashed = false",
            f"appProperties has {{ key='{_APP_FOLDER_PROP_KEY}' and value='{_APP_FOLDER_PROP_VALUE}' }}",
        ]
    )
    marked = _list(q_marked)
    if marked:
        fid = marked[0].get("id")
        if isinstance(fid, str) and fid:
            return fid

    # 2) Fall back to a same-name folder in root.
    escaped = folder_name.replace("'", "\\'")
    q_named = " and ".join(
        [
            f"name = '{escaped}'",
            "mimeType = 'application/vnd.google-apps.folder'",
            "'root' in parents",
            "trashed = false",
        ]
    )
    # If duplicates exist (e.g. from earlier versions), prefer the newest folder.
    named = _list(q_named, order_by="createdTime desc")
    if named:
        fid = named[0].get("id")
        if isinstance(fid, str) and fid:
            # Best-effort: mark it so future runs find it via appProperties.
            try:
                patch = {"appProperties": {_APP_FOLDER_PROP_KEY: _APP_FOLDER_PROP_VALUE}}
                _http_json(
                    f"{DRIVE_FILES_URL}/{urllib.parse.quote(fid)}",
                    method="PATCH",
                    access_token=access_token,
                    data=json.dumps(patch).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
            except Exception:
                pass
            return fid

    meta = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["root"],
        "appProperties": {_APP_FOLDER_PROP_KEY: _APP_FOLDER_PROP_VALUE},
    }
    created = _http_json(
        DRIVE_FILES_URL,
        method="POST",
        access_token=access_token,
        data=json.dumps(meta).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    fid = created.get("id")
    if not isinstance(fid, str) or not fid:
        raise RuntimeError("Failed to create Drive folder")
    return fid


def _list_files_by_name(*, access_token: str, folder_id: str, file_name: str) -> list[dict]:
    q = " and ".join(
        [
            f"name = '{file_name.replace("'", "\\'")}'",
            f"'{folder_id}' in parents",
            "trashed = false",
        ]
    )
    params = urllib.parse.urlencode(
        {
            "q": q,
            "spaces": "drive",
            "fields": "files(id,name,modifiedTime,createdTime)",
            "orderBy": "modifiedTime desc",
            "pageSize": 50,
        }
    )
    resp = _http_json(f"{DRIVE_FILES_URL}?{params}", method="GET", access_token=access_token)
    files = resp.get("files")
    return files if isinstance(files, list) else []


def _find_file_id(*, access_token: str, folder_id: str, file_name: str) -> str:
    files = _list_files_by_name(access_token=access_token, folder_id=folder_id, file_name=file_name)
    if files:
        fid = files[0].get("id")
        if isinstance(fid, str) and fid:
            return fid
    return ""


def _delete_file(*, access_token: str, file_id: str) -> None:
    if not file_id:
        return
    _http_no_content(f"{DRIVE_FILES_URL}/{urllib.parse.quote(file_id)}", method="DELETE", access_token=access_token)


def download_json_file(*, access_token: str, folder_id: str, file_name: str) -> Optional[Dict[str, Any]]:
    file_id = _find_file_id(access_token=access_token, folder_id=folder_id, file_name=file_name)
    if not file_id:
        return None
    raw = _http_bytes(
        f"{DRIVE_FILES_URL}/{urllib.parse.quote(file_id)}?alt=media",
        method="GET",
        access_token=access_token,
    )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def upload_json_file(*, access_token: str, folder_id: str, file_name: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    matches = _list_files_by_name(access_token=access_token, folder_id=folder_id, file_name=file_name)
    file_id = ""
    if matches:
        fid = matches[0].get("id")
        if isinstance(fid, str) and fid:
            file_id = fid

    boundary = "----decarbonifyBoundary"
    meta = {"name": file_name, "parents": [folder_id]}
    if file_id:
        # For updates, don't send parents (Drive requires addParents/removeParents for parent changes).
        meta = {"name": file_name}
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(meta, ensure_ascii=False)
        + "\r\n"
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(obj, ensure_ascii=False, indent=2)
        + "\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    headers = {"Content-Type": f"multipart/related; boundary={boundary}"}

    fields = urllib.parse.urlencode({"fields": "id,modifiedTime,webViewLink,name"})

    if file_id:
        url = f"{DRIVE_UPLOAD_URL}/{urllib.parse.quote(file_id)}?uploadType=multipart&{fields}"
        meta = _http_json(url, method="PATCH", access_token=access_token, data=body, headers=headers)
        # Best-effort: delete duplicates (older same-name files) so future saves update one canonical file.
        for extra in matches[1:]:
            extra_id = extra.get("id")
            if isinstance(extra_id, str) and extra_id and extra_id != file_id:
                try:
                    _delete_file(access_token=access_token, file_id=extra_id)
                except Exception:
                    pass
        return meta

    url = f"{DRIVE_UPLOAD_URL}?uploadType=multipart&{fields}"
    meta = _http_json(url, method="POST", access_token=access_token, data=body, headers=headers)
    # Best-effort: if there were same-name files, clean them up.
    for extra in matches[1:]:
        extra_id = extra.get("id")
        if isinstance(extra_id, str) and extra_id:
            try:
                _delete_file(access_token=access_token, file_id=extra_id)
            except Exception:
                pass
    return meta
