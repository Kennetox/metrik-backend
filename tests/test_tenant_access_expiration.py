from datetime import date, timedelta

import crud
import models


def test_expired_access_date_blocks_tenant_and_session_exposes_remaining_days():
    tenant = models.Tenant(
        id=3,
        slug="kensar",
        name="Kensar",
        is_active=True,
        lifecycle_stage="active",
        access_expires_on=date.today() - timedelta(days=1),
    )

    assert crud.get_tenant_access_issue(tenant) == "El acceso de esta empresa venció."
    session = crud.build_tenant_session_read(tenant)
    assert session is not None
    assert session.access_expires_on == tenant.access_expires_on
    assert session.access_days_remaining == 0


def test_access_date_includes_the_deadline_day():
    tenant = models.Tenant(
        id=3,
        slug="kensar",
        name="Kensar",
        is_active=True,
        lifecycle_stage="active",
        access_expires_on=date.today(),
    )

    assert crud.get_tenant_access_issue(tenant) is None
    assert crud.get_tenant_access_days_remaining(tenant) == 0
