# Evaluations (deprecated)

The standalone evaluation scripts that used to live in this folder have been
removed. Evaluation for GPT-RAG agents now runs through the AgentOps Accelerator,
which scores the live orchestrator endpoint with Foundry's built-in evaluators and
turns the result into a pull request gate.

## Where evaluation lives now

- End-to-end evaluation workflow (dataset, evaluators, PR and dev gates):
  [AgentOps HTTP agent tutorial](https://azure.github.io/agentops/tutorial-http-agent/).
- Retrieval tuning with the Document Retrieval evaluator and ranking metrics:
  [Agent Landing Zone retrieval configuration](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/configuration/#retrieval).

## Why it changed

The old folder ran a one-off batch evaluation against a golden dataset and printed
a Foundry Studio link. AgentOps replaces that with a repeatable workflow: it builds
its own evaluation dataset, runs the evaluators against the deployed endpoint, and
fails the build when scores drop. Keeping a second, divergent harness here only
added drift, so evaluation is consolidated in one place.

