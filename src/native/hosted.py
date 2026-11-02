"""Foundry Responses entrypoint. Native host owns sessions and checkpoints."""

import asyncio
import os
from typing import Any

from agent_framework import Workflow
from agent_framework_foundry_hosting import (
    CheckpointStoreProvider, HostedResponseRequest, ResponsesHostServer, WorkflowTurn,
)

from .agent import agent_factory, create_agent, hosted_identity
from .config import ConfigView, NativeConfig
from .contracts import ResearchReviewResult, ResearchReviewStart, ReviewDecision, ReviewRequest
from .workflow import build_workflow


async def parse_workflow_turn(request: HostedResponseRequest) -> WorkflowTurn:
    if request.request.get("store") is False:
        raise ValueError("Human-reviewed workflows require native store=true.")
    items = await request.get_input_items()
    if any(item.get("type") in ("function_call_output", "computer_call_output", "mcp_approval_response")
           for item in items):
        # Do not recover unknown/stale replies as new runs. SDK validates scoped
        # ownership, native wire aliases and the exact complete pending batch.
        return WorkflowTurn(responses=await request.get_workflow_responses())
    text = await request.get_input_text()
    if not text:
        raise ValueError("Workflow start requires a JSON ResearchReviewStart input.")
    return WorkflowTurn(input=ResearchReviewStart.model_validate_json(text))


def build_server(config: NativeConfig, observability_config: ConfigView | None = None) -> ResponsesHostServer:
    if config.settings is None or config.settings.host != "hosted":
        raise ValueError("The Hosted entrypoint requires ORCHESTRATION_HOST=hosted.")
    auth_mode = config.settings.auth_mode

    def identity_provider() -> Any:
        return hosted_identity(auth_mode)

    # Let the native host configure its own telemetry once, using scoped settings.
    observability: dict[str, Any] = {}
    if observability_config is not None:
        observability = {
            "applicationinsights_connection_string": observability_config.get("APPLICATIONINSIGHTS_CONNECTION_STRING"),
            "log_level": observability_config.get("LOG_LEVEL", "INFO").lower(),
        }
    if auth_mode == "service_identity":
        # Service mode reports missing delegation instead of starting user consent.
        observability["allowed_oauth_consent_origins"] = []
    if config.settings.mode == "conversational":
        return ResponsesHostServer(
            agent=lambda: create_agent(config, identity_provider=identity_provider),
            history_source="agent_server",
            **observability,
        )
    if config.settings.mode != "workflow":
        raise ValueError("Hosted requires one mode per publication.")

    async def workflow_factory(request: HostedResponseRequest) -> Workflow:
        # Called within each native request; graph IDs are immutable across turns.
        identity_provider()
        return build_workflow(config, agent_factory(config, identity_provider))

    return ResponsesHostServer(
        workflow=workflow_factory, parse_response=parse_workflow_turn,
        checkpoint_store_provider=CheckpointStoreProvider(allowed_checkpoint_types=[
            f"{type_.__module__}:{type_.__qualname__}"
            for type_ in (ResearchReviewStart, ReviewRequest, ReviewDecision, ResearchReviewResult)
        ]),
        **observability,
    )


async def main() -> None:
    from azure.identity import DefaultAzureCredential

    with DefaultAzureCredential() as credential:
        config = NativeConfig(os.environ.get("APP_CONFIG_ENDPOINT", ""), credential, credential)
        try:
            await config.open()
            await build_server(config, await config.view()).run_async(port=int(os.getenv("PORT", "8080")))
        finally:
            await config.close()


if __name__ == "__main__":
    asyncio.run(main())
