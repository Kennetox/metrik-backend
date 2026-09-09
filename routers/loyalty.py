from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import crud
import schemas
from database import get_db


router = APIRouter(
    prefix="/rewards",
    tags=["loyalty-rewards"],
)


@router.get("/public/{token}", response_model=schemas.PublicLoyaltyRewardResponse)
def get_public_loyalty_reward(
    token: str,
    db: Session = Depends(get_db),
):
    return crud.get_public_loyalty_reward(db, token)


@router.post(
    "/public/{token}/activate",
    response_model=schemas.PublicLoyaltyRewardActivateResponse,
)
def activate_public_loyalty_reward(
    token: str,
    db: Session = Depends(get_db),
):
    return crud.activate_public_loyalty_reward(db, token)
