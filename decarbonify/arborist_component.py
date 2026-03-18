from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import os

import streamlit.components.v1 as components


_COMPONENT_NAME = "decarbonify_arborist_tree"


def _declare_component() -> Optional[Any]:
    root = Path(__file__).resolve().parents[1]
    build_dir = root / "components" / "arborist_tree" / "frontend" / "dist"

    if build_dir.exists():
        return components.declare_component(_COMPONENT_NAME, path=str(build_dir))

    dev_url = os.environ.get("DECARBONIFY_ARB_DEV_URL", "").strip()
    if dev_url:
        return components.declare_component(_COMPONENT_NAME, url=dev_url)

    return None


_COMPONENT = _declare_component()


def arborist_tree_available() -> bool:
    return _COMPONENT is not None


def arborist_tree(
    *,
    data: list[dict[str, Any]],
    selection: Optional[str],
    height: int = 600,
    key: str,
) -> Dict[str, Any]:
    if _COMPONENT is None:
        raise RuntimeError(
            "Arborist component frontend is not built. "
            "Run: cd components/arborist_tree/frontend && npm install && npm run build "
            "(or set DECARBONIFY_ARB_DEV_URL to a running dev server)."
        )

    default: Dict[str, Any] = {"selectedId": selection or "", "lastAction": None, "lastActionId": 0}
    value = _COMPONENT(
        data=data,
        defaultSelection=selection,
        height=height,
        key=key,
        default=default,
    )

    if not isinstance(value, dict):
        return default
    return value
