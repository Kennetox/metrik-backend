from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

import crud, models, schemas
from database import get_db
from dependencies import get_current_active_user
from services.user_notifications import user_can_receive_notification


router = APIRouter(prefix="/notifications", tags=["notifications"])


def _tenant_id(db: Session, user: models.PosUser) -> int:
    tenant_id = crud.resolve_user_tenant_id(db, user)
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario sin empresa asignada",
        )
    return tenant_id


def _is_visible_to_user(
    notification: models.UserNotification,
    *,
    user: models.PosUser,
    tenant: models.Tenant | None,
    permission_matrix,
) -> bool:
    return user_can_receive_notification(
        tenant=tenant,
        user=user,
        permission_matrix=permission_matrix,
        module_id=notification.module_id,
        required_permission=notification.required_permission,
    )


@router.get("", response_model=schemas.UserNotificationListRead)
def list_my_notifications(
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    tenant_id = _tenant_id(db, current_user)
    now = datetime.utcnow()
    rows = (
        db.query(models.UserNotification)
        .filter(
            models.UserNotification.tenant_id == tenant_id,
            models.UserNotification.user_id == current_user.id,
            models.UserNotification.dismissed_at.is_(None),
            or_(
                models.UserNotification.expires_at.is_(None),
                models.UserNotification.expires_at > now,
            ),
        )
        .order_by(models.UserNotification.created_at.desc(), models.UserNotification.id.desc())
        .all()
    )
    tenant = crud.get_tenant(db, tenant_id)
    permission_matrix = crud.get_role_permissions(db, tenant_id=tenant_id)
    visible = [
        row
        for row in rows
        if _is_visible_to_user(
            row,
            user=current_user,
            tenant=tenant,
            permission_matrix=permission_matrix,
        )
    ]
    return {
        "items": visible[:limit],
        "unread_count": sum(1 for row in visible if row.read_at is None),
    }


def _get_owned_notification(
    db: Session,
    notification_id: int,
    current_user: models.PosUser,
) -> models.UserNotification:
    tenant_id = _tenant_id(db, current_user)
    notification = (
        db.query(models.UserNotification)
        .filter(
            models.UserNotification.id == notification_id,
            models.UserNotification.tenant_id == tenant_id,
            models.UserNotification.user_id == current_user.id,
            models.UserNotification.dismissed_at.is_(None),
        )
        .first()
    )
    if not notification:
        raise HTTPException(status_code=404, detail="Notificación no encontrada")
    return notification


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
def mark_all_as_read(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    tenant_id = _tenant_id(db, current_user)
    now = datetime.utcnow()
    (
        db.query(models.UserNotification)
        .filter(
            models.UserNotification.tenant_id == tenant_id,
            models.UserNotification.user_id == current_user.id,
            models.UserNotification.dismissed_at.is_(None),
            models.UserNotification.read_at.is_(None),
        )
        .update({models.UserNotification.read_at: now}, synchronize_session=False)
    )
    db.commit()


@router.post("/dismiss-all", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_all_notifications(
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    tenant_id = _tenant_id(db, current_user)
    now = datetime.utcnow()
    inbox_query = db.query(models.UserNotification).filter(
        models.UserNotification.tenant_id == tenant_id,
        models.UserNotification.user_id == current_user.id,
        models.UserNotification.dismissed_at.is_(None),
    )
    inbox_query.filter(models.UserNotification.read_at.is_(None)).update(
        {models.UserNotification.read_at: now},
        synchronize_session=False,
    )
    inbox_query.update(
        {models.UserNotification.dismissed_at: now},
        synchronize_session=False,
    )
    db.commit()


@router.patch("/{notification_id}/read", response_model=schemas.UserNotificationRead)
def mark_as_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    notification = _get_owned_notification(db, notification_id, current_user)
    if notification.read_at is None:
        notification.read_at = datetime.utcnow()
        db.commit()
        db.refresh(notification)
    return notification


@router.patch("/{notification_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(get_current_active_user),
):
    notification = _get_owned_notification(db, notification_id, current_user)
    notification.dismissed_at = datetime.utcnow()
    if notification.read_at is None:
        notification.read_at = notification.dismissed_at
    db.commit()
