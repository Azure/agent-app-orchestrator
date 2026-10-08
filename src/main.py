import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from collections.abc import Mapping

import uvicorn
from fastapi import FastAPI, Request, Response, HTTPException, Depends, Header
from fastapi.responses import StreamingResponse

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from dotenv import load_dotenv

# Load local .env variables into os.environ before anything else starts
load_dotenv()

from orchestration.orchestrator import Orchestrator
from orchestration.turn import TurnErrorEvent, TurnRequest
from api.turn_sse import serialize_turn_event
from connectors.appconfig import AppConfigClient
from connectors.cosmosdb import (
    query_user_conversations,
    read_user_conversation,
    update_conversation_name,
    soft_delete_conversation,
)
from dependencies import get_config, validate_auth, validate_access_token
from telemetry import AuditEmitter, Telemetry, new_correlation_id
from schemas import (
    OrchestratorRequest,
    ORCHESTRATOR_RESPONSES,
    ConversationListResponse,
    ConversationMetadata,
    ConversationDetail,
    ConversationUpdateRequest,
    ConversationUpdateResponse,
)
from constants import APPLICATION_INSIGHTS_CONNECTION_STRING
from util.tools import is_azure_environment
from util.jwt_utils import extract_bearer_token

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ----------------------------------------
# Initialization and logging
# - Minimal early logging so config/auth warnings are visible during startup
# - Azure SDK and HTTP pipeline logs are verbose only when LOG_LEVEL=DEBUG;
#   otherwise they’re kept at WARNING to reduce noise
# ----------------------------------------

## Early minimal logging (INFO) until config is loaded; refined by Telemetry.configure_basic
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

# Suppress Azure SDK HTTP logging immediately (before any Azure imports)
for _azure_logger in [
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "azure.core",
    "azure"
]:
    logger = logging.getLogger(_azure_logger)
    logger.setLevel(logging.CRITICAL)
    logger.propagate = False
    logger.disabled = True
    logger.handlers.clear()
    
# Load version from VERSION file 
VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
try:
    APP_VERSION = VERSION_FILE.read_text().strip()
except FileNotFoundError:
    APP_VERSION = "0.0.0"


# Identity and authorization keys that only the server may populate in user_context.
_SERVER_TRUSTED_USER_CONTEXT_KEYS = frozenset(
    {
        "principal_id",
        "principal_name",
        "user_name",
        "oid",
        "user_id",
        "security_ids",
        "groups",
        "group_ids",
        "client_group_names",
    }
)


def _startup_banner() -> None:
    name = "Agent Landing Zone Orchestrator"
    version = None
    try:
        if VERSION_FILE.exists():
            version = VERSION_FILE.read_text().strip() or None
    except (OSError, UnicodeError):
        logging.warning("[Startup] Could not read version file; omitting banner version")

    title = f"{name}{(' v' + version) if version else ''}"
    banner_lines = [
        "",
        "╔══════════════════════════════════════════════╗",
        f"║  {title}".ljust(47) + "║",
        "║  FastAPI Orchestrator API                    ║",
        "╚══════════════════════════════════════════════╝",
        "",
    ]
    for line in banner_lines:
        logging.info(line)


