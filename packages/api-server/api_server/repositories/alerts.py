from datetime import datetime
from typing import List, Optional

from fastapi import Depends

from api_server.authenticator import user_dep
from api_server.logger import logger
from api_server.models import Pagination, User
from api_server.models import tortoise_models as ttm
from api_server.repositories.tasks import TaskRepository, task_repo_dep


def _now_millis() -> int:
    return round(datetime.now().timestamp() * 1e3)


class AlertRepository:
    def __init__(self, user: User, task_repo: Optional[TaskRepository]):
        self.user = user
        self.task_repo = (
            task_repo if task_repo is not None else TaskRepository(self.user)
        )

    async def get_all_alerts(self) -> List[ttm.AlertPydantic]:
        alerts = await ttm.Alert.all()
        return [await ttm.AlertPydantic.from_tortoise_orm(a) for a in alerts]

    async def query_alerts(
        self,
        status: Optional[str] = None,
        category: Optional[str] = None,
        severity: Optional[str] = None,
        fleet: Optional[str] = None,
        robot: Optional[str] = None,
        pagination: Optional[Pagination] = None,
    ) -> List[ttm.AlertPydantic]:
        """FR-31: filterable alert query. `status` is 'open' (unresolved),
        'resolved' (archive), or None/'all'."""
        q = ttm.Alert.all()
        if status == "open":
            q = q.filter(unix_millis_resolved_time__isnull=True)
        elif status == "resolved":
            q = q.filter(unix_millis_resolved_time__isnull=False)
        if category is not None:
            q = q.filter(category=category)
        if severity is not None:
            q = q.filter(severity=severity)
        if fleet is not None:
            q = q.filter(fleet=fleet)
        if robot is not None:
            q = q.filter(robot=robot)
        if pagination is not None:
            q = q.limit(pagination.limit).offset(pagination.offset)
            if pagination.order_by:
                q = q.order_by(*pagination.order_by)
            else:
                q = q.order_by("-unix_millis_created_time")
        else:
            q = q.order_by("-unix_millis_created_time")
        return [await ttm.AlertPydantic.from_tortoise_orm(a) for a in await q]

    async def alert_exists(self, alert_id: str) -> bool:
        result = await ttm.Alert.exists(id=alert_id)
        return result

    async def resolved_millis(self, alert_id: str) -> Optional[int]:
        """F-139: unix-millis the alert was resolved at, for the episode
        re-fire check. None while the row is still open; a MISSING row
        returns 0 (unverifiable → long-resolved, so the episode may
        re-raise instead of staying silenced forever)."""
        alert = await ttm.Alert.get_or_none(id=alert_id)
        if alert is None:
            return 0
        return alert.unix_millis_resolved_time

    async def get_alert(self, alert_id: str) -> Optional[ttm.AlertPydantic]:
        alert = await ttm.Alert.get_or_none(id=alert_id)
        if alert is None:
            logger.error(f"Alert with ID {alert_id} not found")
            return None
        alert_pydantic = await ttm.AlertPydantic.from_tortoise_orm(alert)
        return alert_pydantic

    async def create_alert(
        self,
        alert_id: str,
        category: str,
        severity: str = ttm.Alert.Severity.Warning,
        fleet: Optional[str] = None,
        robot: Optional[str] = None,
        message: Optional[str] = None,
    ) -> Optional[ttm.AlertPydantic]:
        alert, _ = await ttm.Alert.update_or_create(
            {
                "original_id": alert_id,
                "category": category,
                "severity": severity,
                "fleet": fleet,
                "robot": robot,
                "message": message,
                "unix_millis_created_time": _now_millis(),
                "acknowledged_by": None,
                "unix_millis_acknowledged_time": None,
                "resolved_by": None,
                "unix_millis_resolved_time": None,
            },
            id=alert_id,
        )
        if alert is None:
            logger.error(f"Failed to create Alert with ID {alert_id}")
            return None
        alert_pydantic = await ttm.AlertPydantic.from_tortoise_orm(alert)
        return alert_pydantic

    async def resolve_alert(
        self, alert_id: str, resolved_by: str = "system"
    ) -> Optional[ttm.AlertPydantic]:
        """F-29/FR-31: mark an open alert resolved (archived, not deleted).

        Alerts are current exceptions (F-22) — a stuck robot that moves
        again must not leave a standing page — but the episode is kept as
        history for incident review (FR-31). Returns the archived alert
        when an open one was resolved, else None."""
        alert = await ttm.Alert.get_or_none(id=alert_id)
        if alert is None or alert.unix_millis_resolved_time is not None:
            return None
        alert.resolved_by = resolved_by
        alert.unix_millis_resolved_time = _now_millis()
        await alert.save()
        logger.info(f"Resolved alert {alert_id} (condition cleared)")
        return await ttm.AlertPydantic.from_tortoise_orm(alert)

    async def resolve_alerts_by_prefix(
        self, original_id_prefix: str, resolved_by: str = "system-sweep"
    ) -> List[ttm.AlertPydantic]:
        """F-29/F-39/FR-31: resolve (archive) every OPEN alert whose
        original_id starts with the prefix.

        Used to sweep stale episode alerts on api-server restart: episode
        tracking is in-memory, so an open robot_stuck/robot_offline/
        low_battery alert from a previous server life is unverifiable
        history, not a current exception. v2 keeps the rows (archive)
        instead of deleting them."""
        open_alerts = await ttm.Alert.filter(
            original_id__startswith=original_id_prefix,
            unix_millis_resolved_time__isnull=True,
        )
        resolved: List[ttm.AlertPydantic] = []
        now = _now_millis()
        for alert in open_alerts:
            alert.resolved_by = resolved_by
            alert.unix_millis_resolved_time = now
            await alert.save()
            resolved.append(await ttm.AlertPydantic.from_tortoise_orm(alert))
        if resolved:
            logger.info(
                f"Resolved {len(resolved)} stale alert(s) matching "
                f"{original_id_prefix}* (server restart sweep)"
            )
        return resolved

    async def acknowledge_alert(self, alert_id: str) -> Optional[ttm.AlertPydantic]:
        """v2: acknowledge IN PLACE (no clone row, no delete). Falls back to
        original_id lookup so pre-v2 ack-clone ids keep resolving."""
        alert = await ttm.Alert.get_or_none(id=alert_id)
        if alert is None:
            acknowledged_alert = await ttm.Alert.filter(original_id=alert_id).first()
            if acknowledged_alert is None:
                logger.error(f"No existing or past alert with ID {alert_id} found.")
                return None
            acknowledged_alert_pydantic = await ttm.AlertPydantic.from_tortoise_orm(
                acknowledged_alert
            )
            return acknowledged_alert_pydantic

        unix_millis_acknowledged_time = _now_millis()
        alert.acknowledged_by = self.user.username
        alert.unix_millis_acknowledged_time = unix_millis_acknowledged_time
        await alert.save()

        # Task alerts also record the acknowledging user in the task log.
        # v2: only for task-category alerts (robot/fleet alert ids are not
        # task ids), and a missing ledger row must not fail the ack itself.
        if alert.category == ttm.Alert.Category.Task:
            try:
                await self.task_repo.save_log_acknowledged_task_completion(
                    alert.id, self.user.username, unix_millis_acknowledged_time
                )
            except Exception as e:  # pylint: disable=broad-except
                logger.warning(
                    f"Acknowledged alert {alert.id} but could not record it in "
                    f"the task log: {e}"
                )

        ack_alert_pydantic = await ttm.AlertPydantic.from_tortoise_orm(alert)
        return ack_alert_pydantic


def alert_repo_dep(
    user: User = Depends(user_dep), task_repo: TaskRepository = Depends(task_repo_dep)
):
    return AlertRepository(user, task_repo)
