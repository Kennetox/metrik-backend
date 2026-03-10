from uuid import uuid4

import crud
import models
import schemas
from tests.conftest import TestingSessionLocal


def _create_tenant(db, name: str) -> models.Tenant:
    tenant = models.Tenant(
        slug=f"tenant-{uuid4().hex[:10]}",
        name=name,
        is_active=True,
        lifecycle_stage="active",
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def test_update_tenant_syncs_company_name_when_placeholder():
    db = TestingSessionLocal()
    try:
        tenant = _create_tenant(db, "Mi Negocio")
        settings = models.PosSettings(tenant_id=tenant.id, company_name="Mi Negocio")
        db.add(settings)
        db.commit()

        payload = schemas.PlatformTenantUpdateRequest(name="Kensar Electronic")
        crud.update_tenant(db, tenant, payload)
        db.refresh(settings)

        assert tenant.name == "Kensar Electronic"
        assert settings.company_name == "Kensar Electronic"
    finally:
        db.close()


def test_update_tenant_keeps_custom_company_name():
    db = TestingSessionLocal()
    try:
        tenant = _create_tenant(db, "Mi Negocio")
        settings = models.PosSettings(tenant_id=tenant.id, company_name="Kensar Electronic")
        db.add(settings)
        db.commit()

        payload = schemas.PlatformTenantUpdateRequest(name="Metrik Holdings")
        crud.update_tenant(db, tenant, payload)
        db.refresh(settings)

        assert tenant.name == "Metrik Holdings"
        assert settings.company_name == "Kensar Electronic"
    finally:
        db.close()


def test_update_tenant_creates_settings_when_missing():
    db = TestingSessionLocal()
    try:
        tenant = _create_tenant(db, "Base Tenant")

        payload = schemas.PlatformTenantUpdateRequest(name="Tenant Renombrado")
        crud.update_tenant(db, tenant, payload)

        settings = (
            db.query(models.PosSettings)
            .filter(models.PosSettings.tenant_id == tenant.id)
            .first()
        )
        assert settings is not None
        assert settings.company_name == "Tenant Renombrado"
    finally:
        db.close()
