#!/usr/bin/env python3
"""Seed simulation-only evaluation fixtures through existing application services.

Default execution only reports a plan. ``--apply`` adds this script's fixed
synthetic cases, test-actor labels and frozen datasets to the isolated local
stack. These labels are neither human review nor enterprise quality evidence.
Existing business cases, labels and the active Bundle are never changed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5


AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from sqlalchemy.engine import make_url  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.model_runtime.bundle_runtime import NativeProductionBundleRuntimeRegistry  # noqa: E402
from app.model_runtime.config_file import load_model_runtime_config  # noqa: E402
from app.repositories.evaluation_dataset import PostgresEvaluationDatasetRepository  # noqa: E402
from app.repositories.production_bundle import PostgresProductionBundleRepository  # noqa: E402
from app.schemas.analysis_feedback import (  # noqa: E402
    ApproveAnalysisFeedbackLabelCommand, CollectAnalysisFeedbackCommand,
    FeedbackSourceReference, SubmitAnalysisFeedbackLabelCommand,
)
from app.schemas.evaluation_dataset import FreezeEvaluationDatasetCommand, FreezeEvaluationDatasetResult  # noqa: E402
from app.schemas.hot_news import HotNewsAnalysisInput, HotNewsAnalysisReport, HotNewsMetrics, HotScoreComponents  # noqa: E402
from app.services.data_loop.dataset_freezer import EvaluationDatasetFreezer, S3EvaluationDatasetArtifactStore  # noqa: E402
from app.services.data_loop.feedback_collector import AnalysisFeedbackCollector  # noqa: E402


PREFIX = "conversation-demo-fixture-v1"
TENANT = "11111111-1111-4111-8111-111111111111"
LABELER = f"{PREFIX}:synthetic-labeler"
REVIEWER = f"{PREFIX}:synthetic-reviewer"
PAST = datetime(2026, 10, 3, 0, tzinfo=timezone.utc)
LAYERS = ("golden", "fresh_bad_case", "high_risk_regression")
SIMULATION_NOTICE = "仅为本地合成链路验证；测试演员标签不代表人类审核或企业模型质量。"


class SeedGuardError(ValueError):
    """A safe, credential-free reason why the local fixture cannot be applied."""


def fixture_id(kind, layer):
    return uuid5(NAMESPACE_URL, f"NewsAgent:{PREFIX}:{TENANT}:{kind}:{layer}")


def check_target(settings, model_config, *, environ=None):
    values = os.environ if environ is None else environ
    if settings.environment != "e2e" or values.get("LOCAL_SIMULATION_ENABLED") != "1":
        raise SeedGuardError("isolated_local_simulation_required")
    if settings.model_runtime_backend != "native" or model_config.inference.provider != "local":
        raise SeedGuardError("local_model_provider_required")
    database = make_url(str(settings.database_url))
    if (database.host not in {"postgres", "127.0.0.1", "localhost"}
            or database.database != "newsagent_native" or database.username != "newsagent_native"
            or database.port not in {5432, 25432}):
        raise SeedGuardError("isolated_fixture_database_required")
    endpoint = urlsplit(settings.artifact_endpoint or "")
    if (endpoint.scheme != "http" or endpoint.hostname not in {"minio", "127.0.0.1", "localhost"}
            or endpoint.port not in {9000, 29000} or endpoint.username or endpoint.password
            or settings.artifact_bucket != "newsagent-native-artifacts"):
        raise SeedGuardError("isolated_fixture_artifact_store_required")
    if PAST + timedelta(hours=5) >= datetime.now(timezone.utc):
        raise SeedGuardError("fixed_fixture_window_must_be_in_the_past")


def commands_for_layer(base, layer):
    index = LAYERS.index(layer)
    occurred = PAST + timedelta(hours=index * 2)
    news_id = f"{PREFIX}:{layer}:news"
    analysis_input = HotNewsAnalysisInput(news_id=news_id, title=f"本地合成评测样例：{layer}",
        summary=SIMULATION_NOTICE, content_excerpt="虚构事件与固定聚合指标，不包含用户行为明细。",
        content_type="article", window_start=occurred-timedelta(hours=1), window_end=occurred,
        hot_score=0.65, metrics=HotNewsMetrics(impressions=1000, clicks=100, ctr=0.1,
            unique_users=80, effective_consumptions=60, interactions=20),
        score_components=HotScoreComponents(click=0.2, consumption=0.3, interaction=0.1, growth=0.05),
        analysis_policy_version=base.spec.analysis_prompt_version)
    observed = HotNewsAnalysisReport(news_id=news_id, trend_assessment="本地合成分析观察快照",
        dominant_driver="click" if layer == "fresh_bad_case" else "consumption",
        overall_confidence=1, limitations=[SIMULATION_NOTICE], evidence_news_ids=[], applied_memory_ids=[])
    case = CollectAnalysisFeedbackCommand(run_id=None, run_idempotency_key=f"{PREFIX}:source:{layer}",
        news_id=news_id, source_type="validation_failed", problem_type="analysis_incorrect",
        severity="critical" if layer == "high_risk_regression" else "medium",
        production_bundle_version=base.bundle_version, analysis_input_snapshot=analysis_input,
        analysis_output_snapshot=observed,
        source_reference=FeedbackSourceReference(reference_type="validation_attempt",
            reference_id=f"{PREFIX}:synthetic:{layer}", diagnostic_code="synthetic_fixture",
            diagnostic_summary=SIMULATION_NOTICE), occurred_at=occurred,
        idempotency_key=f"{PREFIX}:case:{layer}")
    label = SubmitAnalysisFeedbackLabelCommand(feedback_case_id=fixture_id("case", layer),
        verdict="incorrect" if layer == "fresh_bad_case" else "correct",
        allowed_dominant_drivers=("consumption",), required_metric_keys=("hot_score",),
        must_state_limitation=True, operator_comment=SIMULATION_NOTICE, labeled_by=LABELER,
        labeled_at=occurred+timedelta(minutes=2), idempotency_key=f"{PREFIX}:label:{layer}")
    approval = ApproveAnalysisFeedbackLabelCommand(feedback_case_id=fixture_id("case", layer),
        label_id=fixture_id("label", layer), expected_label_version=1, approved_by=REVIEWER,
        approved_at=occurred+timedelta(minutes=3), review_reason=SIMULATION_NOTICE,
        idempotency_key=f"{PREFIX}:synthetic-review:{layer}")
    freeze = FreezeEvaluationDatasetCommand(tenant_id=TENANT, dataset_name=f"{PREFIX}-{layer}",
        dataset_version="v1", dataset_layer=layer, description=SIMULATION_NOTICE,
        feedback_case_ids=(fixture_id("case", layer),), source_cutoff_at=occurred+timedelta(minutes=4),
        frozen_by=REVIEWER, idempotency_key=f"{PREFIX}:freeze:{layer}")
    return case, label, approval, freeze


async def seed_fixture(settings, model_config, *, apply=False, environ=None, database=None,
                       collector=None, artifacts=None, repository_factory=None):
    """Apply only owned fixtures, with no model call, release or Workflow start."""
    check_target(settings, model_config, environ=environ)
    own_database = database is None
    database = database or Database(settings)
    collector = collector or AnalysisFeedbackCollector()
    repository_factory = repository_factory or PostgresEvaluationDatasetRepository
    try:
        async with database.session() as session:
            base = await PostgresProductionBundleRepository(session).get_active_bundle(tenant_id=TENANT)
        if base is None or base.tenant_id != TENANT or base.status != "active":
            raise SeedGuardError("existing_active_fixture_bundle_required")
        NativeProductionBundleRuntimeRegistry.validation_only(model_config.prompt_registry(),
            settings.hot_news_runtime_manifest_json, allowed_model_routes=model_config.inference.model_routes).ensure_supported(base.spec)
        report = {"schema_version": f"{PREFIX}:report", "mode": "applied" if apply else "check",
            "simulation_only": True, "human_review": False, "enterprise_quality_validated": False,
            "notice": SIMULATION_NOTICE, "tenant_id": TENANT, "active_bundle_version": base.bundle_version,
            "synthetic_labeler": LABELER, "synthetic_reviewer": REVIEWER,
            "analysis_status": "degraded", "analysis_note": "合成案例没有线上源运行；可选源运行统计会降级，核心三层回放仍可执行。",
            "datasets": []}
        if not apply:
            report["datasets"] = [{"layer": layer, "planned_case_id": str(fixture_id("case", layer)),
                "planned_label_id": str(fixture_id("label", layer)), "dataset_name": commands_for_layer(base, layer)[3].dataset_name,
                "dataset_version": "v1", "case_count": 1, "persisted": False} for layer in LAYERS]
            return report
        artifacts = artifacts or S3EvaluationDatasetArtifactStore(settings)
        for layer in LAYERS:
            case, label, approval, freeze = commands_for_layer(base, layer)
            async with database.session() as session:
                written = await collector.collect(session, tenant_id=TENANT, command=case,
                    recorded_at=case.occurred_at+timedelta(minutes=1), feedback_case_id=fixture_id("case", layer))
                stored = written.case
                if (stored.id != fixture_id("case", layer) or stored.tenant_id != TENANT or stored.run_id is not None
                        or stored.idempotency_key != case.idempotency_key
                        or stored.source_reference.reference_id != case.source_reference.reference_id
                        or stored.production_bundle_version != base.bundle_version
                        or stored.model_dump(include=set(CollectAnalysisFeedbackCommand.model_fields)) != case.model_dump()):
                    raise SeedGuardError("refusing_to_review_non_fixture_case")
                labeled = await collector.submit_label(session, tenant_id=TENANT, command=label,
                    recorded_at=label.labeled_at, label_id=fixture_id("label", layer))
                if (labeled.label.id != fixture_id("label", layer) or labeled.label.feedback_case_id != fixture_id("case", layer)
                        or labeled.label.tenant_id != TENANT or labeled.label.labeled_by != LABELER
                        or labeled.label.model_dump(include=set(SubmitAnalysisFeedbackLabelCommand.model_fields)
                            - {"expected_previous_version"}) != label.model_dump(exclude={"expected_previous_version"})):
                    raise SeedGuardError("refusing_to_review_non_fixture_label")
                await collector.approve_label(session, tenant_id=TENANT, command=approval)
            # End the read transaction before uploading. Persist the resulting
            # content-addressed artifact in a fresh short transaction.
            async with database.session() as session:
                freezer = EvaluationDatasetFreezer(repository=repository_factory(session), artifact_store=artifacts)
                prepared = await freezer.prepare(freeze)
            if isinstance(prepared, FreezeEvaluationDatasetResult):
                outcome = prepared
            else:
                artifact = await freezer.upload(prepared)
                async with database.session() as session:
                    outcome = await EvaluationDatasetFreezer(repository=repository_factory(session),
                        artifact_store=artifacts).persist(prepared, artifact)
            dataset = outcome.dataset
            report["datasets"].append({"layer": layer, "dataset_id": str(dataset.dataset_id),
                "dataset_name": dataset.dataset_name, "dataset_version": dataset.dataset_version,
                "content_sha256": dataset.content_sha256, "case_count": dataset.case_count,
                "persisted": True, "created": outcome.created})
        return report
    finally:
        if own_database:
            await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Add only this script's synthetic isolated fixtures")
    parser.add_argument("--check", action="store_true", help="Read the local target and report the plan (default)")
    args = parser.parse_args(argv)
    if args.apply and args.check:
        parser.error("--check and --apply are mutually exclusive")
    try:
        settings = Settings(_env_file=None)
        model_config = load_model_runtime_config(settings.model_runtime_config_path)
        result = asyncio.run(seed_fixture(settings, model_config, apply=args.apply))
    except SeedGuardError as exc:
        print(f"fixture_refused: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # Never expose a DSN, credential or raw database/storage exception.
        print("fixture_failed: check isolated local dependencies and fixture conflicts", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
