from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

class OrchestratorRequest(BaseModel):
    # Core ask/question fields
    ask: Optional[str] = Field(
        None,
        description="Main user question or request. Optional here to allow pure feedback payloads; the API enforces it for non-feedback.",
    )
    question: Optional[str] = Field(
        None,
        description="Alias for 'ask', kept for backward compatibility. (Optional)"
    )

    # Operation type (e.g., feedback)
    type: Optional[str] = Field(
        None,
        description="Operation type. When set to 'feedback', the request is treated as a feedback submission.",
        json_schema_extra={"example": "feedback"},
    )

    question_id: Optional[str] = Field(
        None,
        description="Identifier of the question within the conversation for which feedback is provided. (Optional)",
    )
    is_positive: Optional[bool] = Field(
        None,
        description="Thumbs up/down style flag for feedback. (Optional)",
    )
    stars_rating: Optional[int] = Field(
        None,
        ge=1,
        le=5,
        description="Star rating from 1 to 5. (Optional)",
    )
    feedback_text: Optional[str] = Field(
        None,
        description="Free-form feedback text. (Optional)",
    )
    conversation_id: Optional[str] = Field(
        None,
        description="Conversation identifier for keeping context between requests. (Optional)",
        json_schema_extra={"example": "8db90ba1-aa03-494e-a46e-efddf7cb4277"}
    )
    client_principal_id: Optional[str] = Field(
        None,
        description="[DEPRECATED] Unique ID of the authenticated user.",
        json_schema_extra={"example": "3d18e02b-d957-4cc5-85e6-e595cd53eec6"}
    )
    client_principal_name: Optional[str] = Field(
        None,
        description="[DEPRECATED] Display name of the authenticated user.",
        json_schema_extra={"example": "jdoe@microsoft.com"}
    )
    client_group_names: Optional[List[str]] = Field(
        default_factory=list,
        description="[DEPRECATED] List of groups the user belongs to.",
        json_schema_extra={"example": ['project-a', 'admins']}
    )
    user_context: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Custom user context to pass along to orchestrator. (Optional)",
        json_schema_extra={"example": {}}
    )

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "summary": "Ask a question",
                    "value": {
                        "ask": "How often are performance reviews conducted at Contoso Electronics?",
                        "conversation_id": "8db90ba1-aa03-494e-a46e-efddf7cb4277",
                        "user_context": {}
                    },
                },
                {
                    "summary": "Submit feedback",
                    "value": {
                        "type": "feedback",
                        "conversation_id": "8db90ba1-aa03-494e-a46e-efddf7cb4277",
                        "question_id": "q-123",
                        "is_positive": True,
                        "stars_rating": 5,
                        "feedback_text": "Great answer, concise and accurate."
                    },
                },
            ]
        }


class ConversationMetadata(BaseModel):
    """Metadata for a conversation in list view (no message content)."""
    id: str = Field(..., description="Unique identifier of the conversation")
    name: Optional[str] = Field(None, description="User-provided name for the conversation")
    # ``validation_alias`` accepts the Cosmos field on input (``_ts`` /
    # ``lastUpdated``) while serialization always uses the field name, so
    # the dashboard frontend sees ``created_at`` / ``last_updated`` in JSON.
    # See #241 (Bug 1).
    created_at: Optional[datetime] = Field(None, validation_alias="_ts", description="Timestamp when the conversation was created")
    last_updated: Optional[datetime] = Field(None, validation_alias="lastUpdated", description="Timestamp when the last message was added")

    class Config:
        populate_by_name = True


class ConversationListResponse(BaseModel):
    """Response for GET /conversations list endpoint."""
    conversations: List[ConversationMetadata] = Field(default_factory=list, description="List of conversations with metadata only")
    has_more: bool = Field(..., description="Whether there are more conversations available")
    skip: int = Field(..., description="Number of conversations skipped (pagination offset)")
    limit: int = Field(..., description="Maximum number of conversations returned")


class ConversationDetail(BaseModel):
    """Full conversation details including all messages."""
    id: str = Field(..., description="Unique identifier of the conversation")
    name: Optional[str] = Field(None, description="User-provided name for the conversation")
    principal_id: Optional[str] = Field(None, description="User ID who owns this conversation")
    created_at: Optional[datetime] = Field(None, validation_alias="_ts", description="Timestamp when the conversation was created")
    last_updated: Optional[datetime] = Field(None, validation_alias="lastUpdated", description="Timestamp when the last message was added")
    messages: Optional[List[Dict[str, Any]]] = Field(default_factory=list, description="List of messages in the conversation")

    class Config:
        populate_by_name = True


class ConversationUpdateRequest(BaseModel):
    """Request body for updating conversation metadata."""
    name: str = Field(..., min_length=1, description="New conversation name")


