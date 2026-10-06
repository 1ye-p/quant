"""Machine learning experiment and job routes."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from cquant.api_server.deps import CatalogDep, run_job_async
from cquant.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ml", tags=["ml"])


from cquant.api_server.schemas.common import WalkForwardConfig


class PredictRequest(BaseModel):
    model_version: str = Field(..., min_length=1, max_length=128)
    date: str | None = None
    top_n: int = Field(default=50, ge=1, le=5000)


class RegisterModelRequest(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=128)
    model_version: str = Field(..., min_length=1, max_length=128)
    trainer_name: str = Field(..., min_length=1, max_length=64)
    artifact_path: str = ""
    feature_set_version: str = ""
    target_name: str = ""
    metrics: dict = {}
    description: str = ""


class BatchPredictRequest(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=128)
    dates: list[str] = Field(..., min_length=1, max_length=100)
    top_n: int = Field(default=50, ge=1, le=5000)


class MLJobBody(BaseModel):
    trainer: str                    # 'xgb' | 'lgbm' | 'xgb_clf'
    feature_set_version: str
    target_name: str = "ret_5d"
    params: dict = {}
    model_id: str = ""
    # Walk-forward config
    walk_forward: WalkForwardConfig | None = None
    # Dataset split ratios
    train_ratio: float = 0.7
    valid_ratio: float = 0.15


@router.get("/experiments")
async def list_experiments(catalog: CatalogDep, limit: int = 50) -> dict:
    """List ML experiments — always from DuckDB meta_ml_jobs, enriched with MLflow if available."""
    # Always query DuckDB first (authoritative source for job lifecycle)
    df = catalog.query(
        "SELECT job_id, trainer_name, feature_set_version, target_name, "
        "status, mlflow_run_id, artifact_path, error_text, submitted_at, completed_at "
        "FROM meta_ml_jobs ORDER BY submitted_at DESC LIMIT ?",
        [limit],
    )
    duckdb_items = df.to_dicts() if not df.is_empty() else []

    # Try to enrich with MLflow metrics/params if available
    mlflow_enriched = {}
    try:
        import mlflow
        mlflow.set_tracking_uri(settings.mlflow.tracking_uri)
        client = mlflow.tracking.MlflowClient()
        for exp in client.search_experiments():
            for run in client.search_runs(experiment_ids=[exp.experiment_id], max_results=limit * 2):
                mlflow_enriched[run.info.run_id] = {
                    "metrics": dict(run.data.metrics),
                    "params": dict(run.data.params),
                    "artifact_uri": run.info.artifact_uri,
                }
    except Exception as exc:
        logger.debug("MLflow enrichment unavailable: %s", exc)

    # Merge: DuckDB items enriched with MLflow data where mlflow_run_id matches
    items = []
    for item in duckdb_items:
        mlflow_run_id = item.get("mlflow_run_id", "")
        enriched = mlflow_enriched.get(mlflow_run_id, {})
        items.append({
            "run_id": mlflow_run_id or item["job_id"],
            "job_id": item["job_id"],
            "trainer_name": item["trainer_name"],
            "feature_set_version": item.get("feature_set_version", ""),
            "target_name": item.get("target_name", ""),
            "status": item["status"],
            "model_id": mlflow_run_id or item["job_id"],
            "metrics": enriched.get("metrics", {}),
            "params": enriched.get("params", {}),
            "artifact_path": item.get("artifact_path", ""),
            "artifact_uri": enriched.get("artifact_uri", ""),
            "error_text": item.get("error_text", ""),
            "started_at": item.get("submitted_at", ""),
            "completed_at": item.get("completed_at", ""),
        })

    return {"items": items[:limit], "total": len(items), "source": "duckdb+mlflow"}


@router.get("/experiments/{run_id}")
async def get_experiment(run_id: str, catalog: CatalogDep) -> dict:
    try:
        import mlflow
        mlflow.set_tracking_uri(settings.mlflow.tracking_uri)
        client = mlflow.tracking.MlflowClient()
        run = client.get_run(run_id)
        return {
            "run_id": run.info.run_id,
            "status": run.info.status,
            "metrics": dict(run.data.metrics),
            "params": dict(run.data.params),
            "artifact_uri": run.info.artifact_uri,
            "source": "mlflow",
        }
    except Exception:
        df = catalog.query(
            "SELECT job_id, mlflow_run_id, trainer_name, strategy_id, "
            "dataset_version, feature_set_version, label_name, status, "
            "created_at, completed_at, error "
            "FROM meta_ml_jobs WHERE mlflow_run_id = ? OR job_id = ?",
            [run_id, run_id],
        )
        if df.is_empty():
            raise HTTPException(status_code=404, detail=f"Experiment '{run_id}' not found")
        return {**df.to_dicts()[0], "source": "duckdb"}


@router.get("/experiments/{run_id}/feature-importance")
async def feature_importance(run_id: str, catalog: CatalogDep) -> dict:
    """Get feature importance — from persisted table first, then MLflow artifact."""
    # Try persisted table first (fast, no model loading)
    try:
        df = catalog.query(
            "SELECT feature_name, importance FROM meta_feature_importance "
            "WHERE model_id = ? ORDER BY importance DESC",
            [run_id],
        )
        if not df.is_empty():
            items = [
                {"feature": row["feature_name"], "importance": row["importance"]}
                for row in df.to_dicts()
            ]
            return {"items": items, "total": len(items), "source": "persisted"}
    except Exception:
        pass

    # Fallback: extract from MLflow model artifact
    try:
        import mlflow
        import polars as pl
        from pathlib import Path

        mlflow.set_tracking_uri(settings.mlflow.tracking_uri)
        client = mlflow.tracking.MlflowClient()
        run = client.get_run(run_id)
        trainer = run.data.params.get("trainer_name", "")
        artifact_uri = run.info.artifact_uri.replace("file://", "")

        if "xgboost" in trainer:
            import xgboost as xgb
            model_path = list(Path(artifact_uri).glob("**/*.json"))[0]
            model = xgb.XGBRegressor()
            model.load_model(str(model_path))
            scores = dict(zip(model.get_booster().feature_names or [], model.feature_importances_))
        elif "lightgbm" in trainer:
            import lightgbm as lgb
            model_path = list(Path(artifact_uri).glob("**/*.txt"))[0]
            booster = lgb.Booster(model_file=str(model_path))
            scores = dict(zip(booster.feature_name(), booster.feature_importance(importance_type='gain')))
        else:
            return {"items": [], "total": 0, "note": "Unknown trainer"}

        items = sorted(
            [{"feature": k, "importance": float(v)} for k, v in scores.items()],
            key=lambda x: x["importance"],
            reverse=True,
        )
        return {"items": items, "total": len(items), "source": "mlflow"}
    except Exception as exc:
        logger.warning("Feature importance extraction failed: %s", exc)
        return {"items": [], "total": 0, "error": str(exc)}


@router.get("/predictions")
async def get_latest_predictions(
    catalog: CatalogDep,
    asset_ids: str = "",
) -> dict:
    """返回 gold_predictions 表中最新交易日对给定资产的预测值。

    asset_ids: 逗号分隔的资产代码列表（如 SSE:600036,SZSE:000001）。
    返回 {date, predictions: {asset_id: prediction}}。
    """
    ids = [a.strip() for a in asset_ids.split(",") if a.strip()]
    if not ids:
        return {"date": None, "predictions": {}}

    in_ph = ",".join(["?" for _ in ids])
    try:
        # Fetch only the latest trade_date rows — avoids loading full history
        df = catalog.query(
            f"SELECT trade_date, asset_id, prediction "
            f"FROM gold_predictions "
            f"WHERE asset_id IN ({in_ph}) "
            f"  AND trade_date = ("
            f"    SELECT MAX(trade_date) FROM gold_predictions WHERE asset_id IN ({in_ph})"
            f")",
            ids + ids,  # params repeated for subquery
        )
    except Exception:
        return {"date": None, "predictions": {}}

    if df.is_empty():
        return {"date": None, "predictions": {}}

    latest_date = df["trade_date"][0]
    predictions = dict(zip(df["asset_id"].to_list(), df["prediction"].to_list()))
    return {"date": str(latest_date), "predictions": predictions}


@router.post("/jobs", status_code=202)
async def submit_ml_job(
    body: MLJobBody,
    background_tasks: BackgroundTasks,
    catalog: CatalogDep,
) -> dict:
    """Submit an async ML training job."""
    job_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc).isoformat()

    catalog.execute(
        """INSERT INTO meta_ml_jobs
           (job_id, trainer_name, feature_set_version, target_name, params_json, status, submitted_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        [job_id, body.trainer, body.feature_set_version,
         body.target_name, json.dumps(body.params), now],
    )

    # P5: job_id → 注册表覆盖（取消事件 + deadline watcher）。不传 catalog：
    # ML job 状态表是 meta_ml_jobs，超时持久化不走 _api_jobs。
    background_tasks.add_task(
        run_job_async, _run_ml_job, job_id, body, catalog,
        job_id=job_id, job_type="ml",
    )
    return {"job_id": job_id, "status": "submitted"}


