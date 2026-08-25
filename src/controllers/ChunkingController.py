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
    embedded/indexed — not for generation-time field pre-selection
    anymore (FieldSelectionController was deleted 2026-08-25; the
    fine-tuned model now makes that judgment itself from the full
    `content`, see TextReplyController._narrow_context_block), but it
    remains real, live-consumed structure: scripts/finetune_data/
    sampling.py's entire training-data pipeline is built on it, and it's
    the ground truth ReplyVerificationController's grounding check
    validates the model's own JSON output against.

    Metadata promotion (brand filtering): matches by VALUE, not column
    name — client_config.brand_value_aliases maps real brand strings (in
    either language the source data uses) to one canonical lowercase
    value, e.g. {"Technoscan": "technoscan", "تكنوسكان": "technoscan"}.
    Real data showed brand info lives under 4 different column spellings
    (account/Account/Accounts/Acoount) across 5 sheets, in 2 different
    scripts — a label-keyed match breaks on the next sheet that spells the
    column yet another way; a value-keyed match doesn't care what the
    column is called or which sheet it's on. Every non-empty field's
    value is checked against the alias map; the first match promotes
    metadata["brand"] to the canonical value. A row with no field whose
    value is a known brand alias gets no "brand" key at all — never a
    null placeholder. Never a hardcoded column/sheet name here (claude.md
    §1.3) — a brand-new sheet, under any spelling, is picked up
    automatically; only the business vocabulary itself (a genuinely new
    brand) needs a one-time client_config edit.
    """

    def __init__(self, staging_row_model, schema_registry_model, client_config_model):
        super().__init__()
        self.staging_row_model = staging_row_model
        self.schema_registry_model = schema_registry_model
        self.client_config_model = client_config_model

    async def chunk_source_file(self, client_id: str, source_file: str) -> list[KnowledgeChunk]:
        """Builds (but does not persist) every chunk for this client's
        already-staged rows from one source_file, across however many
        sheets that file contains."""
        rows = await self.staging_row_model.get_rows(client_id=client_id, source_file=source_file)

        rows_by_sheet: dict[str, list] = {}
        for row in rows:
            rows_by_sheet.setdefault(row.sheet_name, []).append(row)

        file_label = os.path.splitext(source_file)[0] if source_file else ""

        client_config = await self.client_config_model.get_client_config(client_id)
        raw_aliases = (client_config.brand_value_aliases if client_config else None) or {}
        # Case-insensitive lookup built once per file, not per row: real
        # data showed the source sheets aren't even internally consistent
        # in casing (Examinations is "Cairoscan" almost everywhere, but one
        # real row has "cairoscan" already lowercase) — matching case-
        # insensitively covers any future casing variant automatically,
        # rather than needing a new literal alias entry per casing seen.
        brand_value_aliases = {alias.lower(): canonical for alias, canonical in raw_aliases.items()}

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
                    **self._promote_brand_metadata(fields, brand_value_aliases),
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

    def _promote_brand_metadata(self, fields: dict, brand_value_aliases: dict) -> dict:
        """Scans every non-empty field's VALUE (never its column name —
        claude.md §1.3) against `brand_value_aliases` (already lowercased-
        keyed by the caller). The first field whose stripped+lowercased
        value matches a known brand alias — in whichever language, casing,
        or spelling the source sheet happens to use — promotes
        metadata["brand"] to the map's canonical value. `fields` preserves
        schema column order (see _extract_non_empty_fields), so "first
        match" is deterministic, not incidental. A row with no matching
        value contributes nothing — never a null placeholder.
        Real, disclosed limitation: this can't distinguish "a column that
        legitimately means brand" from "some unrelated column whose value
        happens to equal a known brand string" — a real but low-probability
        risk given brand names are specific proper nouns, not a risk this
        method silently hides."""
        if not brand_value_aliases:
            return {}
        for value in fields.values():
            normalized = str(value).strip().lower()
            if normalized in brand_value_aliases:
                return {"brand": brand_value_aliases[normalized]}
        return {}

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
