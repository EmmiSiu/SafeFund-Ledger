from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

class GroupCreate(BaseModel):
    name: str = Field(..., max_length=100)
    caja_id: int

class GroupRead(GroupCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime
