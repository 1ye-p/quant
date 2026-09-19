"""custom_factor_loader — meta_custom_factors 表 → ExpressionFactor 消费侧。

自定义因子此前只有 CRUD（api_server /factors/custom），物化管线无法消费。
本模块把数据库中的自定义 DSL/Polars 表达式装配为 ExpressionFactor 实例，
供 FactorMaterializer 在装配 registry 时注册。
"""

from __future__ import annotations

import logging
from typing import Any

from cquant.factorlab.factors.expression_factor import ExpressionFactor

logger = logging.getLogger(__name__)

_CUSTOM_FACTOR_DDL = """
CREATE TABLE IF NOT EXISTS meta_custom_factors (
    factor_id   VARCHAR PRIMARY KEY,
    name        VARCHAR UNIQUE NOT NULL,
    expression  VARCHAR NOT NULL,
    description VARCHAR DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def load_custom_factors(catalog: Any) -> list[ExpressionFactor]:
    """读取 meta_custom_factors 表，返回 ExpressionFactor 实例列表。

    表不存在或读取失败时返回空列表（不阻塞物化流程）。
    表达式求值错误同样跳过（坏行隔离），仅记录 warning。
    """
    try:
        catalog.execute(_CUSTOM_FACTOR_DDL)
        df = catalog.query(
            "SELECT name, expression, description FROM meta_custom_factors"
        )
    except Exception as exc:
        logger.warning("load_custom_factors: could not read meta_custom_factors: %s", exc)
        return []

    if df.is_empty():
        return []

    factors: list[ExpressionFactor] = []
    for row in df.to_dicts():
        try:
            factors.append(
                ExpressionFactor(
                    name=row["name"],
                    expression=row["expression"],
                    description=row.get("description") or "",
                )
            )
        except Exception as exc:
            logger.warning(
                "load_custom_factors: skipping custom factor '%s': %s",
                row.get("name"), exc,
            )
    return factors
