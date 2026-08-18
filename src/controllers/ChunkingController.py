import os

from .BaseController import BaseController
from models.db_schemes.raylab.schemes import KnowledgeChunk


class ChunkingController(BaseController):
    """Converts staged Bucket-A rows into embeddable (not yet embedded —
    that's Step 8) chunks. Runs exactly two steps, in this order, for every
    sheet's rows (claude.md §3.8):

      1. Concatenate every non-empty field into "label: value", walking
         schema_registry's discovered column order — no exclusions, no
         statistical calculation of any kind.
      2. Only THEN prepend "[Document: {file_name}] " to the final string,
         as a separate, final step — never woven into the concatenation
         loop itself.

    Statistical boilerplate detection/exclusion was deliberately removed
    after measuring it against real client data — see claude.md §3.8 for
    the full reasoning. There is nothing per-client to configure here.

    Structured field preservation (Section 3 Step 1 generation-quality
    fix): the same filtered {column: value} dict that step 1 concatenates
    into a flat string is ALSO kept, structured, in
    metadata_payload["field_data"]. `content` is a derived VIEW of that
    dict (built from it, never a separate parallel computation), so the
    flattened text and the structured field data can never disagree about
    which fields exist for a given row. field_data is stored, not
    embedded/indexed — it exists purely so generation-time field
    selection (FieldSelectionController) can hand the LLM one already-
    isolated fact instead of asking it to blindly re-parse the flattened
    string it was itself flattened from.
    """

    def __init__(self, staging_row_model, schema_registry_model):
        super().__init__()
        self.staging_row_model = staging_row_model
        self.schema_registry_model = schema_registry_model

    async def chunk_source_file(self, client_id: str, source_file: str) -> list[KnowledgeChunk]:
        """Builds (but does not persist) every chunk for this client's
        already-staged rows from one source_file, across however many
        sheets that file contains."""
        rows = await self.staging_row_model.get_rows(client_id=client_id, source_file=source_file)

        rows_by_sheet: dict[str, list] = {}
        for row in rows:
            rows_by_sheet.setdefault(row.sheet_name, []).append(row)

        file_label = os.path.splitext(source_file)[0] if source_file else ""

        chunks: list[KnowledgeChunk] = []
        for sheet_name, sheet_rows in rows_by_sheet.items():
            schema_row = await self.schema_registry_model.get_schema(client_id, sheet_name)
            columns = schema_row.columns if schema_row is not None else self._infer_columns(sheet_rows)

            for row in sheet_rows:
                row_data = row.row_data

                # Single source of truth: which fields count (non-empty,
                # schema order) is decided exactly once, here. Both the
                # flattened content string and the structured field_data
                # metadata are built FROM this same dict, so they cannot
                # drift apart — there is no second, separately-maintained
                # filtering pass anywhere else in this method.
                fields = self._extract_non_empty_fields(row_data, columns)

                metadata = {
                    "sheet_name": sheet_name,
                    "source_file": source_file,
                    "field_data": fields,
                }

                # Step 1: concatenate every non-empty field, in schema order.
                concatenated = self._concatenate_fields(fields)

                # Step 2: ONLY here, after step 1 has fully produced the
                # concatenated string, does the document-context prefix get
                # added — a separate, final step.
                content = f"[Document: {file_label}] {concatenated}"

                chunks.append(
                    KnowledgeChunk(
                        client_id=client_id,
                        content=content,
                        source_file=source_file,
                        chunk_type=sheet_name,
                        metadata_payload=metadata,
                    )
                )

        return chunks

    def _extract_non_empty_fields(self, row_data: dict, columns: list) -> dict:
        """Walks schema_registry's discovered column order (never a
        hardcoded field list — `columns` is whatever this client's actual
        registered schema for this sheet contains) and keeps only the
        non-empty ones, in that order. This dict IS the row's structured
        shape — both the flattened `content` string and the structured
        `field_data` metadata are derived from it, never computed
        independently of each other."""
        fields = {}
        for column in columns:
            value = row_data.get(column)
            if value is None or str(value).strip() == "":
                continue
            fields[column] = value
        return fields

    def _concatenate_fields(self, fields: dict) -> str:
        return ". ".join(f"{column}: {value}" for column, value in fields.items())

    def _infer_columns(self, rows: list) -> list[str]:
        """Fallback only — schema_registry should always have a row by the
        time Step 6 runs (Step 5 auto-registers on first sight). Derives a
        stable column order from the rows themselves if it's ever missing."""
        columns: list[str] = []
        seen = set()
        for row in rows:
            for key in row.row_data.keys():
                if key != "sheet_name" and key not in seen:
                    seen.add(key)
                    columns.append(key)
        return columns
