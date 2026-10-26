"""Local diagnostic HTTP -> separate Temporal worker -> native inference contract.

Only a fixed synthetic greeting is accepted; this is not a business endpoint.
Run the API and worker as different processes/containers on an isolated queue.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import os
from uuid import UUID

from fastapi import FastAPI
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker, UnsandboxedWorkflowRunner

from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.factory import build_model_runtime_ports
from app.modules.conversation.plan import ConversationPlan
from app.observability.telemetry import configure_telemetry, operation
from app.observability.temporal import temporal_interceptors
from app.observability.web import TelemetryMiddleware


QUEUE = "newsagent-governance-diagnostic-v1"


@activity.defn(name="governance_diagnostic_model")
async def model_step(request_id: str):
    config = load_model_runtime_config("/app/deploy/model-runtime.local.yml")
    if config.inference.provider != "local":
        raise ValueError("diagnostic requires local inference")
    ports = build_model_runtime_ports(config, environment="e2e")
    try:
        binding = config.agent_scene("conversation")
        result = await StructuredInferenceService(inference=ports.inference,prompts=ports.prompts).run(
            tenant_id="governance-synthetic",trace_id=request_id,idempotency_key=request_id,
            scene=binding.scene,prompt_version=binding.prompt_version,model_route=binding.model_route,
            payload={"message":"你好","history":[],"tool_results":[],"available_tools":[]},
            output_type=ConversationPlan,timeout_seconds=10)
        with operation("model.business_validate"):
            if result.value.action != "respond" or not result.value.answer:
                raise ValueError("diagnostic answer contract failed")
        return {"status":"completed","model_request_id":result.request_id,"synthetic_only":True,
                "worker_pid":os.getpid()}
    finally:
        await ports.close()


@workflow.defn(name="GovernanceDiagnosticWorkflow")
class ProbeWorkflow:
    @workflow.run
    async def run(self, request_id: str) -> dict:
        return await workflow.execute_activity(model_step,request_id,
            start_to_close_timeout=timedelta(seconds=20),
            retry_policy=RetryPolicy(maximum_attempts=1))


async def connect():
    return await Client.connect(os.environ.get("TEMPORAL_ADDRESS","temporal:7233"),
                                interceptors=temporal_interceptors())


def create_app():
    if os.environ.get("ENVIRONMENT") != "e2e":
        raise ValueError("diagnostic API is local e2e only")
    @asynccontextmanager
    async def lifespan(app):
        runtime = configure_telemetry("newsagent-governance-probe-api")
        app.state.client = await connect()
        try:
            yield
        finally:
            runtime.shutdown()
    app = FastAPI(lifespan=lifespan)
    app.add_middleware(TelemetryMiddleware)
    @app.get("/health")
    async def health():
        return {"status":"ok"}
    @app.post("/probe/{request_id}")
    async def probe(request_id: UUID):
        workflow_id = "governance-probe-"+str(request_id)
        async with asyncio.timeout(30):
            result = await app.state.client.execute_workflow(ProbeWorkflow.run,str(request_id),
                id=workflow_id,task_queue=QUEUE,execution_timeout=timedelta(seconds=25))
        return {**result,"workflow_id":workflow_id,"api_pid":os.getpid()}
    return app


async def worker_main():
    runtime = configure_telemetry("newsagent-governance-probe-worker")
    try:
        client = await connect()
        async with Worker(client,task_queue=QUEUE,workflows=[ProbeWorkflow],activities=[model_step],
                          workflow_runner=UnsandboxedWorkflowRunner()):
            await asyncio.Event().wait()
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    asyncio.run(worker_main())