class ConversationUpdateResponse(BaseModel):
    """Response for PATCH /conversations/{id}."""
    id: str = Field(..., description="Unique identifier of the conversation")
    name: str = Field(..., description="Updated conversation name")
    last_updated: Optional[datetime] = Field(None, validation_alias="lastUpdated", description="Updated timestamp")

    class Config:
        populate_by_name = True


# ---------------------------------------------------------------------------
# Dashboard schemas
# ---------------------------------------------------------------------------

class DashboardDailyPoint(BaseModel):
    """One bucket of the conversations-per-day series for the overview chart."""
    date: str = Field(..., description="UTC date in YYYY-MM-DD format")
    count: int = Field(..., ge=0, description="Number of conversations created on that date")


class DashboardOverview(BaseModel):
    """Aggregated metrics shown on the dashboard Overview tab."""
    total: int = Field(..., ge=0, description="Total non-deleted conversations in the store")
    today: int = Field(..., ge=0, description="Conversations created in the last 24 hours")
    last_7_days: int = Field(..., ge=0, description="Conversations created in the last 7 days")
    last_30_days: int = Field(..., ge=0, description="Conversations created in the last 30 days")
    active_users: int = Field(..., ge=0, description="Distinct principal_ids that started a conversation in the window")
    avg_turns: float = Field(..., ge=0, description="Average user turns per conversation in the window")
    conversations_per_day: List[DashboardDailyPoint] = Field(
        default_factory=list, description="Dense daily series across the requested window"
    )
    window_days: int = Field(..., ge=1, description="Size of the selected window in days")
    # Range fields are present for both default and custom windows so the
    # frontend can label the chart and Active users card consistently
    # (#241 — operator-selectable time range).
    from_: Optional[str] = Field(None, alias="from", description="Window start (UTC YYYY-MM-DD)")
    to: Optional[str] = Field(None, description="Window end (UTC YYYY-MM-DD)")
    in_window_count: Optional[int] = Field(
        None, ge=0, description="Conversations created within the selected window"
    )

    model_config = ConfigDict(populate_by_name=True)


class DashboardConversationSummary(BaseModel):
    """Compact conversation row used in the dashboard Conversations table."""
    id: str = Field(..., description="Conversation identifier")
    name: Optional[str] = Field(None, description="User-provided name, if any")
    principal_id: Optional[str] = Field(None, description="Owner principal id")
    created_at: Optional[int] = Field(None, validation_alias="_ts", description="Unix epoch seconds when the conversation was created")
    last_updated: Optional[str] = Field(None, validation_alias="lastUpdated", description="Last activity timestamp (ISO 8601)")
    message_count: Optional[int] = Field(None, description="Number of messages stored in the conversation")

    class Config:
        populate_by_name = True


class DashboardConversationListResponse(BaseModel):
    """Paginated list response for the dashboard Conversations tab."""
    conversations: List[DashboardConversationSummary] = Field(default_factory=list)
    has_more: bool = Field(..., description="Whether more results exist beyond this page")
    skip: int = Field(..., ge=0)
    limit: int = Field(..., ge=1)


class DashboardConversationDetail(BaseModel):
    """Full conversation document returned by the dashboard detail view.

    Message bodies are NOT stored in Cosmos -- the orchestrator only persists
    user prompts under ``questions[]`` and delegates the full transcript
    (assistant replies, tool calls) to the Azure AI Foundry agent thread.
    The dashboard reconstructs the user turns from ``questions[]`` and
    surfaces ``thread_id`` so the frontend can render a friendly note and a
    deep-link to Foundry instead of showing empty cards (#247 Bug 4).
    """
    id: str
    name: Optional[str] = None
    principal_id: Optional[str] = None
    created_at: Optional[int] = Field(None, validation_alias="_ts")
    last_updated: Optional[str] = Field(None, validation_alias="lastUpdated")
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    thread_id: Optional[str] = Field(
        None,
        description=(
            "Azure AI Foundry agent thread id holding the full transcript, "
            "when one was persisted at orchestration time."
        ),
    )
    feedback: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Feedback entries captured on the conversation, if any.",
    )

    class Config:
        populate_by_name = True


class DashboardVersionResponse(BaseModel):
    """Tiny response used by the frontend to render the version chip in the header."""
    version: str


