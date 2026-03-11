from __future__ import annotations

import json
from typing import Any, Dict, List


def safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def load_portfolio_from_bytes(raw_bytes: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc
    validate_portfolio(data)
    return data


def load_portfolio_from_path(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    validate_portfolio(data)
    return data


def validate_portfolio(portfolio: Dict[str, Any]) -> None:
    if not isinstance(portfolio, dict):
        raise ValueError("Portfolio must be a JSON object")
    if "portfolio_name" not in portfolio:
        raise ValueError("Portfolio must contain 'portfolio_name'")
    if "assets" not in portfolio or not isinstance(portfolio["assets"], list):
        raise ValueError("Portfolio must contain 'assets' as a list")
