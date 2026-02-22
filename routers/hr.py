from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from dependencies import require_permission
from services import storage


router = APIRouter(
    prefix="/hr",
    tags=["hr"],
)


@router.get("/employees", response_model=List[schemas.HREmployeeRead])
def list_hr_employees(
    status: str | None = None,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.view")),
):
    return crud.list_hr_employees(db, status=status)


@router.post("/employees", response_model=schemas.HREmployeeRead, status_code=201)
def create_hr_employee(
    payload: schemas.HREmployeeCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.manage")),
):
    return crud.create_hr_employee(db, payload)


@router.get("/employees/{employee_id}", response_model=schemas.HREmployeeRead)
def get_hr_employee(
    employee_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.view")),
):
    employee = crud.get_hr_employee(db, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    return employee


@router.get("/system-users", response_model=List[schemas.HRSystemUserOption])
def list_system_users_for_hr(
    q: str | None = None,
    only_unlinked: bool = True,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.manage")),
):
    users = crud.list_pos_users(db)
    query = (q or "").strip().lower()
    rows: list[models.PosUser] = []
    for user in users:
        if only_unlinked and user.employee_id is not None:
            continue
        if query:
            name = (user.name or "").lower()
            email = (user.email or "").lower()
            if query not in name and query not in email:
                continue
        rows.append(user)
    return rows


@router.patch("/employees/{employee_id}", response_model=schemas.HREmployeeRead)
def update_hr_employee(
    employee_id: int,
    payload: schemas.HREmployeeUpdate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.manage")),
):
    employee = crud.get_hr_employee(db, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    return crud.update_hr_employee(db, employee, payload)


@router.post("/employees/{employee_id}/system-user", response_model=schemas.PosUserRead, status_code=201)
def create_system_user_for_employee(
    employee_id: int,
    payload: schemas.HREmployeeCreateSystemUserRequest,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.manage")),
):
    employee = crud.get_hr_employee(db, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    if employee.system_user:
        raise HTTPException(status_code=400, detail="El empleado ya tiene un usuario vinculado")

    try:
        user = crud.create_pos_user(
            db,
            schemas.PosUserCreate(
                name=employee.name,
                email=payload.email,
                role=payload.role,
                phone=employee.phone,
                position=employee.position,
                notes=employee.notes,
                password=payload.password,
                pin_plain=payload.pin_plain,
                employee_id=employee.id,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return user


@router.post("/employees/{employee_id}/system-user/link", response_model=schemas.PosUserRead)
def link_system_user_to_employee(
    employee_id: int,
    payload: schemas.HREmployeeLinkSystemUserRequest,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.manage")),
):
    employee = crud.get_hr_employee(db, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    if employee.system_user:
        raise HTTPException(status_code=400, detail="El empleado ya tiene un usuario vinculado")

    user = crud.get_pos_user(db, payload.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if user.employee_id:
        raise HTTPException(status_code=400, detail="Ese usuario ya está vinculado a otro empleado")

    user.employee_id = employee.id
    db.commit()
    db.refresh(user)
    return user


@router.post("/employees/{employee_id}/system-user/deactivate", response_model=schemas.PosUserRead)
def deactivate_system_user_for_employee(
    employee_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("hr.manage")),
):
    employee = crud.get_hr_employee(db, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    user = employee.system_user
    if not user:
        raise HTTPException(status_code=404, detail="El empleado no tiene usuario vinculado")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="No puedes desactivar tu propio usuario")
    try:
        return crud.update_pos_user(db, user, schemas.PosUserUpdate(status="Inactivo"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/employees/{employee_id}/system-user", status_code=204)
def delete_system_user_for_employee(
    employee_id: int,
    db: Session = Depends(get_db),
    current_user: models.PosUser = Depends(require_permission("hr.manage")),
):
    employee = crud.get_hr_employee(db, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    user = employee.system_user
    if not user:
        raise HTTPException(status_code=404, detail="El empleado no tiene usuario vinculado")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="No puedes borrar tu propio usuario")

    try:
        db.delete(user)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="No se puede borrar este usuario por trazabilidad. Se recomienda desactivarlo.",
        ) from exc
    return Response(status_code=204)


@router.post("/employees/{employee_id}/avatar", response_model=schemas.UploadAvatarResponse)
async def upload_hr_employee_avatar(
    employee_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.manage")),
):
    employee = crud.get_hr_employee(db, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    try:
        result = await storage.save_user_avatar(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        raise HTTPException(500, detail=f"No se pudo guardar la imagen: {exc}") from exc

    employee.avatar_url = result.url
    if employee.system_user:
        employee.system_user.avatar_url = result.url
    db.commit()
    return schemas.UploadAvatarResponse(url=result.url)


@router.delete("/employees/{employee_id}/avatar", response_model=schemas.HREmployeeRead)
def clear_hr_employee_avatar(
    employee_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.manage")),
):
    employee = crud.get_hr_employee(db, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    employee.avatar_url = None
    if employee.system_user:
        employee.system_user.avatar_url = None
    db.commit()
    db.refresh(employee)
    return employee


@router.get(
    "/employees/{employee_id}/documents",
    response_model=List[schemas.HREmployeeDocumentRead],
)
def list_hr_employee_documents(
    employee_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.view")),
):
    employee = crud.get_hr_employee(db, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    hr_docs = crud.list_hr_employee_documents(db, employee_id)
    merged: list[schemas.HREmployeeDocumentRead] = [
        schemas.HREmployeeDocumentRead(
            id=doc.id,
            employee_id=doc.employee_id,
            file_name=doc.file_name,
            file_url=doc.file_url,
            file_size=doc.file_size,
            note=doc.note,
            created_at=doc.created_at,
            source="hr",
            can_delete=True,
        )
        for doc in hr_docs
    ]

    if employee.system_user:
        profile_docs = crud.list_user_documents(db, employee.system_user.id)
        merged.extend(
            schemas.HREmployeeDocumentRead(
                id=doc.id,
                employee_id=employee_id,
                file_name=doc.file_name,
                file_url=doc.file_url,
                file_size=doc.file_size,
                note=doc.note,
                created_at=doc.created_at,
                source="profile",
                can_delete=True,
            )
            for doc in profile_docs
        )

    merged.sort(key=lambda row: row.created_at, reverse=True)
    return merged


@router.post(
    "/employees/{employee_id}/documents",
    response_model=schemas.HREmployeeDocumentRead,
    status_code=201,
)
async def upload_hr_employee_document(
    employee_id: int,
    file: UploadFile = File(...),
    note: str | None = Form(None),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.manage")),
):
    employee = crud.get_hr_employee(db, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    existing = crud.list_hr_employee_documents(db, employee_id)
    if len(existing) >= 10:
        raise HTTPException(status_code=400, detail="Se alcanzó el límite de 10 documentos.")

    try:
        result = await storage.save_user_document(file, employee_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - filesystem errors
        raise HTTPException(500, detail=f"No se pudo guardar el documento: {exc}") from exc

    return crud.create_hr_employee_document(
        db,
        employee_id=employee_id,
        file_name=result.filename,
        file_url=result.url,
        file_size=result.size,
        note=note.strip() if note else None,
    )


@router.delete("/employees/{employee_id}/documents/{doc_id}", status_code=204)
def delete_hr_employee_document(
    employee_id: int,
    doc_id: int,
    source: str = "hr",
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.manage")),
):
    employee = crud.get_hr_employee(db, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")
    if source == "profile":
        if not employee.system_user:
            raise HTTPException(status_code=400, detail="El empleado no tiene usuario vinculado")
        deleted = crud.delete_user_document(db, employee.system_user.id, doc_id)
    else:
        deleted = crud.delete_hr_employee_document(db, employee_id, doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    return Response(status_code=204)