class DashboardAuthConfigResponse(BaseModel):
    """Runtime auth configuration for the dashboard SPA.

    Returned by the unauthenticated ``GET /api/dashboard/auth-config`` endpoint
    so the SPA can decide, at load time, whether to bootstrap MSAL and which
    tenant/client/scope to sign the user in against. Values are computed
    server-side so the browser never has to concatenate strings that must
    match app-registration configuration.

    When auth is off (``OAUTH_AZURE_AD_TENANT_ID`` unset) only ``auth_enabled``
    is returned; when auth is on the full quartet is included.
    """

    auth_enabled: bool = Field(
        ...,
        description="Whether Entra ID auth is enabled for the orchestrator.",
    )
    client_id: Optional[str] = Field(
        None,
        description="Entra ID application (client) id for the API app registration.",
    )
    tenant_id: Optional[str] = Field(
        None,
        description="Entra ID tenant id.",
    )
    authority: Optional[str] = Field(
        None,
        description="Full MSAL authority URL (login.microsoftonline.com/<tenant>).",
    )
    api_scope: Optional[str] = Field(
        None,
        description="Delegated scope the SPA must request to call this API.",
    )


# ---------------------------------------------------------------------------
# Configuration tab (admin-only)
# ---------------------------------------------------------------------------

class DashboardSettingOption(BaseModel):
    """One entry in an enum dropdown rendered on the Configuration tab."""
    value: str
    label: str
    description: str


class DashboardSettingField(BaseModel):
    """A single editable setting along with its current value and metadata."""
    key: str = Field(..., description="App Configuration key")
    type: str = Field(..., description="One of 'enum', 'bool', 'int', 'float'")
    value: Any = Field(..., description="Current value typed per `type`")
    default: Any = Field(..., description="Default applied when the key is absent")
    label: str = Field(..., description="Short human-readable name")
    description: str = Field(..., description="Tooltip body explaining the setting")
    options: Optional[List[DashboardSettingOption]] = Field(
        None, description="Allowed values for enum fields"
    )
    min: Optional[float] = Field(None, description="Inclusive lower bound for numeric fields")
    max: Optional[float] = Field(None, description="Inclusive upper bound for numeric fields")
    step: Optional[float] = Field(None, description="Recommended UI step for numeric fields")
    unit: Optional[str] = Field(None, description="Optional unit suffix, e.g. 'tokens'")


class DashboardSettingSection(BaseModel):
    """A group of related settings rendered as one card."""
    id: str
    label: str
    description: str
    settings: List[DashboardSettingField]


class DashboardConfigResponse(BaseModel):
    """Full payload for the Configuration tab."""
    label: str = Field(..., description="App Configuration label written to on PUT")
    sections: List[DashboardSettingSection]


class DashboardConfigUpdateItem(BaseModel):
    """One key/value pair sent by the Save action."""
    key: str
    value: Any


class DashboardConfigUpdateRequest(BaseModel):
    """Body for ``PUT /api/dashboard/config``."""
    settings: List[DashboardConfigUpdateItem] = Field(default_factory=list)


class DashboardConfigFieldError(BaseModel):
    """Per-key validation or write error returned with 422 / 500."""
    key: str
    error: str


class DashboardConfigErrorResponse(BaseModel):
    """422 / 500 payload describing what went wrong, per-key."""
    errors: List[DashboardConfigFieldError]


class DashboardConfigRefreshResponse(BaseModel):
    """Body returned by ``POST /api/dashboard/config/refresh``."""
    status: str = "refreshed"
    detail: Optional[str] = None


class DashboardConfigApplyResponse(BaseModel):
    """Body returned by ``POST /api/dashboard/config/apply``.

    The endpoint name says what it does: refresh the in-process cache so
    subsequent requests pick up new values. The orchestrator does not perform
    a container restart here — see the endpoint docstring for the rationale.
    """
    status: str
    detail: str


# Reusable OpenAPI responses for the orchestrator endpoint. Put here so
# route decorators in `main.py` remain compact and the examples are centralized.
ORCHESTRATOR_RESPONSES = {
    200: {
        "description": "OK — successful stream response (SSE). Each SSE 'data:' line contains a JSON object.",
        "content": {
            "text/event-stream": {
                "example": "agent answer"
            }
        }
    },
    400: {
        "description": "Bad Request — missing or invalid fields",
        "content": {
            "application/json": {
                "example": {"detail": "No 'ask' or 'question' field in request body"}
            }
        }
    },
    401: {
        "description": "Unauthorized — missing or invalid credentials",
        "content": {
            "application/json": {
                "example": {"detail": "Missing or invalid API key"}
            }
        }
    },
    403: {
        "description": "Forbidden — authenticated but not allowed to perform this action",
        "content": {
            "application/json": {
                "example": {"detail": "You are not authorized to perform this action"}
            }
        }
    },
    422: {
        "description": "Validation Error — request body did not match schema",
        "content": {
            "application/json": {
                "example": {"detail": [{"loc": ["body", "ask"], "msg": "field required", "type": "value_error.missing"}]}
            }
        }
    }
}
