from fastapi.testclient import TestClient

import crud, models
from services.user_notifications import (
    create_user_notification,
    distribute_notification,
    resolve_notification_recipients,
)
from conftest import TestingSessionLocal


def _auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/auth/login",
        json={"email": "master@kensar.com", "password": "2301"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _create_test_user(
    db,
    *,
    tenant_id: int,
    email: str,
    role: str,
    status: str = "Activo",
    is_active: bool = True,
) -> models.PosUser:
    user = models.PosUser(
        tenant_id=tenant_id,
        name=f"Prueba {role}",
        email=email,
        role=role,
        status=status,
        is_active=is_active,
        password_hash="not-used-in-notification-tests",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_personal_notification_lifecycle_and_deduplication(client: TestClient):
    headers = _auth_headers(client)
    with TestingSessionLocal() as db:
        user = crud.get_pos_user_by_email(db, "master@kensar.com")
        assert user is not None and user.tenant_id is not None
        first, created = create_user_notification(
            db,
            tenant_id=user.tenant_id,
            user_id=user.id,
            source="kora",
            category="web_opportunity",
            title="Oportunidad para Comercio Web",
            message="Hay productos con buen desempeño que aún no están publicados.",
            module_id="dashboard",
            action_label="Revisar productos",
            action_href="/dashboard/comercio-web/catalog",
            dedupe_key="test:web-opportunity:week-1",
        )
        duplicate, duplicate_created = create_user_notification(
            db,
            tenant_id=user.tenant_id,
            user_id=user.id,
            title="No debe duplicarse",
            message="Mismo ciclo semanal.",
            dedupe_key="test:web-opportunity:week-1",
        )
        notification_id = first.id
        assert created is True
        assert duplicate_created is False
        assert duplicate.id == notification_id

    inbox = client.get("/notifications", headers=headers)
    assert inbox.status_code == 200
    payload = inbox.json()
    assert payload["unread_count"] >= 1
    assert any(item["id"] == notification_id for item in payload["items"])

    marked = client.patch(f"/notifications/{notification_id}/read", headers=headers)
    assert marked.status_code == 200
    assert marked.json()["read_at"] is not None

    dismissed = client.patch(
        f"/notifications/{notification_id}/dismiss",
        headers=headers,
    )
    assert dismissed.status_code == 204

    refreshed = client.get("/notifications", headers=headers)
    assert refreshed.status_code == 200
    assert all(item["id"] != notification_id for item in refreshed.json()["items"])


def test_notification_action_rejects_external_urls(client: TestClient):
    _auth_headers(client)
    with TestingSessionLocal() as db:
        user = crud.get_pos_user_by_email(db, "master@kensar.com")
        assert user is not None and user.tenant_id is not None
        try:
            create_user_notification(
                db,
                tenant_id=user.tenant_id,
                user_id=user.id,
                title="Acción insegura",
                message="No debe aceptar destinos externos.",
                action_href="https://example.com",
            )
        except ValueError as exc:
            assert "ruta interna" in str(exc)
        else:
            raise AssertionError("Se esperaba rechazo para una URL externa")


def test_recipient_resolution_enforces_permission_status_and_module_assignment(
    client: TestClient,
):
    _auth_headers(client)
    with TestingSessionLocal() as db:
        admin = crud.get_pos_user_by_email(db, "master@kensar.com")
        assert admin is not None and admin.tenant_id is not None
        tenant = crud.get_tenant(db, admin.tenant_id)
        assert tenant is not None
        original_access = tenant.module_user_access
        supervisor = _create_test_user(
            db,
            tenant_id=admin.tenant_id,
            email="notifications-supervisor@test.local",
            role="Supervisor",
        )
        seller = _create_test_user(
            db,
            tenant_id=admin.tenant_id,
            email="notifications-seller@test.local",
            role="Vendedor",
        )
        inactive = _create_test_user(
            db,
            tenant_id=admin.tenant_id,
            email="notifications-inactive@test.local",
            role="Supervisor",
            status="Inactivo",
            is_active=False,
        )
        candidate_ids = [admin.id, supervisor.id, seller.id, inactive.id]
        try:
            permitted = resolve_notification_recipients(
                db,
                tenant_id=admin.tenant_id,
                module_id="dashboard",
                required_permission="dashboard.history",
                user_ids=candidate_ids,
            )
            assert {user.id for user in permitted} == {admin.id, supervisor.id}

            tenant.module_user_access = {
                **(original_access or {}),
                "dashboard": [admin.id, seller.id],
            }
            db.commit()
            assigned = resolve_notification_recipients(
                db,
                tenant_id=admin.tenant_id,
                module_id="dashboard",
                required_permission="dashboard.view",
                user_ids=candidate_ids,
            )
            assert {user.id for user in assigned} == {admin.id, seller.id}
        finally:
            tenant.module_user_access = original_access
            db.commit()
            for user in (supervisor, seller, inactive):
                db.delete(user)
            db.commit()


def test_distribution_creates_one_notification_per_eligible_user_and_deduplicates(
    client: TestClient,
):
    _auth_headers(client)
    with TestingSessionLocal() as db:
        admin = crud.get_pos_user_by_email(db, "master@kensar.com")
        assert admin is not None and admin.tenant_id is not None
        tenant = crud.get_tenant(db, admin.tenant_id)
        assert tenant is not None
        original_modules = tenant.enabled_modules
        original_access = tenant.module_user_access
        web_manager = _create_test_user(
            db,
            tenant_id=admin.tenant_id,
            email="notifications-distribution@test.local",
            role="Gestor Web",
        )
        candidate_ids = [admin.id, web_manager.id]
        dedupe_key = "test:distribution:week-1"
        try:
            tenant.enabled_modules = list(
                dict.fromkeys([*crud.get_tenant_enabled_modules(tenant), "commerce_web"])
            )
            tenant.module_user_access = {
                **(original_access or {}),
                "commerce_web": candidate_ids,
            }
            db.commit()
            first = distribute_notification(
                db,
                tenant_id=admin.tenant_id,
                source="kora",
                category="web_opportunity",
                module_id="commerce_web",
                required_permission="commerce_web.manage",
                user_ids=candidate_ids,
                title="Resumen semanal",
                message="Hay oportunidades que requieren revisión.",
                action_label="Revisar",
                action_href="/dashboard",
                dedupe_key=dedupe_key,
            )
            second = distribute_notification(
                db,
                tenant_id=admin.tenant_id,
                source="kora",
                category="web_opportunity",
                module_id="commerce_web",
                required_permission="commerce_web.manage",
                user_ids=candidate_ids,
                title="Resumen semanal repetido",
                message="No debe crear avisos nuevos.",
                dedupe_key=dedupe_key,
            )

            assert set(first.recipient_ids) == set(candidate_ids)
            assert first.created_count == 2
            assert first.duplicate_count == 0
            assert second.created_count == 0
            assert second.duplicate_count == 2
            stored = (
                db.query(models.UserNotification)
                .filter(models.UserNotification.dedupe_key == dedupe_key)
                .all()
            )
            assert {row.user_id for row in stored} == set(candidate_ids)
        finally:
            db.query(models.UserNotification).filter(
                models.UserNotification.dedupe_key == dedupe_key
            ).delete(synchronize_session=False)
            tenant.enabled_modules = original_modules
            tenant.module_user_access = original_access
            db.delete(web_manager)
            db.commit()
