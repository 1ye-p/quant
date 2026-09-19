"""Compile DSL AST into Polars expressions."""

from __future__ import annotations
from typing import Any

import polars as pl

from cquant.factorlab.dsl_parser import (
    ASTNode, NumberNode, ColumnNode, BinaryOpNode, UnaryOpNode, FunctionCallNode,
)
from cquant.factorlab.dsl_functions import FUNCTIONS, AVAILABLE_COLUMNS


class DSLError(Exception):
    pass


def _evaluate_func_arg(
    node: ASTNode, extra_columns: "set[str] | list[str] | None" = None
) -> pl.Expr | int | float:
    """Evaluate a function argument — return scalar for NumberNode, Expr otherwise."""
    if isinstance(node, NumberNode):
        v = node.value
        return int(v) if v == int(v) else v
    return evaluate(node, extra_columns)


def evaluate(node: ASTNode, extra_columns: "set[str] | list[str] | None" = None) -> pl.Expr:
    """Compile an AST node into a Polars expression.

    *extra_columns* extends the module-level ``AVAILABLE_COLUMNS`` whitelist
    for the duration of this compilation (e.g. external-indicator aliases in
    the regime ``__MARKET__`` pseudo-panel — spike A adaptation #1). It does
    not mutate the global whitelist.
    """
    allowed_extras = set(extra_columns) if extra_columns else set()

    if isinstance(node, NumberNode):
        return pl.lit(node.value)

    if isinstance(node, ColumnNode):
        if node.name not in AVAILABLE_COLUMNS and node.name not in allowed_extras:
            raise DSLError(f"Unknown column: '{node.name}'. Available: {sorted(AVAILABLE_COLUMNS | allowed_extras)}")
        return pl.col(node.name)

    if isinstance(node, UnaryOpNode):
        operand = evaluate(node.operand, extra_columns)
        if node.op == '-':
            return -operand
        raise DSLError(f"Unknown unary operator: {node.op}")

    if isinstance(node, BinaryOpNode):
        left = evaluate(node.left, extra_columns)
        right = evaluate(node.right, extra_columns)
        ops = {
            '+': lambda l, r: l + r,
            '-': lambda l, r: l - r,
            '*': lambda l, r: l * r,
            '/': lambda l, r: l / r,
            '^': lambda l, r: l ** r,
            '>': lambda l, r: (l > r).cast(pl.Int8),
            '<': lambda l, r: (l < r).cast(pl.Int8),
            '>=': lambda l, r: (l >= r).cast(pl.Int8),
            '<=': lambda l, r: (l <= r).cast(pl.Int8),
            '==': lambda l, r: (l == r).cast(pl.Int8),
            '!=': lambda l, r: (l != r).cast(pl.Int8),
        }
        if node.op not in ops:
            raise DSLError(f"Unknown operator: {node.op}")
        return ops[node.op](left, right)

    if isinstance(node, FunctionCallNode):
        if node.name not in FUNCTIONS:
            raise DSLError(f"Unknown function: '{node.name}'. Available: {sorted(FUNCTIONS.keys())}")
        fn, min_args, max_args, _ = FUNCTIONS[node.name]
        nargs = len(node.args)
        if nargs < min_args or nargs > max_args:
            raise DSLError(
                f"'{node.name}' expects {min_args}-{max_args} args, got {nargs}"
            )
        evaluated_args = [_evaluate_func_arg(a, extra_columns) for a in node.args]
        return fn(*evaluated_args)

    raise DSLError(f"Unknown AST node type: {type(node).__name__}")


def compile_expression(
    expression: str, extra_columns: "set[str] | list[str] | None" = None
) -> pl.Expr:
    """Parse and compile a DSL expression string into a Polars expression.

    Parameters
    ----------
    expression:
        DSL expression string.
    extra_columns:
        Optional extra column names allowed in ``ColumnNode`` beyond the
        module-level ``AVAILABLE_COLUMNS`` whitelist (spike A adaptation:
        external-indicator aliases on the ``__MARKET__`` pseudo-panel).
    """
    from cquant.factorlab.dsl_parser import parse
    ast = parse(expression)
    return evaluate(ast, extra_columns)
