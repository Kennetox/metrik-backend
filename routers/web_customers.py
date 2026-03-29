from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from security import WEB_INACTIVITY_TIMEOUT_SECONDS, WEB_TOKEN_TTL_SECONDS, create_access_token, verify_access_token, verify_password


router = APIRouter(
    prefix="/web/customers",
    tags=["web-customers"],
)


def _serialize_web_customer(account: models.WebCustomerAccount) -> schemas.WebCustomerRead:
    customer = account.customer
    if not customer:
        raise HTTPException(status_code=500, detail="Cuenta web sin cliente asociado")
    return schemas.WebCustomerRead(
        id=account.id,
        pos_customer_id=customer.id,
        name=customer.name,
        email=account.email,
        phone=customer.phone,
        tax_id=customer.tax_id,
        address=customer.address,
        email_verified=bool(account.email_verified),
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def require_web_customer_auth(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> models.WebCustomerAccount:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token requerido",
        )

    token = authorization.split(" ", 1)[1]
    try:
        payload = verify_access_token(token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    if payload.get("kind") != "web-customer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
        )

    account_id = payload.get("sub")
    if not account_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
        )

    session = crud.get_web_customer_session_by_token(db, token)
    if not session or session.revoked_at:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión inválida o cerrada",
        )

    now = datetime.utcnow()
    if session.expires_at < now:
        session.revoked_at = now
        session.revoked_reason = "expired"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión expirada",
        )

    if (
        session.last_seen_at
        and now - session.last_seen_at > timedelta(seconds=WEB_INACTIVITY_TIMEOUT_SECONDS)
    ):
        session.revoked_at = now
        session.revoked_reason = "inactive"
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión expirada por inactividad",
        )

    account = session.account
    if not account or account.id != int(account_id) or not account.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Cuenta no autorizada",
        )

    if not account.customer or not account.customer.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cliente inactivo",
        )

    session.last_seen_at = now
    db.commit()
    return account


@router.post("/register", response_model=schemas.WebCustomerAuthResponse, status_code=201)
def register_web_customer(
    payload: schemas.WebCustomerRegisterRequest,
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    try:
        account = crud.create_web_customer_account(db, payload, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token = create_access_token(
        account.id,
        role="WebCustomer",
        ttl=WEB_TOKEN_TTL_SECONDS,
        subject_type="web-customer",
    )
    expires_at = datetime.utcnow() + timedelta(seconds=WEB_TOKEN_TTL_SECONDS)
    crud.create_web_customer_session(db, account.id, token, expires_at)
    account.last_login = datetime.utcnow()
    db.commit()
    db.refresh(account)
    return schemas.WebCustomerAuthResponse(
        token=token,
        customer=_serialize_web_customer(account),
        expires_at=expires_at,
    )


@router.post("/login", response_model=schemas.WebCustomerAuthResponse)
def login_web_customer(
    payload: schemas.WebCustomerLoginRequest,
    db: Session = Depends(get_db),
):
    tenant_id = crud.resolve_public_catalog_tenant_id(db)
    account = crud.get_web_customer_account_by_email(db, payload.email, tenant_id=tenant_id)
    if not account or not account.is_active or not verify_password(payload.password, account.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas",
        )

    if not account.customer or not account.customer.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cliente inactivo",
        )

    crud.revoke_web_customer_sessions(db, account.id, reason="replaced")
    token = create_access_token(
        account.id,
        role="WebCustomer",
        ttl=WEB_TOKEN_TTL_SECONDS,
        subject_type="web-customer",
    )
    expires_at = datetime.utcnow() + timedelta(seconds=WEB_TOKEN_TTL_SECONDS)
    crud.create_web_customer_session(db, account.id, token, expires_at)
    account.last_login = datetime.utcnow()
    db.commit()
    db.refresh(account)
    return schemas.WebCustomerAuthResponse(
        token=token,
        customer=_serialize_web_customer(account),
        expires_at=expires_at,
    )


@router.get("/me", response_model=schemas.WebCustomerRead)
def get_current_web_customer(
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
):
    return _serialize_web_customer(account)


@router.post("/logout", status_code=204)
def logout_web_customer(
    account: models.WebCustomerAccount = Depends(require_web_customer_auth),
    db: Session = Depends(get_db),
):
    crud.revoke_web_customer_sessions(db, account.id, reason="logout")
    return None
