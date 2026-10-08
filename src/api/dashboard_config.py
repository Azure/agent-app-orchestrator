"""Admin dashboard Configuration tab API.

Three endpoints, all protected by :func:`api.dashboard.require_admin`:

* ``GET  /api/dashboard/config``          — current values for the curated allowlist.
* ``PUT  /api/dashboard/config``          — validate + persist a partial update.
* ``POST /api/dashboard/config/refresh``  — rebuild the in-process AppConfig cache.
* ``POST /api/dashboard/config/apply``    — soft-restart: refresh cache and
                                            return a clear status string. The
                                            orchestrator container is **not**
                                            recycled here; the docstring on
                                            the handler explains why.

Routes live in their own module so :mod:`api.dashboard` stays focused on the
existing read-only surface and this file owns the validation + persistence
flow end-to-end.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, HTTPException

from api.config_settings import (
    SECTIONS,
    SettingSpec,
    find_spec,
    is_denied,
)
from api.dashboard import require_admin
from connectors.appconfig import AppConfigClient
from dependencies import get_config
from schemas import (
    DashboardConfigApplyResponse,
    DashboardConfigErrorResponse,
    DashboardConfigFieldError,
    DashboardConfigRefreshResponse,
    DashboardConfigResponse,
    DashboardConfigUpdateRequest,
    DashboardSettingField,
    DashboardSettingOption,
    DashboardSettingSection,
)


# The App Configuration label all dashboard writes target. Picked to match the
# orchestrator-specific selector loaded by :class:`AppConfigClient`, so values
# set here take precedence over the shared ``gpt-rag`` baseline.
WRITE_LABEL = "gpt-rag-orchestrator"


router = APIRouter(prefix="/api/dashboard", tags=["dashboard-config"])


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def _coerce_for_type(value: Any, spec: SettingSpec) -> Any:
    """Coerce a raw value read from App Configuration into the spec's type.

    App Configuration stores everything as strings; we normalise here so the
    response body has properly typed JSON values (booleans, numbers, strings).
    Unparseable values fall back to the spec's default so a misconfigured key
    never blows up the GET endpoint.
    """
    if value is None:
        return spec.default
    try:
        if spec.type == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"true", "1", "yes"}
        if spec.type == "int":
            return int(value)
        if spec.type == "float":
            return float(value)
        # enum or other string types
        return str(value)
    except (ValueError, TypeError):
        logging.warning(
            "[dashboard-config] could not coerce key=%s to type=%s; using default",
            spec.key, spec.type,
        )
        return spec.default


def _read_current_value(cfg: AppConfigClient, spec: SettingSpec) -> Any:
    """Read the current value for one spec, falling back to the spec's default."""
    raw = cfg.get_value(spec.key, default=None, allow_none=True)
    return _coerce_for_type(raw, spec)


def _build_response(cfg: AppConfigClient) -> DashboardConfigResponse:
    sections: List[DashboardSettingSection] = []
    for section in SECTIONS:
        fields: List[DashboardSettingField] = []
        for spec in section.settings:
            value = _read_current_value(cfg, spec)
            options = None
            if spec.options is not None:
                options = [
                    DashboardSettingOption(
                        value=opt.value, label=opt.label, description=opt.description
                    )
                    for opt in spec.options
                ]
            fields.append(
                DashboardSettingField(
                    key=spec.key,
                    type=spec.type,
                    value=value,
                    default=spec.default,
                    label=spec.label,
                    description=spec.description,
                    options=options,
                    min=spec.min,
                    max=spec.max,
                    step=spec.step,
                    unit=spec.unit,
                )
            )
        sections.append(
            DashboardSettingSection(
                id=section.id,
                label=section.label,
                description=section.description,
                settings=fields,
            )
        )
    return DashboardConfigResponse(label=WRITE_LABEL, sections=sections)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_update(
    key: str, value: Any
) -> Tuple[bool, Any, str]:
    """Validate one ``(key, value)`` update against the allowlist + spec.

    Returns ``(ok, coerced_value, error_message)``. ``coerced_value`` is the
    value to actually persist when ``ok`` is true. ``error_message`` is
    populated when ``ok`` is false.
    """
    if is_denied(key):
        return False, None, "Key is not allowed to be modified from the dashboard."

    spec = find_spec(key)
    if spec is None:
        return False, None, "Key is not exposed by the dashboard."

    # Coerce per type then validate range / enum membership.
    try:
        if spec.type == "bool":
            if isinstance(value, bool):
                coerced: Any = value
            elif isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"true", "1", "yes"}:
                    coerced = True
                elif lowered in {"false", "0", "no"}:
                    coerced = False
                else:
                    return False, None, "Value must be a boolean."
            else:
                return False, None, "Value must be a boolean."
        elif spec.type == "int":
            coerced = int(value)
            if isinstance(value, float) and not value.is_integer():
                return False, None, "Value must be a whole number."
        elif spec.type == "float":
            coerced = float(value)
        elif spec.type == "enum":
            coerced = str(value)
            allowed_values = {opt.value for opt in (spec.options or [])}
            if allowed_values and coerced not in allowed_values:
                return False, None, f"Value must be one of: {sorted(allowed_values)}."
        else:
            coerced = str(value)
    except (ValueError, TypeError):
        return False, None, f"Value could not be parsed as {spec.type}."

    if spec.type in ("int", "float"):
        if spec.min is not None and coerced < spec.min:
            return False, None, f"Value must be >= {spec.min}."
        if spec.max is not None and coerced > spec.max:
            return False, None, f"Value must be <= {spec.max}."

    return True, coerced, ""


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/config",
    response_model=DashboardConfigResponse,
    dependencies=[Depends(require_admin)],
)
async def get_config_view(
    cfg: AppConfigClient = Depends(get_config),
) -> DashboardConfigResponse:
    """Return the current values for every setting in the allowlist."""
    return _build_response(cfg)