@router.get("/jobs/{job_id}")
async def get_ml_job(job_id: str, catalog: CatalogDep) -> dict:
    df = catalog.query(
        "SELECT * FROM meta_ml_jobs WHERE job_id = ?", [job_id]
    )
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return df.to_dicts()[0]


@router.post("/predict")
async def predict(body: PredictRequest, catalog: CatalogDep) -> dict:
    from cquant.ml_lab.predict_service import run_online_prediction
    try:
        result = run_online_prediction(
            catalog=catalog,
            model_version=body.model_version,
            target_date=body.date,
            top_n=body.top_n,
        )
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("Prediction failed for model %s", body.model_version)
        raise HTTPException(status_code=500, detail=f"Prediction failed: {exc}")


# ---------------------------------------------------------------------------
# Model Diagnostics
# ---------------------------------------------------------------------------

@router.get("/models/{model_version}/diagnostics")
async def get_model_diagnostics(model_version: str, catalog: CatalogDep) -> dict:
    """Return diagnostics data for a model version.

    Three sections:
      1. training_curve  — epoch vs train_loss, valid_loss, valid_ic
      2. prediction_distribution — histogram bins of gold_predictions
      3. walk_forward_stability — fold vs IC, Sharpe, win_rate
    """
    result: dict = {
        "model_version": model_version,
        "training_curve": [],
        "prediction_distribution": [],
        "walk_forward_stability": [],
    }

    # 1. Training curve — try MLflow metrics history first
    try:
        import mlflow
        mlflow.set_tracking_uri(settings.mlflow.tracking_uri)
        client = mlflow.tracking.MlflowClient()
        run = client.get_run(model_version)
        metrics = run.data.metrics or {}

        # MLflow logs scalar metrics; build a synthetic curve from available keys
        # Typical keys: train_loss, valid_loss, valid_ic, best_iteration
        curve: list[dict] = []
        # If epoch-level metrics are stored as train_loss_0, train_loss_1, ...
        epoch_keys = sorted(
            [k for k in metrics if k.startswith("train_loss_") or k.startswith("valid_loss_")],
            key=lambda k: int(k.rsplit("_", 1)[-1]),
        )
        if epoch_keys:
            seen_epochs: set[int] = set()
            for k in epoch_keys:
                epoch = int(k.rsplit("_", 1)[-1])
                seen_epochs.add(epoch)
            for epoch in sorted(seen_epochs):
                entry: dict = {"epoch": epoch}
                tl = metrics.get(f"train_loss_{epoch}")
                vl = metrics.get(f"valid_loss_{epoch}")
                vi = metrics.get(f"valid_ic_{epoch}")
                if tl is not None:
                    entry["train_loss"] = float(tl)
                if vl is not None:
                    entry["valid_loss"] = float(vl)
                if vi is not None:
                    entry["valid_ic"] = float(vi)
                curve.append(entry)
        else:
            # Fallback: single-point curve from scalar metrics
            if any(k in metrics for k in ("train_loss", "valid_loss", "rmse", "mae")):
                curve.append({
                    "epoch": 0,
                    "train_loss": float(metrics.get("train_loss", metrics.get("rmse", 0))),
                    "valid_loss": float(metrics.get("valid_loss", metrics.get("mae", 0))),
                    "valid_ic": float(metrics.get("valid_ic", metrics.get("ic", 0))) if "valid_ic" in metrics or "ic" in metrics else None,
                })
        result["training_curve"] = curve
    except Exception as exc:
        logger.debug("Training curve extraction failed for %s: %s", model_version, exc)

    # 2. Prediction distribution from gold_predictions
    try:
        df = catalog.query(
            "SELECT prediction FROM gold_predictions WHERE model_version = ? LIMIT 10000",
            [model_version],
        )
        if not df.is_empty():
            preds = df["prediction"].to_list()
            if preds:
                n_bins = 30
                mn, mx = min(preds), max(preds)
                if mn == mx:
                    result["prediction_distribution"] = [
                        {"bin_start": mn, "bin_end": mx, "count": len(preds)}
                    ]
                else:
                    bin_width = (mx - mn) / n_bins
                    bins: list[dict] = []
                    for i in range(n_bins):
                        lo = mn + i * bin_width
                        hi = mn + (i + 1) * bin_width
                        cnt = sum(1 for p in preds if (lo <= p < hi) or (i == n_bins - 1 and p == hi))
                        bins.append({
                            "bin_start": round(lo, 6),
                            "bin_end": round(hi, 6),
                            "count": cnt,
                        })
                    result["prediction_distribution"] = bins
    except Exception as exc:
        logger.debug("Prediction distribution extraction failed for %s: %s", model_version, exc)

    # 3. Walk-forward stability from gold_backtest_runs (walk-forward folds)
    try:
        # Escape LIKE wildcards to prevent unintended matches
        escaped = model_version.replace("%", "\\%").replace("_", "\\_")
        df = catalog.query(
            "SELECT run_id, metrics_json FROM gold_backtest_runs "
            "WHERE strategy_id LIKE ? OR run_id LIKE ? "
            "ORDER BY started_at LIMIT 50",
            [f"%{escaped}%", f"%{escaped}%"],
        )
        if not df.is_empty():
            folds: list[dict] = []
            for row in df.to_dicts():
                metrics_json = row.get("metrics_json")
                if not metrics_json:
                    continue
                if isinstance(metrics_json, str):
                    m = json.loads(metrics_json)
                else:
                    m = metrics_json
                folds.append({
                    "fold_id": len(folds) + 1,
                    "run_id": row.get("run_id", ""),
                    "ic": float(m.get("information_ratio", 0) or 0),
                    "sharpe": float(m.get("sharpe_ratio", 0) or 0),
                    "win_rate": float(m.get("win_rate", 0) or 0),
                })
            result["walk_forward_stability"] = folds
    except Exception as exc:
        logger.debug("Walk-forward stability extraction failed for %s: %s", model_version, exc)

    return result


