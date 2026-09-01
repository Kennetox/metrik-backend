"""Reusable notification producer helpers for dashboard modules such as Kora."""

from datetime import datetime
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy.orm import Session

import crud, models
from services import permissions, tenant_modules


ALLOWED_SEVERITIES = {"info", "success", "warning", "critical"}


@dataclass(frozen=True)
class NotificationDistributionResult:
    recipient_ids: tuple[int, ...]
    created_notification_ids: tuple[int, ...]
    duplicate_notification_ids: tuple[int, ...]

    @property
    def recipient_count(self) -> int:
        return len(self.recipient_ids)

    @property
    def created_count(self) -> int:
        return len(self.created_notification_ids)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicate_notification_ids)


def user_can_receive_notification(
    *,
    tenant: models.Tenant | None,
    user: models.PosUser,
    permission_matrix,
    module_id: str | None = None,
    required_permission: str | None = None,
) -> bool:
    """Checks the same effective module and role access used by the dashboard.

    A child action can remain enabled in a customized permission matrix even
    when its parent module is disabled for the role. Notifications must honor
    the parent module switch as well as the optional action permission.
    """

    if module_id:
        if not crud.can_user_access_tenant_module(tenant, module_id, user=user):
            return False
        if not permissions.role_has_permission(
            permission_matrix,
            module_id,
            user.role,
        ):
            return False
    if required_permission and not permissions.role_has_permission(
        permission_matrix,
        required_permission,
        user.role,
    ):
        return False
    return True


def _validate_notification_content(
    *,
    title: str,
    message: str,
    severity: str,
    action_href: str | None,
) -> tuple[str, str]:
    clean_title = title.strip()
    clean_message = message.strip()
    if not clean_title or not clean_message:
        raise ValueError("La notificación requiere título y mensaje")
    if severity not in ALLOWED_SEVERITIES:
        raise ValueError("Nivel de notificación no válido")
    if action_href and not action_href.startswith("/dashboard"):
        raise ValueError("La acción debe apuntar a una ruta interna del dashboard")
    return clean_title, clean_message


def resolve_notification_recipients(
    db: Session,
    *,
    tenant_id: int,
    module_id: str | None = None,
    required_permission: str | None = None,
    roles: Sequence[str] | None = None,
    user_ids: Sequence[int] | None = None,
    exclude_user_ids: Sequence[int] | None = None,
) -> list[models.PosUser]:
    """Returns active users who can currently receive a module notification.

    All filters are cumulative. ``user_ids`` narrows the audience; it never
    bypasses role, permission, tenant-module or per-user module access rules.
    """

    tenant = crud.get_tenant(db, tenant_id)
    if not tenant:
        raise ValueError("La empresa indicada no existe")
    if module_id:
        if module_id not in tenant_modules.MODULE_IDS:
            raise ValueError("Módulo de notificación no válido")
        if not tenant_modules.is_module_enabled(tenant.enabled_modules, module_id):
            return []

    allowed_roles: set[str] | None = None
    if roles is not None:
        allowed_roles = {role.strip() for role in roles if role and role.strip()}
        invalid_roles = allowed_roles.difference(permissions.ROLE_KEYS)
        if invalid_roles:
            raise ValueError(f"Rol de destinatario no válido: {sorted(invalid_roles)[0]}")
        if not allowed_roles:
            return []

    query = db.query(models.PosUser).filter(
        models.PosUser.tenant_id == tenant_id,
        models.PosUser.is_active.is_(True),
        models.PosUser.status == "Activo",
    )
    if allowed_roles is not None:
        query = query.filter(models.PosUser.role.in_(allowed_roles))
    if user_ids is not None:
        selected_ids = {int(user_id) for user_id in user_ids if int(user_id) > 0}
        if not selected_ids:
            return []
        query = query.filter(models.PosUser.id.in_(selected_ids))
    excluded_ids = {
        int(user_id) for user_id in (exclude_user_ids or []) if int(user_id) > 0
    }
    if excluded_ids:
        query = query.filter(~models.PosUser.id.in_(excluded_ids))

    permission_matrix = None
    if module_id or required_permission:
        permission_matrix = crud.get_role_permissions(db, tenant_id=tenant_id)
    if required_permission:
        known_permission_ids = {
            item_id
            for module in permission_matrix
            for item_id in [
                module.get("id"),
                *(action.get("id") for action in module.get("actions", [])),
            ]
            if item_id
        }
        if required_permission not in known_permission_ids:
            raise ValueError("Permiso de notificación no válido")
    recipients: list[models.PosUser] = []
    for user in query.order_by(models.PosUser.id.asc()).all():
        if not user_can_receive_notification(
            tenant=tenant,
            user=user,
            permission_matrix=permission_matrix,
            module_id=module_id,
            required_permission=required_permission,
        ):
            continue
        recipients.append(user)
    return recipients


