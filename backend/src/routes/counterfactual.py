from fastapi import APIRouter, Depends
from ..config.database import db_session
from ..schemas.counterfactual import CounterfactualInput, ResumeInput
from ..services.auth import current_user, writer
from ..controllers.counterfactual import CounterfactualController as C
router = APIRouter()


@router.get('/runs/{id}/counterfactual')
def jobs(id: int, db=Depends(db_session), u=Depends(current_user)):
    return C.list(db, id)


@router.get('/runs/{id}/counterfactual/options')
def options(id: int, db=Depends(db_session), u=Depends(current_user)):
    return C.options(db, id)


@router.post('/runs/{id}/counterfactual')
def create(id: int, data: CounterfactualInput, db=Depends(db_session), u=Depends(writer)):
    data.run_id = id
    job, _ = C.create(db, u, data)
    return job


@router.get('/counterfactual/{id}')
def detail(id: int, db=Depends(db_session), u=Depends(current_user)):
    return C.detail(db, id)


@router.post('/counterfactual/{id}/resume')
def resume(id: int, data: ResumeInput, db=Depends(db_session), u=Depends(writer)):
    return C.resume(db, u, id, data.budget)


@router.post('/counterfactual/{id}/verify')
def verify(id: int, db=Depends(db_session), u=Depends(current_user)):
    return C.verify(db, id)
