from sqlalchemy import Column,Integer,String,JSON,Text,ForeignKey
from ..config.database import Base


class CounterfactualSearch(Base):
    __tablename__='counterfactual_searches'
    id=Column(Integer,primary_key=True)
    run_id=Column(Integer,ForeignKey('recommendation_runs.id'),nullable=False)
    visitor=Column(String(120),nullable=False)
    scope_mode=Column(String(20),nullable=False)
    scope_refs=Column(JSON,nullable=False)
    parameters=Column(JSON,nullable=False)
    fingerprint=Column(String(64),nullable=False)
    status=Column(String(20),default='queued')
    budget=Column(Integer,nullable=False)
    state=Column(JSON,nullable=True)
    result=Column(JSON,nullable=True)
    error=Column(Text,default='')
    attempts=Column(Integer,default=0)
    created_by=Column(String(120),nullable=False)
    created_at=Column(String(40),nullable=False)
    updated_at=Column(String(40),nullable=False)