# ---------------------------------------------------------------------------
# Model Registry endpoints
# ---------------------------------------------------------------------------

@router.get("/models")
async def list_models(
    catalog: CatalogDep,
    stage: str | None = None,
    model_id: str | None = None,
) -> dict:
    """List registered models, optionally filtered by stage or model_id."""
    from cquant.ml_lab.model_registry import VALID_STAGES, ModelRegistry

    if stage is not None and stage not in VALID_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stage '{stage}'. Valid: {VALID_STAGES}",
        )
    registry = ModelRegistry(catalog)
    models = registry.list_models(stage=stage, model_id=model_id)
    return {"items": models, "total": len(models)}


@router.get("/models/catalog")
async def models_catalog() -> dict:
    """Return the full model catalog with metadata (type, engine, etc.)."""
    from cquant.qlib_bridge.models import get_all_models

    all_models = get_all_models()
    result = {}
    for name, info in all_models.items():
        result[name] = {
            "name": info.name,
            "display_name": info.display_name,
            "model_type": info.model_type,
            "engine": info.engine,
            "description": info.description,
            "category_label": info.category_label,
            "default_params": info.default_params,
            "tunable_params": list(info.tunable_params),
            "requires_alpha360": info.requires_alpha360,
        }
    return result


@router.post("/models", status_code=201)
async def register_model(
    body: RegisterModelRequest,
    catalog: CatalogDep,
) -> dict:
    """Register a model in the registry (staging stage)."""
    from cquant.ml_lab.model_registry import ModelRegistry

    registry = ModelRegistry(catalog)
    result = registry.register(
        model_id=body.model_id,
        model_version=body.model_version,
        trainer_name=body.trainer_name,
        artifact_path=body.artifact_path,
        feature_set_version=body.feature_set_version,
        target_name=body.target_name,
        metrics=body.metrics,
        description=body.description,
    )
    return result