@router.put(
    "/config",
    response_model=DashboardConfigResponse,
    responses={
        422: {"model": DashboardConfigErrorResponse},
        500: {"model": DashboardConfigErrorResponse},
    },
    dependencies=[Depends(require_admin)],
)
async def update_config(
    body: DashboardConfigUpdateRequest,
    cfg: AppConfigClient = Depends(get_config),
) -> DashboardConfigResponse:
    """Validate and persist a partial update of the allowlisted settings."""
    if not body.settings:
        return _build_response(cfg)

    validated: Dict[str, Any] = {}
    validation_errors: List[DashboardConfigFieldError] = []
    seen: set[str] = set()
    for item in body.settings:
        if item.key in seen:
            validation_errors.append(
                DashboardConfigFieldError(
                    key=item.key, error="Duplicate key in request body."
                )
            )
            continue
        seen.add(item.key)
        ok, coerced, error = _validate_update(item.key, item.value)
        if not ok:
            validation_errors.append(
                DashboardConfigFieldError(key=item.key, error=error)
            )
            continue
        validated[item.key] = coerced

    if validation_errors:
        raise HTTPException(
            status_code=422,
            detail=DashboardConfigErrorResponse(errors=validation_errors).model_dump(),
        )

    # Persist sequentially: per-key isolation is more useful than throughput
    # for a 12-setting allowlist and keeps the error surface obvious.
    write_errors: List[DashboardConfigFieldError] = []
    for key, value in validated.items():
        try:
            await asyncio.to_thread(cfg.set_value, key, value, WRITE_LABEL)
        except Exception:
            logging.error("[dashboard-config] write failed for %s", key)
            write_errors.append(
                DashboardConfigFieldError(key=key, error="Unable to persist setting")
            )

    if write_errors:
        raise HTTPException(
            status_code=500,
            detail=DashboardConfigErrorResponse(errors=write_errors).model_dump(),
        )

    # Refresh the in-process cache so the response reflects the new values.
    refreshed = get_config("refresh")
    return _build_response(refreshed)


@router.post(
    "/config/refresh",
    response_model=DashboardConfigRefreshResponse,
    dependencies=[Depends(require_admin)],
)
async def refresh_config() -> DashboardConfigRefreshResponse:
    """Rebuild the orchestrator's in-process App Configuration cache.

    Equivalent to what the orchestrator does on cold start: a fresh
    :class:`AppConfigClient` is constructed and replaces the cached singleton,
    so the next request reads the latest values for every key (not just
    dashboard-exposed ones).
    """
    get_config("refresh")
    return DashboardConfigRefreshResponse(
        status="refreshed",
        detail="In-process configuration cache rebuilt. New values take effect on the next request.",
    )


@router.post(
    "/config/apply",
    response_model=DashboardConfigApplyResponse,
    dependencies=[Depends(require_admin)],
)
async def apply_config() -> DashboardConfigApplyResponse:
    """Apply configuration changes by refreshing the cache.

    Every setting exposed by the Configuration tab is read on demand by the
    orchestrator (agent strategy resolution, retrieval flags, sampling
    parameters, retry counts). A cache refresh is therefore sufficient to
    pick up new values without recycling the container.

    A hard container restart would require ``azure-mgmt-appcontainers`` plus
    extra RBAC on the deployment, neither of which is in scope for the
    initial Configuration tab. Boot-only settings are documented as needing a
    manual ``az containerapp revision restart`` and remain a follow-up.
    """
    get_config("refresh")
    return DashboardConfigApplyResponse(
        status="applied",
        detail=(
            "Configuration cache refreshed. All dashboard-exposed settings "
            "are read on demand, so changes take effect on the next request. "
            "For settings outside the dashboard that are only read at startup, "
            "restart the orchestrator container."
        ),
    )
