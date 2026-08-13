from sqlalchemy import Column, String

from .raylab_base import SQLAlchemyBase


class DialogueStateTemplateMap(SQLAlchemyBase):
    """The dialogue-state -> Bucket B/C template_id mapping Mode B reads
    to decide whether — and with which verbatim template — to fire
    (Implementation Plan, Step 1's Architectural Logic). Stored as data,
    keyed by (client_id, dialogue_state), so a new scripted moment is a
    row insert, never a new `if` branch in TextReplyController
    (claude.md §1.3, §6.3)."""

    __tablename__ = "dialogue_state_template_map"

    client_id = Column(String, primary_key=True)      # Tenant isolation key — mandatory, universal
    dialogue_state = Column(String, primary_key=True)

    bucket = Column(String, nullable=False)      # "B" | "C" — see stores/llm/templates/template_parser.TemplateBucket
    template_id = Column(String, nullable=False)
