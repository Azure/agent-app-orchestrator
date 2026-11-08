"""A native graph: research, durable human input, approved/rejected result."""

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from agent_framework import Agent, Executor, Workflow, WorkflowBuilder, WorkflowContext, handler, response_handler

from .config import FunctionalSnapshot, NativeConfig
from .presentation import merge_presentations, response_projection, supervise
from .contracts import (
    WORKFLOW_NAME, ResearchReviewResult, ResearchReviewStart, ReviewDecision, ReviewRequest,
)

AgentFactory = Callable[[FunctionalSnapshot | None], AbstractAsyncContextManager[Agent]]


@dataclass(frozen=True)
class ResearchProducer:
    """An existing native definition at one declared workflow occurrence."""
    definition_id: str
    workflow_ordinal: int
    agent_factory: AgentFactory


class Research(Executor):
    def __init__(self, config: NativeConfig, agent_factory: AgentFactory,
                 producers: Sequence[ResearchProducer] | None = None):
        super().__init__(id="research")
        self.config = config
        self.agent_factory = agent_factory
        self.producers = tuple(producers) if producers is not None else None
        if self.producers is not None and (
            not self.producers or len(self.producers) > 10 or
            len({p.workflow_ordinal for p in self.producers}) != len(self.producers) or
            any(not p.definition_id or type(p.workflow_ordinal) is not int or p.workflow_ordinal < 0 for p in self.producers)
        ):
            raise ValueError("Research producers require unique declared occurrence ordinals (maximum ten).")

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
        if self.producers is None:
            async with self.agent_factory(snapshot) as agent:
                answer = await agent.run(start.topic)
            draft, presentation = response_projection(answer.messages)
        else:
            drafts, envelopes = [], []
            # Reuse the native executor, not a separate scheduler or transcript store.
            for producer in sorted(self.producers, key=lambda p: p.workflow_ordinal):
                async with producer.agent_factory(snapshot) as agent:
                    if agent.id != producer.definition_id:
                        raise ValueError("Research producer does not match its native definition.")
                    answer = await agent.run(start.topic)
                readable, envelope = response_projection(answer.messages)
                drafts.append(readable)
                if envelope is not None:
                    for contribution in envelope["contributions"]:
                        if contribution["agentId"] != producer.definition_id:
                            raise ValueError("Research contribution does not match its native definition.")
                        contribution["workflowOrdinal"] = producer.workflow_ordinal
                envelopes.append(envelope)
            draft = "\n\n".join(filter(None, drafts))
            presentation = await supervise(asyncio.to_thread(merge_presentations, envelopes))
        await ctx.send_message(ReviewRequest(
            topic=start.topic, draft=draft, configuration_revision=snapshot.revision,
            **({"presentation": presentation} if presentation is not None else {}),
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
            **({"presentation": request.presentation} if request.presentation is not None else {}),
        ))


class Complete(Executor):
    def __init__(self):
        super().__init__(id="complete")

    @handler
    async def finish(
        self, result: ResearchReviewResult, ctx: WorkflowContext[None, ResearchReviewResult],
    ) -> None:
        await ctx.yield_output(result)


def build_workflow(config: NativeConfig, agent_factory: AgentFactory,
                   *, producers: Sequence[ResearchProducer] | None = None) -> Workflow:
    research, review, complete = Research(config, agent_factory, producers), HumanReview(), Complete()
    return (
        WorkflowBuilder(name=WORKFLOW_NAME, start_executor=research, output_from=[complete])
        .add_edge(research, review)
        .add_edge(review, complete)
        .build()
    )