@router.put("/models/{model_id}/promote")
async def promote_model(
    model_id: str,
    catalog: CatalogDep,
    model_version: str | None = None,
) -> dict:
    """Promote a model version to production.

    If ``model_version`` is not provided, promotes the latest staging version.
    """
    from cquant.ml_lab.model_registry import ModelRegistry

    registry = ModelRegistry(catalog)

    if model_version is None:
        # Find the latest staging version for this model_id
        models = registry.list_models(stage="staging", model_id=model_id)
        if not models:
            raise HTTPException(
                status_code=404,
                detail=f"No staging model found for model_id='{model_id}'",
            )
        model_version = models[0]["model_version"]

    try:
        result = registry.promote(model_id, model_version)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/predict/batch")
async def batch_predict(
    body: BatchPredictRequest,
    catalog: CatalogDep,
) -> dict:
    """Run batch predictions for multiple dates using the production model."""
    from cquant.ml_lab.model_registry import ModelRegistry

    registry = ModelRegistry(catalog)
    prod = registry.get_production(body.model_id)
    if prod is None:
        raise HTTPException(
            status_code=404,
            detail=f"No production model found for model_id='{body.model_id}'",
        )

    model_version = prod["model_version"]
    from cquant.ml_lab.predict_service import run_online_prediction
    results = []
    errors = []

    for date_str in body.dates:
        try:
            result = run_online_prediction(
                catalog=catalog,
                model_version=model_version,
                target_date=date_str,
                top_n=body.top_n,
            )
            results.append(result)
        except Exception as exc:
            logger.warning("Batch prediction failed for date %s: %s", date_str, exc)
            errors.append({"date": date_str, "error": str(exc)})

    return {
        "model_id": body.model_id,
        "model_version": model_version,
        "results": results,
        "errors": errors,
        "total_requested": len(body.dates),
        "total_succeeded": len(results),
    }


