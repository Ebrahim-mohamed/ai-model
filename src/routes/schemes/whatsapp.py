from uuid import UUID
from typing import Optional

from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: UUID
    message: str


class ChatResponse(BaseModel):
    client_id: str
    session_id: UUID
    reply: str
    intent: str
    mode: Optional[str] = None
