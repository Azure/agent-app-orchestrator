"""Tests for the web grounding (Grounding with Bing) knowledge source path.

Covers the 5th Foundry IQ knowledge source kind, ``web``:

- ``maxRuntimeInSeconds`` is emitted when web grounding is enabled (Bing
  calls add latency; web grounding counts toward the runtime ceiling).
- ``web`` knowledge source params are appended only when
  ``WEB_GROUNDING_ENABLED`` is true and ``WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME``
  is set.
- ``web`` params never carry a ``filterAddOn``. There is no ACL on public
  web data.
- ``web`` never requires an OBO token; the source is emitted even when
  no ``x-ms-query-source-authorization`` header is available.
- ``webParameters.domains`` is emitted only when at least one allow or
  block domain is configured, and parses comma / newline lists.
- ``_normalize_references`` maps the generic ``{title, url, snippet}``
  Bing-style shape to the shared ``{title, link, content}`` contract.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def text(self):
        import json
        return json.dumps(self._payload)

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status
        self.captured = {}

    def post(self, url, headers=None, json=None):  # noqa: A002
        self.captured = {"url": url, "headers": headers, "json": json}
        return _FakeResponse(self._payload, self._status)


def _build_client(payload, status=200, config_overrides=None):
    from connectors import foundry_iq

    values = {
        "KNOWLEDGE_BASE_ENDPOINT": "https://fake-search.search.windows.net",
        "SEARCH_SERVICE_QUERY_ENDPOINT": "https://fake-search.search.windows.net",
        "KNOWLEDGE_BASE_NAME": "kb-test",
        "FOUNDRY_IQ_API_VERSION": "2026-05-01-preview",
        "FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME": "",
        "FOUNDRY_IQ_KNOWLEDGE_SOURCE_KIND": "",
        "FOUNDRY_IQ_PATTERN": "",
        "FOUNDRY_IQ_FILTER_ADD_ON_ENABLED": False,
        "FOUNDRY_IQ_SECURITY_FIELD_NAME": "metadata_security_id",
        "FOUNDRY_IQ_MAX_OUTPUT_DOCUMENTS": None,
        "FOUNDRY_IQ_FORWARD_SOURCE_AUTH": True,
        "FOUNDRY_IQ_CONVERSATION_UPLOAD_ENABLED": False,
        "FOUNDRY_IQ_CONVERSATION_KNOWLEDGE_SOURCE_NAME": "",
        "FOUNDRY_IQ_MAX_RUNTIME_SECONDS": 120,
        "WORK_IQ_ENABLED": False,
        "WORK_IQ_KNOWLEDGE_SOURCE_NAME": "",
        "FABRIC_IQ_ENABLED": False,
        "FABRIC_IQ_KNOWLEDGE_SOURCE_NAME": "",
        "FABRIC_DATA_AGENT_ENABLED": False,
        "FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME": "",
        "WEB_GROUNDING_ENABLED": False,
        "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME": "",
        "WEB_GROUNDING_ALLOWED_DOMAINS": "",
        "WEB_GROUNDING_BLOCKED_DOMAINS": "",
    }
    values.update(config_overrides or {})
    cfg = MagicMock()
    cfg.get.side_effect = lambda key, default=None, type=str: values.get(key, default)  # noqa: A002
    cfg.aiocredential = MagicMock()
    cfg.aiocredential.get_token = AsyncMock(return_value=SimpleNamespace(token="svc-token"))

    with patch("connectors.foundry_iq.get_config", return_value=cfg):
        client = foundry_iq.FoundryIQClient()
    session = _FakeSession(payload, status)
    client._get_session = AsyncMock(return_value=session)
    return client, session


_SAMPLE_PAYLOAD = {"references": []}


# ---------------------------------------------------------------------------
# maxRuntimeInSeconds: web grounding counts toward the runtime ceiling.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_runtime_emitted_when_web_grounding_enabled():
    client, session = _build_client(
        _SAMPLE_PAYLOAD,
        config_overrides={
            "WEB_GROUNDING_ENABLED": True,
            "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME": "web-ks",
        },
    )
    await client.retrieve("hello")
    assert session.captured["json"]["maxRuntimeInSeconds"] == 120


# ---------------------------------------------------------------------------
# Web knowledge source emission
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_web_not_emitted_when_disabled():
    client, session = _build_client(
        _SAMPLE_PAYLOAD,
        config_overrides={
            "FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME": "documents-blob-ks",
            "WEB_GROUNDING_ENABLED": False,
            "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME": "web-ks",
        },
    )
    await client.retrieve("hello")
    sources = session.captured["json"].get("knowledgeSourceParams", [])
    assert not any(s.get("kind") == "web" for s in sources)


@pytest.mark.asyncio
async def test_web_not_emitted_when_name_missing():
    client, session = _build_client(
        _SAMPLE_PAYLOAD,
        config_overrides={
            "WEB_GROUNDING_ENABLED": True,
            "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME": "",
        },
    )
    await client.retrieve("hello")
    sources = session.captured["json"].get("knowledgeSourceParams", [])
    assert not any(s.get("kind") == "web" for s in sources)


@pytest.mark.asyncio
async def test_web_emitted_when_enabled_without_domains():
    """Web grounding requires no OBO and no ACL; when no allow/block lists
    are configured, ``webParameters`` is omitted so the body stays minimal."""
    client, session = _build_client(
        _SAMPLE_PAYLOAD,
        config_overrides={
            "WEB_GROUNDING_ENABLED": True,
            "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME": "web-ks",
        },
    )
    await client.retrieve("hello")
    sources = session.captured["json"]["knowledgeSourceParams"]
    web = [s for s in sources if s.get("kind") == "web"]
    assert web == [
        {
            "knowledgeSourceName": "web-ks",
            "kind": "web",
            "includeReferences": True,
            "includeReferenceSourceData": True,
        }
    ]


@pytest.mark.asyncio
async def test_web_emitted_with_allowed_and_blocked_domains():
    client, session = _build_client(
        _SAMPLE_PAYLOAD,
        config_overrides={
            "WEB_GROUNDING_ENABLED": True,
            "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME": "web-ks",
            "WEB_GROUNDING_ALLOWED_DOMAINS": "learn.microsoft.com, azure.microsoft.com",
            "WEB_GROUNDING_BLOCKED_DOMAINS": "example.com",
        },
    )
    await client.retrieve("hello")
    web = [
        s for s in session.captured["json"]["knowledgeSourceParams"]
        if s.get("kind") == "web"
    ]
    assert len(web) == 1
    assert web[0]["webParameters"] == {
        "domains": {
            "allowedDomains": ["learn.microsoft.com", "azure.microsoft.com"],
            "blockedDomains": ["example.com"],
        }
    }


@pytest.mark.asyncio
async def test_web_domain_list_parses_newlines_and_dedupes():
    client, session = _build_client(
        _SAMPLE_PAYLOAD,
        config_overrides={
            "WEB_GROUNDING_ENABLED": True,
            "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME": "web-ks",
            "WEB_GROUNDING_ALLOWED_DOMAINS": "Learn.Microsoft.com\nazure.microsoft.com,learn.microsoft.com",
        },
    )
    await client.retrieve("hello")
    web = [
        s for s in session.captured["json"]["knowledgeSourceParams"]
        if s.get("kind") == "web"
    ]
    # Lower-cased, de-duplicated, order preserved.
    assert web[0]["webParameters"]["domains"]["allowedDomains"] == [
        "learn.microsoft.com",
        "azure.microsoft.com",
    ]
    assert "blockedDomains" not in web[0]["webParameters"]["domains"]


@pytest.mark.asyncio
async def test_web_never_carries_filter_add_on():
    """Public data - no ACL and no filterAddOn, even with user_context."""
    client, session = _build_client(
        _SAMPLE_PAYLOAD,
        config_overrides={
            "WEB_GROUNDING_ENABLED": True,
            "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME": "web-ks",
        },
    )
    await client.retrieve(
        "hello",
        conversation_id="conv-1",
        user_context={"principal_id": "user-1", "groups": ["g-a"]},
    )
    web = [
        s for s in session.captured["json"]["knowledgeSourceParams"]
        if s.get("kind") == "web"
    ]
    assert len(web) == 1
    assert "filterAddOn" not in web[0]


@pytest.mark.asyncio
async def test_web_emitted_without_obo_token():
    """Web grounding must work anonymously: no OBO required."""
    client, session = _build_client(
        _SAMPLE_PAYLOAD,
        config_overrides={
            "FOUNDRY_IQ_KNOWLEDGE_SOURCE_NAME": "documents-blob-ks",
            "WEB_GROUNDING_ENABLED": True,
            "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME": "web-ks",
        },
    )
    await client.retrieve("hello")  # no obo_token

    sources = session.captured["json"]["knowledgeSourceParams"]
    kinds = [s.get("kind") for s in sources]
    assert "web" in kinds
    assert "azureBlob" in kinds


@pytest.mark.asyncio
async def test_web_coexists_with_all_remote_kinds():
    """workIQ + fabricOntology + fabricDataAgent + web must all be emitted."""
    client, session = _build_client(
        _SAMPLE_PAYLOAD,
        config_overrides={
            "WORK_IQ_ENABLED": True,
            "WORK_IQ_KNOWLEDGE_SOURCE_NAME": "workiq-ks",
            "FABRIC_IQ_ENABLED": True,
            "FABRIC_IQ_KNOWLEDGE_SOURCE_NAME": "fabric-ks",
            "FABRIC_DATA_AGENT_ENABLED": True,
            "FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME": "fda-ks",
            "WEB_GROUNDING_ENABLED": True,
            "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME": "web-ks",
        },
    )
    await client.retrieve("hello", obo_token="user-obo")
    kinds = [s.get("kind") for s in session.captured["json"]["knowledgeSourceParams"]]
    assert "workIQ" in kinds
    assert "fabricOntology" in kinds
    assert "fabricDataAgent" in kinds
    assert "web" in kinds


@pytest.mark.parametrize("obo_token", [None, "synthetic-user-obo"], ids=["missing", "present"])
async def test_remote_sources_require_obo_but_web_does_not(obo_token):
    client, session = _build_client(
        _SAMPLE_PAYLOAD,
        config_overrides={
            "WORK_IQ_ENABLED": True,
            "WORK_IQ_KNOWLEDGE_SOURCE_NAME": "workiq-ks",
            "FABRIC_IQ_ENABLED": True,
            "FABRIC_IQ_KNOWLEDGE_SOURCE_NAME": "fabric-ks",
            "FABRIC_DATA_AGENT_ENABLED": True,
            "FABRIC_DATA_AGENT_KNOWLEDGE_SOURCE_NAME": "fda-ks",
            "SHAREPOINT_REMOTE_ENABLED": True,
            "SHAREPOINT_REMOTE_KNOWLEDGE_SOURCE_NAME": "sharepoint-ks",
            "WEB_GROUNDING_ENABLED": True,
            "WEB_GROUNDING_KNOWLEDGE_SOURCE_NAME": "web-ks",
        },
    )
    await client.retrieve("hello", obo_token=obo_token)
    kinds = {source["kind"] for source in session.captured["json"]["knowledgeSourceParams"]}
    remote_kinds = {"workIQ", "fabricOntology", "fabricDataAgent", "remoteSharePoint"}
    assert "web" in kinds
    assert kinds & remote_kinds == (remote_kinds if obo_token else set())
    headers = session.captured["headers"]
    if obo_token:
        assert headers["x-ms-query-source-authorization"] == obo_token
    else:
        assert "x-ms-query-source-authorization" not in headers


# ---------------------------------------------------------------------------
# _normalize_references for the web shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normalize_web_reference_shape():
    """Web references use the generic ``{title, url, snippet}`` Bing-style
    contract. Empty payloads are dropped, and the title falls back to the
    URL tail when the server omits it."""
    payload = {
        "references": [
            {
                "type": "web",
                "sourceData": {
                    "title": "Azure OpenAI Service - Documentation",
                    "url": "https://learn.microsoft.com/azure/ai-services/openai/",
                    "snippet": "Azure OpenAI Service provides REST API access...",
                },
            },
            {
                # Missing title - derived from URL tail.
                "type": "web",
                "sourceData": {
                    "url": "https://azure.microsoft.com/products/ai-foundry",
                    "snippet": "Build agents with Azure AI Foundry.",
                },
            },
            {
                # Empty payload must be dropped.
                "type": "web",
                "sourceData": {"title": "", "url": "", "snippet": ""},
            },
            {
                # Alternate field names (name/description) are accepted.
                "type": "web",
                "sourceData": {
                    "name": "Bing Web Result",
                    "displayUrl": "https://example.org/page",
                    "description": "Alternate field names still normalize.",
                },
            },
        ]
    }
    client, _ = _build_client(payload)
    records = await client.retrieve("hello")
    assert records == [
        {
            "title": "Azure OpenAI Service - Documentation",
            "link": "https://learn.microsoft.com/azure/ai-services/openai/",
            "content": "Azure OpenAI Service provides REST API access...",
        },
        {
            "title": "ai-foundry",
            "link": "https://azure.microsoft.com/products/ai-foundry",
            "content": "Build agents with Azure AI Foundry.",
        },
        {
            "title": "Bing Web Result",
            "link": "https://example.org/page",
            "content": "Alternate field names still normalize.",
        },
    ]
