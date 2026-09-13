from fastapi import (
    APIRouter,
    Depends,
)
from sqlalchemy.orm import Session

from ..database import get_database_session
from ..schemas import BrokenPromiseSweepResponse
from ..services.deadline_worker import (
    current_business_date,
    mark_broken_promises,
)


router = APIRouter(tags=["Internal Jobs"])


@router.post(
    "/jobs/mark-broken-promises",
    response_model=BrokenPromiseSweepResponse,
)
def run_broken_promise_sweep(
    session: Session = Depends(get_database_session),
) -> BrokenPromiseSweepResponse:
    as_of = current_business_date()

    try:
        broken_promise_ids = mark_broken_promises(
            session,
            as_of=as_of,
        )

        session.commit()

    except Exception:
        session.rollback()
        raise

    return BrokenPromiseSweepResponse(
        as_of=as_of,
        broken_count=len(broken_promise_ids),
        broken_promise_ids=broken_promise_ids,
    )