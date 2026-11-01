"""A native graph: research, durable human input, approved/rejected result."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from agent_framework import Agent, Executor, Workflow, WorkflowBuilder, WorkflowContext, handler, response_handler

from .config import FunctionalSnapshot, NativeConfig
from .contracts import (
    WORKFLOW_NAME, ResearchReviewResult, ResearchReviewStart, ReviewDecision, ReviewRequest,
)

AgentFactory = Callable[[FunctionalSnapshot | None], AbstractAsyncContextManager[Agent]]


class Research(Executor):
    def __init__(self, config: NativeConfig, agent_factory: AgentFactory):
        super().__init__(id="research")
        self.config = config
        self.agent_factory = agent_factory

    @handler
    async def draft(
        self, start: ResearchReviewStart | dict[str, Any], ctx: WorkflowContext[ReviewRequest],
    ) -> None:
        # DTS client wire inputs are JSON mappings; validate before any model/tool work.
        start = ResearchReviewStart.model_validate(start)
        view = await self.config.view()
        snapshot = FunctionalSnapshot.capture(view.values)
        # These are native executor state, not application-owned checkpoint documents.
        ctx.set_state("functional_snapshot", snapshot.model_dump(mode="json"))
        async with self.agent_factory(snapshot) as agent:
            answer = await agent.run(start.topic)
        await ctx.send_message(ReviewRequest(
            topic=start.topic, draft=answer.text, configuration_revision=snapshot.revision,
        ))


class HumanReview(Executor):
    def __init__(self):
        super().__init__(id="human-review")

    @handler
    async def request(self, draft: ReviewRequest, ctx: WorkflowContext[ResearchReviewResult]) -> None:
        await ctx.request_info(draft, ReviewDecision)

    @response_handler
    async def decide(
        self, request: ReviewRequest, response: ReviewDecision, ctx: WorkflowContext[ResearchReviewResult],
    ) -> None:
        # Validate dicts too: durable and Hosted deserialize at their own native boundaries.
        decision = ReviewDecision.model_validate(response)
        await ctx.send_message(ResearchReviewResult(
            status="approved" if decision.decision == "approve" else "rejected",
            topic=request.topic, draft=request.draft, review_comment=decision.comment,
            configuration_revision=request.configuration_revision,
        ))


class Complete(Executor):
    def __init__(self):
        super().__init__(id="complete")

    @handler
    async def finish(
        self, result: ResearchReviewResult, ctx: WorkflowContext[None, ResearchReviewResult],
    ) -> None:
        await ctx.yield_output(result)


def build_workflow(config: NativeConfig, agent_factory: AgentFactory) -> Workflow:
    research, review, complete = Research(config, agent_factory), HumanReview(), Complete()
    return (
        WorkflowBuilder(name=WORKFLOW_NAME, start_executor=research, output_from=[complete])
        .add_edge(research, review)
        .add_edge(review, complete)
        .build()
    )
