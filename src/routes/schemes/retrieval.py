from typing import Optional
from uuid import UUID

from pydantic import BaseModel


class RetrieveRequest(BaseModel):
    query: str
    metadata_filters: Optional[dict] = None


class RetrievedChunk(BaseModel):
    id: UUID
    content: str
    chunk_type: Optional[str] = None
    metadata: dict
    source_file: Optional[str] = None
    score: float


class RetrieveResponse(BaseModel):
    client_id: str
    query: str
    results: list[RetrievedChunk]
