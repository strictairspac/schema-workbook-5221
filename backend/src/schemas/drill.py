from typing import Any
from pydantic import BaseModel,Field
class DrillInput(BaseModel):
    name:str=Field(min_length=2,max_length=120)
    report_id:int=Field(gt=0)
class DrillRowInput(BaseModel):
    values:dict[str,Any]=Field(default_factory=dict)
    cascade:bool=False
