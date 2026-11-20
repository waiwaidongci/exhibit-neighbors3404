from pydantic import BaseModel,Field
from typing import Optional


class CounterfactualInput(BaseModel):
    run_id: Optional[int] = None
    visitor: str = Field(min_length=1, max_length=120)
    scope_mode: str = Field(default='source')
    scope_refs: Optional[list[str]] = None
    budget: int = 200000


class ResumeInput(BaseModel):
    budget: Optional[int] = None
