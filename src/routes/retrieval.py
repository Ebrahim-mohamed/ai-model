from fastapi import APIRouter, Header, HTTPException, Request, status

from .schemes.retrieval import RetrieveRequest, RetrieveResponse, RetrievedChunk
from controllers.RetrievalController import MetadataFilterValidationError, UnknownClientError

retrieval_router = APIRouter(
    prefix="/api",
    tags=["retrieval"],
)


@retrieval_router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(body: RetrieveRequest, request: Request, x_admin_api_key: str = Header(...)):
    """client_id is resolved server-side from the admin API key header —
    the same mechanism routes/sync.py uses (Section 2 has no broader
    session/auth system in scope; see README's Step 9 section for why
    this reuses that mechanism rather than inventing a second one).
    Never accepted as a body/path/query field, so a tenant can only ever
    retrieve their own content."""
    client_id = await request.app.client_config_model.get_client_id_by_admin_api_key(x_admin_api_key)
    if client_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin API key")

    try:
        results = await request.app.retrieval_controller.retrieve(
            client_id=client_id, query=body.query, metadata_filters=body.metadata_filters,
        )
    except MetadataFilterValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    except UnknownClientError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    return RetrieveResponse(
        client_id=client_id,
        query=body.query,
        results=[
            RetrievedChunk(
                id=result["chunk"].id,
                content=result["chunk"].content,
                chunk_type=result["chunk"].chunk_type,
                metadata=result["chunk"].metadata_payload,
                source_file=result["chunk"].source_file,
                score=result["score"],
            )
            for result in results
        ],
    )
