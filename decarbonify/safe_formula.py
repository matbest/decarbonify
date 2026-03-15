from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class FormulaError(Exception):
    message: str

    def __str__(self) -> str:  # pragma: no cover
        return self.message


_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)


def _as_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            return None
    return None


def eval_arithmetic(expr: str, *, variables: Dict[str, Any]) -> float:
    """Safely evaluate an arithmetic-only expression.

    Allowed:
      - numbers
      - variable names (must exist in variables)
      - +, -, *, /, **
      - parentheses

    Disallowed:
      - function calls
      - attribute access, subscripts
      - comprehensions, lambdas, etc.
    """

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Invalid formula syntax: {exc}") from exc

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)

        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return float(node.value)
            raise FormulaError("Formula constants must be numbers")

        # Legacy node type for very old Pythons (<3.8). In newer Pythons this
        # class may not exist at all (e.g. 3.14+), so guard access.
        _AstNum = getattr(ast, "Num", None)  # pragma: no cover
        if _AstNum is not None and isinstance(node, _AstNum):  # pragma: no cover
            return float(node.n)

        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise FormulaError(f"Unknown variable: {node.id}")
            v = _as_number(variables.get(node.id))
            if v is None:
                raise FormulaError(f"Missing or non-numeric variable: {node.id}")
            return float(v)

        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                raise FormulaError("Only +, -, *, /, ** are allowed")
            left = visit(node.left)
            right = visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                return left**right
            raise FormulaError("Unsupported operator")

        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, _ALLOWED_UNARYOPS):
                raise FormulaError("Only unary + and - are allowed")
            value = visit(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +value
            if isinstance(node.op, ast.USub):
                return -value
            raise FormulaError("Unsupported unary operator")

        # Explicitly block everything else.
        raise FormulaError(f"Disallowed expression element: {type(node).__name__}")

    return float(visit(tree))
