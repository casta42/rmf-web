from typing import List, Optional

from fastapi import Depends, HTTPException, Query
from reactivex import operators as rxops

from api_server.authenticator import user_dep
from api_server.dependencies import pagination_query
from api_server.fast_io import FastIORouter, SubscriptionRequest
from api_server.models import Pagination, User
from api_server.models import tortoise_models as ttm
from api_server.repositories import AlertRepository, alert_repo_dep
from api_server.rmf_io import alert_events

router = FastIORouter(tags=["Alerts"])


@router.sub("", response_model=ttm.AlertPydantic)
async def sub_alerts(_req: SubscriptionRequest):
    return alert_events.alerts.pipe(rxops.filter(lambda x: x is not None))


# ----------------------------------------------------------------------
# F-270: THE ENUM IS THE VALIDATOR.
#
# `category` and `severity` were plain `str` here and handed straight to
# a tortoise CharEnumField, which raises on a value outside the enum —
# so an unknown filter came back 500, and so did an unknown value on the
# CREATE path, where the alert is simply LOST. Measured 2026-09-08, all
# four: GET ?category=nonsense, GET ?severity=nonsense, POST with either.
#
# The trigger was not a typo. `Alert.Category.Instrument` was added for
# the F-268 referee alerts and the sentinel posted to it for an hour
# against a database column still sized for the older, shorter names —
# every one of those alerts answered 500 and discarded, while the
# sentinel logged that it was shouting.
#
# Typing the parameters AS the enums makes the API refuse a bad value at
# the door, with 422 and the permitted set named, and makes the accepted
# values a fact derived from the model rather than a list somebody has to
# remember to update — which is exactly the maintenance the last two
# defects were missing. `status` is not enum-backed and keeps its
# hand-written check below.
# ----------------------------------------------------------------------
@router.get("", response_model=List[ttm.AlertPydantic])
async def get_alerts(
    repo: AlertRepository = Depends(alert_repo_dep),
    status: Optional[str] = Query(
        None, description="'open' (unresolved), 'resolved' (archive), or omit for all"
    ),
    category: Optional[ttm.Alert.Category] = Query(
        None, description="filter by alert category; omit for all"
    ),
    severity: Optional[ttm.Alert.Severity] = Query(
        None, description="filter by severity; omit for all"
    ),
    fleet: Optional[str] = Query(None),
    robot: Optional[str] = Query(None),
    pagination: Pagination = Depends(pagination_query),
):
    """FR-31: filterable, paginated alert list (newest first by default)."""
    if status is not None and status not in ("open", "resolved", "all"):
        raise HTTPException(422, "status must be 'open', 'resolved' or 'all'")
    return await repo.query_alerts(
        status=None if status == "all" else status,
        category=category,
        severity=severity,
        fleet=fleet,
        robot=robot,
        pagination=pagination,
    )


@router.get("/{alert_id}", response_model=ttm.AlertPydantic)
async def get_alert(alert_id: str, repo: AlertRepository = Depends(alert_repo_dep)):
    alert = await repo.get_alert(alert_id)
    if alert is None:
        raise HTTPException(404, f"Alert with ID {alert_id} not found")
    return alert


@router.post("", status_code=201, response_model=ttm.AlertPydantic)
async def create_alert(
    alert_id: str,
    category: ttm.Alert.Category,
    severity: ttm.Alert.Severity = ttm.Alert.Severity.Warning,
    fleet: Optional[str] = None,
    robot: Optional[str] = None,
    message: Optional[str] = None,
    repo: AlertRepository = Depends(alert_repo_dep),
):
    """FR-31/FR-17: external creators (e.g. the co-location sentinel)
    pass the structured context the repository already stores — severity,
    fleet, robot and a human sentence — so the alert center never has to
    fall back to rendering a machine ID. The create path also pushes the
    alert event so a critical alert toasts live (F-93), exactly like the
    acknowledge/resolve paths already do."""
    alert = await repo.create_alert(
        alert_id, category, severity=severity, fleet=fleet, robot=robot,
        message=message
    )
    if alert is None:
        raise HTTPException(404, f"Could not create alert with ID {alert_id}")
    alert_events.alerts.on_next(alert)
    return alert


@router.post("/{alert_id}", status_code=201, response_model=ttm.AlertPydantic)
async def acknowledge_alert(
    alert_id: str, repo: AlertRepository = Depends(alert_repo_dep)
):
    alert = await repo.acknowledge_alert(alert_id)
    if alert is None:
        raise HTTPException(404, f"Could acknowledge alert with ID {alert_id}")
    alert_events.alerts.on_next(alert)
    return alert


@router.post("/{alert_id}/resolve", response_model=ttm.AlertPydantic)
async def resolve_alert(
    alert_id: str,
    user: User = Depends(user_dep),
    repo: AlertRepository = Depends(alert_repo_dep),
):
    """FR-31: operator resolution — archives the alert (never deletes)."""
    alert = await repo.resolve_alert(alert_id, resolved_by=user.username)
    if alert is None:
        raise HTTPException(404, f"No open alert with ID {alert_id}")
    alert_events.alerts.on_next(alert)
    return alert