def _format_request_debug(
    request: Request,
    conversation_id: Optional[str],
    question_id: Optional[str],
    op_type: Optional[str],
) -> str:
    # NOTE: keep this safe. Do not log secrets/tokens.
    sensitive_markers = (
        "authorization",
        "cookie",
        "set-cookie",
        "token",
        "apikey",
        "api-key",
        "secret",
        "password",
    )
    # A small allowlist of headers we often want to see fully.
    safe_value_allowlist = {
        "host",
        "user-agent",
        "content-type",
        "content-length",
        "accept",
        "accept-encoding",
        "accept-language",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        "x-request-id",
        "traceparent",
        "tracestate",
        "x-correlation-id",
    }

    def _is_sensitive(name: str) -> bool:
        n = name.lower()
        return any(m in n for m in sensitive_markers)

    def _redact(name: str, value: str) -> str:
        # Show presence + length only.
        return f"<redacted len={len(value)}>"

    # Pull a few request metadata fields
    method = request.method
    path = request.url.path
    client_host = getattr(getattr(request, "client", None), "host", None)

    # Normalize headers into a simple dict
    hdrs: Mapping[str, str] = request.headers
    header_lines: list[str] = []
    for k in sorted(hdrs.keys(), key=lambda s: s.lower()):
        v = hdrs.get(k, "")
        if not v:
            continue
        kl = k.lower()
        if _is_sensitive(kl):
            header_lines.append(f"  - {k}: {_redact(kl, v)}")
        elif kl in safe_value_allowlist:
            header_lines.append(f"  - {k}: {v}")
        else:
            # For non-sensitive, non-allowlisted headers, log a short preview.
            preview = (v[:120] + "…") if len(v) > 120 else v
            header_lines.append(f"  - {k}: {preview}")

    req_id = hdrs.get("x-request-id") or hdrs.get("x-correlation-id") or None
    traceparent = hdrs.get("traceparent") or None

    lines = [
        "[Orchestrator] ── Request Debug ─────────────────────────────",
        f"method: {method}",
        f"path:   {path}",
        f"client: {client_host or 'unknown'}",
        f"type:   {op_type or 'ask'}",
        f"conversation_id: {conversation_id or '∅'}",
        f"question_id:     {question_id or '∅'}",
    ]
    if req_id:
        lines.append(f"request_id: {req_id}")
    if traceparent:
        lines.append(f"traceparent: {traceparent}")

    lines.append("headers:")
    lines.extend(header_lines if header_lines else ["  (none)"])
    lines.append("[Orchestrator] ─────────────────────────────────────────────")
    return "\n".join(lines)

# 2) Create configuration client (sets cfg.auth_failed=True if auth is unavailable)
cfg: AppConfigClient = get_config()

# 3) Configure logging level/format from LOG_LEVEL
Telemetry.configure_basic(cfg)
Telemetry.log_log_level_diagnostics(cfg)

# Reduce noise from low-level HTTP libraries unless explicitly requested.
# When LOG_LEVEL=DEBUG we still want app debug, but httpcore/httpx/urllib3 can overwhelm logs.
if os.getenv("HTTP_CLIENT_DEBUG", "false").lower() not in ("1", "true", "yes"):
    for _noisy in [
        "httpcore",
        "httpcore.connection",
        "httpcore.http11",
        "httpx",
        "urllib3",
        "urllib3.connectionpool",
    ]:
        logging.getLogger(_noisy).setLevel(logging.WARNING)

# 4) If authentication failed, exit immediately
if getattr(cfg, "auth_failed", False):
    logging.warning("The orchestrator is not authenticated (run 'az login' or configure Managed Identity). Exiting...")
    logging.shutdown()
    os._exit(1)

# ----------------------------------------
# Create FastAPI app with lifespan
# ----------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    import time
    _startup_banner()
    AuditEmitter.configure(
        cfg,
        service_name="agent-app-orchestrator",  # audit-event-v2 producer name
        service_version=APP_VERSION,
    )
    Telemetry.configure_monitoring(
        cfg,
        APPLICATION_INSIGHTS_CONNECTION_STRING,
        "agentlz.orchestrator",  # OTel service.name (Azure/agent-landing-zone#695)
        APP_VERSION,
    )
    
    # ---------------------------------------------------------
    # Pre-warming Clients to reduce Cold Start Latency
    # ---------------------------------------------------------
    logging.info("[Startup] Pre-warming clients to eliminate cold-start latency...")
    warmup_start = time.time()
    try:
        from connectors.search import get_search_client
        from connectors.cosmosdb import get_cosmosdb_client
        from connectors.identity_manager import get_identity_manager
        # 1. Warm up Identity Singleton
        identity_manager = get_identity_manager()
        credential = identity_manager.get_aio_credential()

        # 2. Pre-fetch Azure Identity Token silently (warms up AAD/MSAL)
        try:
            logging.info("[Startup] Pre-fetching Azure Search Entra ID token...")
            await credential.get_token("https://search.azure.com/.default")
        except Exception:
            logging.warning("[Startup] Token pre-fetch failed; continuing optional warmup")

        # 3. Warm up SearchClient singleton (reused across all requests)
        search_client = get_search_client()
        logging.info("[Startup] Waking up Azure AI Search Index and checking empty status...")
        await search_client.is_index_empty()

        # 4. Warm up CosmosDBClient singleton (persistent connection)
        get_cosmosdb_client()

        # 5. Pre-warm Agent Service only for strategies that use it.
        try:
            from startup_warmup import prewarm_agents_for_strategy
            await prewarm_agents_for_strategy(cfg)
        except Exception:
            logging.warning("[Startup] AgentsClient pre-warm failed; startup will proceed")

        logging.info(f"[Startup] ✅ Application pre-warming completed in {time.time() - warmup_start:.2f}s")
    except Exception:
        logging.warning("[Startup] Pre-warming encountered an issue, but startup will proceed")

    yield  # <-- application runs here
    # cleanup logic after shutdown

