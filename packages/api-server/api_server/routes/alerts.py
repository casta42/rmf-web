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


@router.get("", response_model=List[ttm.AlertPydantic])
async def get_alerts(
    repo: AlertRepository = Depends(alert_repo_dep),
    status: Optional[str] = Query(
        None, description="'open' (unresolved), 'resolved' (archive), or omit for all"
    ),
    category: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
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
    alert_id: str, category: str, repo: AlertRepository = Depends(alert_repo_dep)
):
    alert = await repo.create_alert(alert_id, category)
    if alert is None:
        raise HTTPException(404, f"Could not create alert with ID {alert_id}")
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