def create_user_notification(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    title: str,
    message: str,
    source: str = "system",
    category: str = "general",
    severity: str = "info",
    module_id: str | None = None,
    required_permission: str | None = None,
    action_label: str | None = None,
    action_href: str | None = None,
    dedupe_key: str | None = None,
    payload: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    commit: bool = True,
) -> tuple[models.UserNotification, bool]:
    """Creates one inbox item, returning ``(notification, created)``.

    ``dedupe_key`` makes recurring producers idempotent per tenant and recipient;
    Kora can use a value such as ``web-opportunities:2026-W30``.
    """

    clean_title, clean_message = _validate_notification_content(
        title=title,
        message=message,
        severity=severity,
        action_href=action_href,
    )

    recipient = (
        db.query(models.PosUser)
        .filter(
            models.PosUser.id == user_id,
            models.PosUser.tenant_id == tenant_id,
        )
        .first()
    )
    if not recipient:
        raise ValueError("El destinatario no pertenece a la empresa indicada")

    clean_dedupe_key = dedupe_key.strip() if dedupe_key else None
    if clean_dedupe_key:
        existing = (
            db.query(models.UserNotification)
            .filter(
                models.UserNotification.tenant_id == tenant_id,
                models.UserNotification.user_id == user_id,
                models.UserNotification.dedupe_key == clean_dedupe_key,
            )
            .first()
        )
        if existing:
            return existing, False

    notification = models.UserNotification(
        tenant_id=tenant_id,
        user_id=user_id,
        source=source.strip() or "system",
        category=category.strip() or "general",
        severity=severity,
        module_id=module_id.strip() if module_id else None,
        required_permission=(required_permission.strip() if required_permission else None),
        title=clean_title,
        message=clean_message,
        action_label=action_label.strip() if action_label else None,
        action_href=action_href,
        dedupe_key=clean_dedupe_key,
        payload=payload,
        expires_at=expires_at,
    )
    db.add(notification)
    if commit:
        db.commit()
        db.refresh(notification)
    else:
        db.flush()
    return notification, True


def distribute_notification(
    db: Session,
    *,
    tenant_id: int,
    title: str,
    message: str,
    source: str = "system",
    category: str = "general",
    severity: str = "info",
    module_id: str | None = None,
    required_permission: str | None = None,
    roles: Sequence[str] | None = None,
    user_ids: Sequence[int] | None = None,
    exclude_user_ids: Sequence[int] | None = None,
    action_label: str | None = None,
    action_href: str | None = None,
    dedupe_key: str | None = None,
    payload: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> NotificationDistributionResult:
    """Creates one personal inbox item for every eligible recipient."""

    _validate_notification_content(
        title=title,
        message=message,
        severity=severity,
        action_href=action_href,
    )
    recipients = resolve_notification_recipients(
        db,
        tenant_id=tenant_id,
        module_id=module_id,
        required_permission=required_permission,
        roles=roles,
        user_ids=user_ids,
        exclude_user_ids=exclude_user_ids,
    )
    created_ids: list[int] = []
    duplicate_ids: list[int] = []
    try:
        for recipient in recipients:
            notification, created = create_user_notification(
                db,
                tenant_id=tenant_id,
                user_id=recipient.id,
                title=title,
                message=message,
                source=source,
                category=category,
                severity=severity,
                module_id=module_id,
                required_permission=required_permission,
                action_label=action_label,
                action_href=action_href,
                dedupe_key=dedupe_key,
                payload=payload,
                expires_at=expires_at,
                commit=False,
            )
            target = created_ids if created else duplicate_ids
            target.append(notification.id)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return NotificationDistributionResult(
        recipient_ids=tuple(user.id for user in recipients),
        created_notification_ids=tuple(created_ids),
        duplicate_notification_ids=tuple(duplicate_ids),
    )