app = FastAPI(
    title="Agent Landing Zone Orchestrator",
    description="Agent Landing Zone Orchestrator FastAPI",
    version=APP_VERSION,
    lifespan=lifespan
)

@app.post(
    "/orchestrator",
    dependencies=[Depends(validate_auth)],
    summary="Ask orchestrator a question",
    response_description="Returns the orchestrator’s response in real time, streamed via SSE.",
    responses=ORCHESTRATOR_RESPONSES
)
async def orchestrator_endpoint(
    request: Request,
    response: Response,
    body: OrchestratorRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-KEY"),
    dapr_api_token: Optional[str] = Header(None, alias="dapr-api-token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """
    Accepts JSON payload with ask/question, optional conversation_id and context,
    then streams back an answer via SSE.
    """

    correlation_id = new_correlation_id()
    request.state.correlation_id = correlation_id
    response.headers["X-Correlation-ID"] = correlation_id

    # Determine operation type first (defensive: body may not include type)
    op_type = getattr(body, "type", None)

    import time
    request_start_time = time.time()
    auth_start_time = request_start_time

    # Anonymous-mode toggle (mirrors frontend behavior):
    # - If ALLOW_ANONYMOUS=true, requests without Authorization can proceed as anonymous.
    # - If ALLOW_ANONYMOUS=false, Authorization is required (401 when missing).
    # - If Entra auth isn't configured (tenant/client id missing), ALLOW_ANONYMOUS controls whether to proceed.
    _tenant_id = (cfg.get("OAUTH_AZURE_AD_TENANT_ID", default="") or "").strip()
    _client_id = (
        (cfg.get("OAUTH_AZURE_AD_CLIENT_ID", default="") or "").strip()
        or (cfg.get("CLIENT_ID", default="") or "").strip()
    )
    auth_configured = bool(_tenant_id and _client_id)

    # Default to allowing anonymous access unless explicitly disabled.
    allow_anonymous = cfg.get("ALLOW_ANONYMOUS", default=True, type=bool)

    # For troubleshooting, track the auth decision taken for this request.
    auth_header_present = bool(authorization)
    auth_decision = "unknown"

    # Pretty request/header logging (DEBUG only)
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        try:
            logging.debug(
                _format_request_debug(
                    request=request,
                    conversation_id=getattr(body, "conversation_id", None),
                    question_id=getattr(body, "question_id", None),
                    op_type=op_type,
                )
            )
        except Exception:
            # Never fail the request due to logging
            logging.debug("[Orchestrator] Failed to render request debug info")

    # If Authorization header is provided, validate the access token and apply authorization checks.
    # If Authorization header is missing, treat the request as anonymous only when ALLOW_ANONYMOUS=true.
    user_context = dict(body.user_context or {})
    ignored_identity_keys = sorted(k for k in user_context if k in _SERVER_TRUSTED_USER_CONTEXT_KEYS)
    for key in ignored_identity_keys:
        user_context.pop(key, None)
    if ignored_identity_keys:
        logging.debug(
            "[Orchestrator] Ignoring client-supplied identity keys in user_context: %s",
            ",".join(ignored_identity_keys),
        )
    access_token: Optional[str] = None
    if authorization:
        # If auth isn't configured, decide whether to proceed based on ALLOW_ANONYMOUS.
        if not auth_configured:
            if allow_anonymous:
                auth_decision = "allow_anonymous_auth_not_configured"
                logging.warning(
                    "[Orchestrator] Authorization header provided but Entra auth is not configured; proceeding as anonymous (ALLOW_ANONYMOUS=true)"
                )
                user_context["principal_id"] = "anonymous"
                user_context["principal_name"] = "anonymous"
            else:
                auth_decision = "reject_auth_not_configured"
                logging.warning(
                    "[Orchestrator] Authentication required (ALLOW_ANONYMOUS=false) but Entra auth is not configured (missing OAUTH_AZURE_AD_TENANT_ID/OAUTH_AZURE_AD_CLIENT_ID)"
                )
                raise HTTPException(status_code=401, detail="Authentication is not configured")
        else:
            auth_decision = "validate_bearer_token"
            logging.debug("[Orchestrator] Authorization header provided; validating access token...")

            # Extract Bearer token safely (handles extra whitespace, casing, etc.)
            access_token = extract_bearer_token(authorization)
            if not access_token:
                logging.warning("[Orchestrator] Invalid Authorization header format (expected 'Bearer <token>')")
                raise HTTPException(status_code=401, detail="Invalid Authorization header format")

            logging.debug("[Orchestrator] Access token received, length: %d chars", len(access_token))

            try:
                # Validate token and extract user info
                user_info = await validate_access_token(access_token)
                user_context["principal_id"] = user_info.get("oid")
                user_context["principal_name"] = user_info.get("preferred_username")
                user_context["user_name"] = user_info.get("name")

                logging.debug(
                    "[Orchestrator] User info extracted: OID=%s, Username=%s, Name=%s",
                    user_info.get("oid"),
                    user_info.get("preferred_username"),
                    user_info.get("name"),
                )

                # Check authorization based on user principals/names
                allowed_names = [n.strip() for n in cfg.get("ALLOWED_USER_NAMES", default="").split(",") if n.strip()]
                allowed_ids = [id.strip() for id in cfg.get("ALLOWED_USER_PRINCIPALS", default="").split(",") if id.strip()]

                logging.debug(
                    "[Orchestrator] Authorization policy - allowed_names=%d allowed_ids=%d",
                    len(allowed_names),
                    len(allowed_ids),
                )

                is_authorized = (
                    not (allowed_names or allowed_ids) or
                    user_info.get("preferred_username") in allowed_names or
                    user_info.get("oid") in allowed_ids
                )

                if not is_authorized:
                    # High-signal deny reason (no secrets)
                    deny_reasons = []
                    if allowed_names and user_info.get("preferred_username") not in allowed_names:
                        deny_reasons.append("preferred_username_not_allowed")
                    if allowed_ids and user_info.get("oid") not in allowed_ids:
                        deny_reasons.append("oid_not_allowed")
                    deny_reason = ",".join(deny_reasons) if deny_reasons else "policy_requires_no_restrictions"
                    logging.warning(
                        "[Orchestrator] ❌ Access denied: user_oid=%s user=%s reason=%s",
                        user_info.get("oid"),
                        user_info.get("preferred_username"),
                        deny_reason,
                    )
                    raise HTTPException(status_code=403, detail="You are not authorized to perform this action")

                logging.info(
                    "[Orchestrator] ✅ Authenticated request: conversation_id=%s question_id=%s user=%s oid=%s",
                    getattr(body, "conversation_id", None) or "∅",
                    getattr(body, "question_id", None) or "∅",
                    user_info.get("preferred_username") or user_info.get("oid") or "<unknown>",
                    user_info.get("oid") or "<unknown>",
                )

                auth_decision = "authenticated"
            except HTTPException as e:
                logging.warning(
                    "[Orchestrator] Request rejected: status=%d",
                    e.status_code,
                )
                raise
            except Exception:
                logging.error("[Orchestrator] Error validating user token")
                raise HTTPException(status_code=401, detail="Invalid or expired token")
    else:
        # No Authorization header: allow anonymous only when explicitly enabled.
        if allow_anonymous:
            auth_decision = "allow_anonymous_missing_auth_header"
            logging.debug("[Orchestrator] No Authorization header; treating as anonymous (ALLOW_ANONYMOUS=true)")
            user_context["principal_id"] = "anonymous"
            user_context["principal_name"] = "anonymous"
        else:
            auth_decision = "reject_missing_auth_header"
            # Mirror token-invalid behavior: 401 when auth is required.
            logging.warning(
                "[Orchestrator] Missing Authorization header and ALLOW_ANONYMOUS=false; rejecting request"
            )
            raise HTTPException(status_code=401, detail="Missing Authorization header")

    auth_duration = time.time() - auth_start_time
    logging.info(f"[Timing][main.py] Authentication & Authorization took {auth_duration:.3f}s")

    # One INFO line per request: quickly answers "was this anonymous? who was it?" without DEBUG.
    try:
        principal_name = (user_context.get("principal_name") or "").strip()
        principal_id = (user_context.get("principal_id") or "").strip()
        principal = principal_name or principal_id or "anonymous"
        auth_mode = "authenticated" if (authorization and principal != "anonymous") else "anonymous"
        logging.info(
            "[Orchestrator] Request context: type=%s conversation_id=%s question_id=%s auth=%s principal=%s allow_anonymous=%s auth_configured=%s auth_header=%s decision=%s",
            op_type or "ask",
            getattr(body, "conversation_id", None) or "∅",
            getattr(body, "question_id", None) or "∅",
            auth_mode,
            principal,
            allow_anonymous,
            auth_configured,
            auth_header_present,
            auth_decision,
        )
    except Exception:
        # Never fail due to logging
        logging.warning("[Orchestrator] Failed to render request context")

    logging.debug(
        "[Orchestrator] Request identity resolved: principal_name=%s principal_id=%s",
        user_context.get("principal_name"),
        user_context.get("principal_id"),
    )

    # Feedback submissions: allow missing ask/question; validate only what's required
    if op_type == "feedback":
        # Handle feedback submission
        conversation_id = body.conversation_id
        if not conversation_id:
            logging.error("No 'conversation_id' provided in feedback body")
            raise HTTPException(status_code=400, detail="No 'conversation_id' field in request body")

        # Create orchestrator instance and save feedback
        orchestrator = await Orchestrator.create(
            conversation_id=conversation_id,
            user_context=user_context,
            correlation_id=correlation_id,
        )
        # Build feedback dict defensively; optional fields may be absent
        _qid = getattr(body, "question_id", None)
        feedback = {
            "conversation_id": conversation_id,
            "question_id": _qid,
            "is_positive": getattr(body, "is_positive", None),
            "stars_rating": getattr(body, "stars_rating", None),
            # Normalize empty strings to None
            "feedback_text": (getattr(body, "feedback_text", None) or "").strip() or None,
        }
        await orchestrator.save_feedback(feedback)
        return {"status": "success", "message": "Feedback saved successfully"}    

    # For non-feedback operations, require an ask/question
    ask = (getattr(body, "ask", None) or getattr(body, "question", None))
    if not ask:
        raise HTTPException(status_code=400, detail="No 'ask' or 'question' field in request body")

    turn = TurnRequest(
        ask=ask,
        conversation_id=body.conversation_id,
        question_id=getattr(body, "question_id", None),
        user_context=user_context,
        request_access_token=access_token if authorization else None,
        correlation_id=correlation_id,
    )

    orchestrator_create_start = time.time()
    orchestrator = await Orchestrator.from_turn_request(turn)
    logging.info(f"[Timing][main.py] Orchestrator.create took {time.time() - orchestrator_create_start:.3f}s")

    async def sse_event_generator():
        error_event_emitted = False
        try:
            async for event in orchestrator.stream_turn(turn):
                if isinstance(event, TurnErrorEvent):
                    error_event_emitted = True
                chunk = serialize_turn_event(event)
                if chunk is not None:
                    yield chunk
        except Exception:
            logging.error("Error in SSE generator (internal_error)")
            if not error_event_emitted:
                yield "event: error\ndata: An internal server error occurred.\n\n"

    return StreamingResponse(
        sse_event_generator(),
        media_type="text/event-stream",
        headers={"X-Correlation-ID": correlation_id},
    )


# ----------------------------------------
# Helper function for auth validation on conversation endpoints
# ----------------------------------------
async def validate_user_access(authorization: Optional[str], endpoint_name: str) -> str:
    """
    Validates user authorization and returns principal_id.
    Conversation history endpoints always require authentication.
    
    Returns:
        principal_id (OID) of the authenticated user.
    
    Raises:
        HTTPException: If authorization fails or is invalid.
    """
    # Check if auth is configured
    _tenant_id = (cfg.get("OAUTH_AZURE_AD_TENANT_ID", default="") or "").strip()
    _client_id = (
        (cfg.get("OAUTH_AZURE_AD_CLIENT_ID", default="") or "").strip()
        or (cfg.get("CLIENT_ID", default="") or "").strip()
    )
    auth_configured = bool(_tenant_id and _client_id)

    if not auth_configured:
        logging.warning(f"{endpoint_name} Authentication required but Entra auth is not configured")
        raise HTTPException(status_code=401, detail="Authentication is required for this endpoint but is not configured")

    if not authorization or not authorization.startswith("Bearer "):
        logging.warning(f"{endpoint_name} Missing or invalid Authorization header")
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    access_token = extract_bearer_token(authorization)
    if not access_token:
        raise HTTPException(status_code=401, detail="Invalid Authorization header format")

    try:
        user_info = await validate_access_token(access_token)
        principal_id = user_info.get("oid")

        # Check authorization based on user principals/names
        allowed_names = [n.strip() for n in cfg.get("ALLOWED_USER_NAMES", default="").split(",") if n.strip()]
        allowed_ids = [id.strip() for id in cfg.get("ALLOWED_USER_PRINCIPALS", default="").split(",") if id.strip()]

        is_authorized = (
            not (allowed_names or allowed_ids) or
            user_info.get("preferred_username") in allowed_names or
            principal_id in allowed_ids
        )

        if not is_authorized:
            logging.warning(f"{endpoint_name} Access denied for user %s", principal_id)
            raise HTTPException(status_code=403, detail="You are not authorized to perform this action")

        logging.info(f"{endpoint_name} Authorization successful for user %s", user_info.get("preferred_username"))
        return principal_id

    except HTTPException:
        raise
    except Exception:
        logging.error("%s Error validating user token", endpoint_name)
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# ----------------------------------------
# Conversation History Endpoints
# ----------------------------------------
@app.get(
    "/conversations",
    dependencies=[Depends(validate_auth)],
    summary="List user conversations",
    response_model=ConversationListResponse,
    responses={
        200: {"description": "OK — list of conversations"},
        401: {"description": "Unauthorized — missing or invalid credentials"},
        500: {"description": "Internal Server Error"},
    },
)
async def list_conversations(
    skip: int = 0,
    limit: int = 10,
    name: Optional[str] = None,
    x_api_key: Optional[str] = Header(None, alias="X-API-KEY"),
    dapr_api_token: Optional[str] = Header(None, alias="dapr-api-token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """Retrieve paginated list of conversations for the authenticated user."""
    principal_id = await validate_user_access(authorization, "[ListConversations]")

    try:
        conversation_docs = await query_user_conversations(
            principal_id=principal_id, skip=skip, limit=limit, name=name,
        )
        conversations = [
            ConversationMetadata(
                id=doc.get("id"),
                name=doc.get("name"),
                created_at=doc.get("_ts"),
                last_updated=doc.get("lastUpdated"),
            )
            for doc in conversation_docs
        ]

        has_more = len(conversations) == limit
        return ConversationListResponse(
            conversations=conversations, has_more=has_more, skip=skip, limit=limit,
        )
    except Exception as e:
        logging.error("[ListConversations] Error retrieving conversations (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Error retrieving conversations")


@app.get(
    "/conversations/{conversation_id}",
    dependencies=[Depends(validate_auth)],
    summary="Get specific conversation",
    response_model=ConversationDetail,
    responses={
        200: {"description": "OK — full conversation detail"},
        401: {"description": "Unauthorized"},
        403: {"description": "Forbidden"},
        404: {"description": "Not Found"},
        500: {"description": "Internal Server Error"},
    },
)
async def get_conversation(
    conversation_id: str,
    x_api_key: Optional[str] = Header(None, alias="X-API-KEY"),
    dapr_api_token: Optional[str] = Header(None, alias="dapr-api-token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """Retrieve full details of a specific conversation including all messages."""
    principal_id = await validate_user_access(authorization, "[GetConversation]")

    try:
        conversation_doc = await read_user_conversation(conversation_id, principal_id)
        if conversation_doc is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        if conversation_doc.get("principal_id") != principal_id:
            raise HTTPException(status_code=403, detail="You do not have permission to access this conversation")

        return ConversationDetail(
            id=conversation_doc.get("id"),
            name=conversation_doc.get("name"),
            principal_id=conversation_doc.get("principal_id"),
            created_at=conversation_doc.get("_ts"),
            last_updated=conversation_doc.get("lastUpdated"),
            messages=conversation_doc.get("messages", []),
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error("[GetConversation] Error retrieving conversation (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Error retrieving conversation")


@app.patch(
    "/conversations/{conversation_id}",
    dependencies=[Depends(validate_auth)],
    summary="Update conversation name",
    responses={
        200: {"description": "OK — conversation updated"},
        401: {"description": "Unauthorized"},
        403: {"description": "Forbidden"},
        404: {"description": "Not Found"},
        500: {"description": "Internal Server Error"},
    },
)
async def update_conversation(
    conversation_id: str,
    body: ConversationUpdateRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-KEY"),
    dapr_api_token: Optional[str] = Header(None, alias="dapr-api-token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """Update a conversation's name."""
    principal_id = await validate_user_access(authorization, "[UpdateConversation]")

    try:
        conversation_doc = await read_user_conversation(conversation_id, principal_id)
        if conversation_doc is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        if conversation_doc.get("principal_id") != principal_id:
            raise HTTPException(status_code=403, detail="You do not have permission to access this conversation")

        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="Conversation name cannot be empty")

        updated_doc = await update_conversation_name(conversation_id, principal_id, new_name)
        if updated_doc is None:
            raise HTTPException(status_code=500, detail="Failed to update conversation")

        return ConversationUpdateResponse(
            id=updated_doc.get("id"),
            name=updated_doc.get("name"),
            last_updated=updated_doc.get("lastUpdated"),
        )
    except HTTPException:
        raise
    except Exception as e:
        logging.error("[UpdateConversation] Error updating conversation (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Error updating conversation")


@app.delete(
    "/conversations/{conversation_id}",
    dependencies=[Depends(validate_auth)],
    summary="Delete conversation",
    responses={
        200: {"description": "OK — conversation deleted"},
        401: {"description": "Unauthorized"},
        403: {"description": "Forbidden"},
        404: {"description": "Not Found"},
        500: {"description": "Internal Server Error"},
    },
)
async def delete_conversation(
    conversation_id: str,
    x_api_key: Optional[str] = Header(None, alias="X-API-KEY"),
    dapr_api_token: Optional[str] = Header(None, alias="dapr-api-token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """Soft delete a conversation (marks as deleted without removing data)."""
    principal_id = await validate_user_access(authorization, "[DeleteConversation]")

    try:
        conversation_doc = await read_user_conversation(conversation_id, principal_id)
        if conversation_doc is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        if conversation_doc.get("principal_id") != principal_id:
            raise HTTPException(status_code=403, detail="You do not have permission to access this conversation")

        deleted_doc = await soft_delete_conversation(conversation_id, principal_id)
        if deleted_doc is None:
            raise HTTPException(status_code=500, detail="Failed to delete conversation")

        logging.info("[DeleteConversation] User %s deleted conversation %s", principal_id, conversation_id)
        return {"status": "success", "message": "Conversation deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logging.error("[DeleteConversation] Error deleting conversation (%s)", type(e).__name__)
        raise HTTPException(status_code=500, detail="Error deleting conversation")


# Instrumentation
HTTPXClientInstrumentor().instrument()
FastAPIInstrumentor.instrument_app(app)

# ----------------------------------------
# Admin dashboard (opt-in)
# ----------------------------------------
# Mounted only when ``ENABLE_DASHBOARD`` is true so the dashboard surface
# does not exist by default. The HTML page at ``/dashboard`` is open (the
# SPA renders an access-denied state on 403 from the API). All API routes
# under ``/api/dashboard`` are gated by the Admin app role when auth is on.
_dashboard_enabled = bool(get_config().get("ENABLE_DASHBOARD", default=False, type=bool))
if _dashboard_enabled:
    logging.info("[startup] ENABLE_DASHBOARD=true — mounting admin dashboard at /dashboard")
    from api.dashboard import router as dashboard_router
    from api.dashboard_config import router as dashboard_config_router

    app.include_router(dashboard_router)
    app.include_router(dashboard_config_router)

    _static_dir = Path(__file__).resolve().parent / "static"
    if _static_dir.is_dir():
        _assets_dir = _static_dir / "assets"
        if _assets_dir.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=str(_assets_dir)),
                name="dashboard-assets",
            )

        @app.get("/logo.png", include_in_schema=False)
        async def dashboard_logo():
            return FileResponse(str(_static_dir / "logo.png"))

        @app.get("/dashboard", include_in_schema=False)
        async def dashboard_index():
            return FileResponse(str(_static_dir / "index.html"))
    else:
        logging.warning(
            "[startup] ENABLE_DASHBOARD=true but %s does not exist — frontend not built. "
            "Run 'npm run build' inside frontend/ or rebuild the Docker image.",
            _static_dir,
        )

# Run the app locally (avoid nested event loop when started by uvicorn CLI)
if __name__ == "__main__" and not is_azure_environment():
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="info", timeout_keep_alive=60)
