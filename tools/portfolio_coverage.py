import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple


def iter_assets_with_paths(portfolio: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    out: List[Tuple[str, Dict[str, Any]]] = []
    stack: List[Tuple[str, Dict[str, Any]]] = []

    for root in portfolio.get("assets") or []:
        if isinstance(root, dict):
            stack.append((root.get("name") or "Unnamed", root))

    while stack:
        path, asset = stack.pop()
        out.append((path, asset))
        for child in asset.get("assets") or []:
            if isinstance(child, dict):
                stack.append((path + " / " + (child.get("name") or "Unnamed"), child))

    return out


def main() -> None:
    portfolio = json.loads(Path("portfolio.json").read_text(encoding="utf-8"))

    templates_dir = Path("asset_types")
    existing_templates = {p.stem for p in templates_dir.glob("*.json")}

    assets = iter_assets_with_paths(portfolio)

    by_type: Counter[str] = Counter()
    missing: List[Tuple[str, str, str, str]] = []
    unknown: List[Tuple[str, str]] = []

    for path, asset in assets:
        atid = str(asset.get("asset_type_id") or "").strip()
        if atid:
            by_type[atid] += 1
            if atid not in existing_templates:
                unknown.append((path, atid))
        else:
            missing.append(
                (
                    path,
                    str(asset.get("core_type") or ""),
                    str(asset.get("subtype") or ""),
                    str(asset.get("current_role") or ""),
                )
            )

    print(f"Total assets: {len(assets)}")
    print(f"Assets with asset_type_id: {sum(by_type.values())}")
    print(f"Unique asset_type_id: {len(by_type)}")
    print(f"Existing templates: {len(existing_templates)}")

    print("\nUsed asset_type_id counts:")
    for atid, n in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {atid}: {n}")

    print("\nMissing asset_type_id summary (core_type/subtype/current_role):")
    summary = Counter((ct, st, role) for _, ct, st, role in missing)
    for (ct, st, role), n in sorted(summary.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {n:>3}  core_type={ct} subtype={st} role={role}")

    print("\nMissing asset_type_id examples (first 40):")
    for row in missing[:40]:
        print("  ", row)
    print(f"Missing asset_type_id total: {len(missing)}")

    print(f"\nUnknown asset_type_id (template file missing): {len(unknown)}")
    for row in unknown[:40]:
        print("  ", row)


if __name__ == "__main__":
    main()
