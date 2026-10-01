from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..deps import current_user, get_db
from ..models import User
from ..services import payshap
from .insights import _clients

router = APIRouter(prefix="/api/payshap", tags=["payshap"])


@router.get("")
def overview(days: int = 90, client_id: Optional[int] = None, user: User = Depends(current_user),
             db: Session = Depends(get_db)):
    clients = _clients(db, user, client_id)
    end = date.today()
    payshap.match(db, user.practice_id)
    return {"days": days, **payshap.summary(db, [c.id for c in clients], end - timedelta(days=max(1, min(days, 730))), end)}
