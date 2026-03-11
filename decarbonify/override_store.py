from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict


def _overrides_path() -> str:
    # Allow overriding in deployment.
    return os.environ.get("DECARBONIFY_OVERRIDES_PATH", ".decarbonify_overrides.json")


def load_emissions_overrides(*, user_key: str) -> Dict[str, float]:
    """Load emissions overrides for a given user.

    Stored separately from the portfolio JSON.
    Returns a mapping asset_id -> tCO2e/year.
    """

    path = _overrides_path()
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}

    raw = data.get(user_key)
    if not isinstance(raw, dict):
        return {}

    out: Dict[str, float] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k:
            continue
        try:
            out[k] = float(v)
        except Exception:
            continue
    return out


def save_emissions_overrides(*, user_key: str, overrides: Dict[str, Any]) -> None:
    """Persist emissions overrides for a given user.

    Best-effort: failures should not break the app.
    """

    path = _overrides_path()

    # Read existing file (if any)
    data: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    cleaned: Dict[str, float] = {}
    for k, v in overrides.items():
        if not isinstance(k, str) or not k:
            continue
        try:
            cleaned[k] = float(v)
        except Exception:
            continue

    data[user_key] = cleaned

    # Atomic write
    try:
        folder = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="overrides_", suffix=".json", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
    except Exception:
        return
