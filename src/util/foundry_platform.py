"""Foundry hosted-agent container protocol 2.0 header contract.

The Foundry hosted-agent platform gateway drops caller headers outside a
fixed allow-list before a request reaches this container -- most importantly,
``Authorization`` never arrives here and can never be read or forwarded by
this codebase. Two platform-injected headers survive the gateway:

- ``x-agent-user-id``: an opaque *global partition key* for the caller, used
  only for container-side state partitioning. It is not an authenticated
  identity assertion, must never be forwarded to Toolbox or any other
  downstream service, and must never be used to authorize document access.
- ``x-agent-foundry-call-id``: an opaque *per-request* call identifier. Per
  the Foundry hosted-agent contract this is the ONLY thing echoed on
  outbound Foundry/Toolbox-bound HTTP calls. Toolbox resolves the signed-in
  user from this call id server-side -- where the real OAuth context lives
  -- and applies native, per-user document trimming.

ADR-0001 (Azure/agent-landing-zone) freezes this passthrough as the required hosted
document-security path: a group-filter or manual ``metadata_security_id``
fallback is not a hosted default. When the call id is absent or malformed,
hosted retrieval must fail closed instead of silently falling back to
service identity or a manual filter.

Never log the call id value, nor whether it was present, above debug level.
"""

from __future__ import annotations

import re
from typing import Mapping, Optional

# Platform-injected header carrying the opaque, per-request call id. This is
# the ONLY header echoed outbound on Foundry/Toolbox-bound HTTP calls.
FOUNDRY_CALL_ID_HEADER = "x-agent-foundry-call-id"

# Platform-injected header carrying an opaque per-user partition key.
# Container-side state partitioning only -- never forwarded downstream and
# never treated as an identity or authorization assertion.
FOUNDRY_USER_ID_HEADER = "x-agent-user-id"

_MAX_CALL_ID_LENGTH = 256
# Printable, visible ASCII only. This blocks header injection (no CR/LF or
# other whitespace) when the value is echoed on an outbound request, while
# still accepting any opaque platform-issued identifier (GUID, base64url
# token, etc.).
_VALID_CALL_ID = re.compile(r"^[\x21-\x7e]+$")

# Canonical, safe-to-expose message reused at every point that fails closed
# on a missing/malformed Foundry call context: the pre-``StreamingResponse``
# HTTP 401 guard in ``api.hosted_entrypoint.invocations`` and the
# defense-in-depth check inside ``api.hosted_entrypoint._hosted_stream``
# (which guards direct/internal callers that might bypass that HTTP
# precheck). Keeping one shared string means every fail-closed path reports
# identical, reviewable text -- never the call id value, never strategy
# internals, never auth header content.
MISSING_CALL_CONTEXT_MESSAGE = (
    "Missing or malformed platform call context "
    f"('{FOUNDRY_CALL_ID_HEADER}'). Hosted retrieval requires the "
    "Foundry-injected call id to resolve per-user document security "
    "through Toolbox; refusing to fall back to service identity or "
    "a manual metadata filter."
)


class MissingFoundryCallContextError(ValueError):
    """The platform-injected Foundry call context is absent or malformed.

    Hosted retrieval must fail closed on this error rather than falling back
    to service identity or a manual ``metadata_security_id`` filter.
    """


def extract_foundry_call_id(headers: Mapping[str, str]) -> Optional[str]:
    """Return the raw, unvalidated ``x-agent-foundry-call-id`` header value."""
    value = headers.get(FOUNDRY_CALL_ID_HEADER)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def require_foundry_call_id(headers: Mapping[str, str]) -> str:
    """Return the validated opaque Foundry call id, or fail closed.

    Raises :class:`MissingFoundryCallContextError` when the header is
    missing, empty, surrounded by whitespace, too long, or contains characters
    that would allow outbound header injection when echoed to Toolbox.
    """
    raw_call_id = headers.get(FOUNDRY_CALL_ID_HEADER)
    call_id = extract_foundry_call_id(headers)
    if (
        not call_id
        or raw_call_id != call_id
        or len(call_id) > _MAX_CALL_ID_LENGTH
        # ``fullmatch`` (not ``match``) is required here: with ``^...$`` and
        # no ``re.MULTILINE``, Python's ``$`` matches just before a single
        # trailing "\n", so ``.match()`` would wrongly accept a value like
        # "call-id\n". ``extract_foundry_call_id`` already strips such
        # values, but validation must not rely on that as its only defense
        # -- ``fullmatch`` anchors to the true end of the string so this
        # check is safe even if called directly with an unstripped value.
        or not _VALID_CALL_ID.fullmatch(call_id)
    ):
        raise MissingFoundryCallContextError(MISSING_CALL_CONTEXT_MESSAGE)
    return call_id
