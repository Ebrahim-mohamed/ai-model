from typing import Optional

from pydantic import BaseModel


class SyncTriggerResponse(BaseModel):
    task_id: str
    status: str


class SyncStatusResponse(BaseModel):
    task_id: str
    status: str
    result: Optional[str] = None
