# Testing and evidence

Choose validation according to the changed boundary:

- Local logic: focused pytest unit tests.
- Async orchestration or strategy lifecycle: streaming, cancellation,
  request-scope, and cleanup tests.
- FastAPI routes: request/response tests with dependency overrides.
- Connectors and plugins: integration-style tests with mocked Azure and
  network boundaries.
- Pydantic or JSON schemas: contract, serialization, and fixture tests.
- Identity and document authorization: negative tests proving an
  unauthorized principal cannot access protected content.
- Dashboard: existing frontend lint/build plus backend route tests.
- PowerShell or shell deployment assets: syntax and behavioral parity checks.
- Cross-repository changes: validate the exact compatible commits or tags.

Use `tests/conftest.py` fixtures instead of live Azure clients. The full Python
suite is run with `pytest`; the repository's pytest configuration enables
asyncio automatically and adds `src/` to `PYTHONPATH`.

For every change, capture:

1. acceptance criterion and observable result;
2. commands run and results;
3. relevant configuration, dependency, and component versions;
4. API, persistence, security, deployment, and rollback impact; and
5. validation that could not run and the resulting risk.

Do not treat a successful deployment as sufficient evidence when a change
affects authorization, data correctness, retrieval quality, streaming,
cleanup, or upgrade behavior.

Quality evaluates the revision under review: its checker, aggregate, pinned
tools, dependency manifests and contracts. The PR base supplies regression
history, not an immutable evaluator or SDK authority. Control maintenance is
an ordinary reviewed change; it does not need a separate authorization record.
Individual typing-debt allowances cannot be added or inflated, covered live
modules cannot leave scope, and suppressions cannot widen. Retirement requires
physical removal, not an inventory deletion or unchanged code copied elsewhere.
Keep historical module/handler provenance in Git and the owning versioned
retirement contract. Every remaining broad handler still needs its exact current
fingerprint, catch breadth and passing failure selectors. The aggregate requires
actual successful jobs, identical inventories and reports bound to the exact
reviewed head, base, policy, repository, run and attempt. Never rename tests to
impersonate retired evidence or manufacture success statuses.
