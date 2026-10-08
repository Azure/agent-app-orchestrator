---
name: engineering-principles
description: Agent Landing Zone Orchestrator architecture and implementation principles. Use for design, review, meaningful refactoring, runtime strategies, FastAPI, Azure integration, security, testing, or operations.
---

# Agent Landing Zone Orchestrator engineering principles

Load only the references needed for the task:

| When the task involves | Read |
| --- | --- |
| Repository purpose, boundaries, runtime strategies, or component ownership | [Orchestrator architecture](references/orchestrator-architecture.md) |
| Python, FastAPI, async code, modularity, or implementation clarity | [Python and FastAPI](references/python-fastapi.md) |
| Tests, validation, compatibility, or evidence | [Testing and evidence](references/testing-and-evidence.md) |
| Identity, secrets, networking, retrieval, MCP, telemetry, or operations | [Security and operations](references/security-and-operations.md) |

Use these principles as design questions rather than dogma. Task
requirements, executable configuration, versioned contracts, and the current
implementation remain the sources of truth.