def _extract_feature_importance(trainer, artifact) -> dict[str, float]:
    """Extract feature importance from a trained model artifact."""
    trainer_name = artifact.trainer_name
    if "lightgbm" in trainer_name:
        try:
            return trainer.feature_importance(artifact, importance_type="gain")
        except Exception:
            pass
    elif "xgboost" in trainer_name:
        try:
            import xgboost as xgb
            model = xgb.XGBRegressor()
            model.load_model(artifact.model_path)
            names = model.get_booster().feature_names or artifact.feature_names
            return dict(zip(names, model.feature_importances_.tolist()))
        except Exception:
            pass
    # Fallback: try generic approach
    try:
        return trainer.feature_importance(artifact)
    except Exception:
        return {}


def _run_ml_job(job_id: str, body: MLJobBody, catalog: CatalogDep) -> None:
    """Background task: run the ML training job."""
    now = datetime.now(tz=timezone.utc).isoformat()
    try:
        catalog.execute(
            "UPDATE meta_ml_jobs SET status = 'running' WHERE job_id = ?", [job_id]
        )

        if body.trainer == "xgb":
            from cquant.ml_lab.trainers.xgb import XGBTrainer
            trainer = XGBTrainer()
        elif body.trainer == "lgbm":
            from cquant.ml_lab.trainers.lgbm import LGBMTrainer
            trainer = LGBMTrainer()
        elif body.trainer == "xgb_clf":
            from cquant.ml_lab.trainers.xgb_classifier import XGBClassifierTrainer
            trainer = XGBClassifierTrainer()
        else:
            raise ValueError(f"Unknown trainer: {body.trainer!r}")

        from cquant.ml_lab.datasets import MLDataset

        # Auto-infer feature names from gold_factor_values for this feature_set_version,
        # excluding the target factor.
        factor_df = catalog.query(
            "SELECT DISTINCT factor_name FROM gold_factor_values WHERE feature_set_version = ?",
            [body.feature_set_version],
        )
        all_factors = factor_df["factor_name"].to_list() if not factor_df.is_empty() else []
        feature_names = [f for f in all_factors if f != body.target_name]

        if not feature_names:
            raise ValueError(
                f"No factor values found for feature_set_version='{body.feature_set_version}'. "
                "Run the factor pipeline first to populate gold_factor_values."
            )

        dataset = MLDataset.from_catalog(
            catalog=catalog,
            feature_set_version=body.feature_set_version,
            feature_names=feature_names,
            target_name=body.target_name,
        )

        config = {"target_name": body.target_name, "params": body.params}
        if body.model_id:
            config["model_id"] = body.model_id

        if body.walk_forward:
            # Walk-forward training: use pipeline
            from cquant.ml_lab.pipeline import run_ml_prediction_pipeline
            model_id = run_ml_prediction_pipeline(
                catalog=catalog,
                features=dataset.data,
                target_col=body.target_name,
                model_id_prefix=body.model_id or body.trainer,
                n_splits=body.walk_forward.n_splits,
                gap_days=body.walk_forward.gap_days,
            )
            artifact = None  # pipeline handles persistence
        else:
            # Single train/valid split (existing behavior)
            train, valid, _ = dataset.train_valid_test_split(
                train_ratio=body.train_ratio,
                valid_ratio=body.valid_ratio,
            )
            artifact = trainer.fit(train, valid, config)

        # Persist feature importance if we have an artifact
        if artifact and artifact.feature_names:
            try:
                fi = _extract_feature_importance(trainer, artifact)
                if fi:
                    from cquant.ml_lab.base import persist_feature_importance
                    persist_feature_importance(artifact, fi, catalog, job_id=job_id)
            except Exception as fi_exc:
                logger.warning("Feature importance persistence failed: %s", fi_exc)

        from cquant.ml_lab.experiments import ExperimentTracker
        tracker = ExperimentTracker()
        mlflow_run_id = artifact.model_id if artifact else body.model_id or job_id
        with tracker.start_run(run_name=f"{body.trainer}_{job_id[:8]}") as mlrun:
            tracker.log_params({
                "trainer": body.trainer,
                "feature_set": body.feature_set_version,
                "target": body.target_name,
                "walk_forward": str(body.walk_forward is not None),
            })
            if artifact:
                tracker.log_metrics(artifact.metrics)
                tracker.log_artifact(artifact.model_path)
            if mlrun is not None and hasattr(mlrun, "info"):
                mlflow_run_id = mlrun.info.run_id

        completed = datetime.now(tz=timezone.utc).isoformat()
        catalog.execute(
            "UPDATE meta_ml_jobs SET status = 'done', mlflow_run_id = ?, artifact_path = ?, completed_at = ? WHERE job_id = ?",
            [mlflow_run_id, artifact.model_path if artifact else "", completed, job_id],
        )
    except Exception as exc:
        logger.exception("ML job %s failed: %s", job_id, exc)
        catalog.execute(
            "UPDATE meta_ml_jobs SET status = 'error', error_text = ?, completed_at = ? WHERE job_id = ?",
            [str(exc), datetime.now(tz=timezone.utc).isoformat(), job_id],
        )
