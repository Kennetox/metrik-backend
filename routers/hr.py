from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import models
import schemas
from database import get_db
from dependencies import require_permission


router = APIRouter(
    prefix="/hr",
    tags=["hr"],
)


@router.get("/employees", response_model=List[schemas.PosUserRead])
def list_hr_employees(
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("hr.view")),
):
    users = (
        db.query(models.PosUser)
        .order_by(models.PosUser.created_at.desc())
        .all()
    )
    return users
