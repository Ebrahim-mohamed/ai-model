import io

import pandas as pd

from .BaseController import BaseController


class MissingMandatoryFieldError(Exception):
    """Raised when a sheet's configured mandatory fields (schema_registry)
    aren't all present as columns in the actual sheet. The whole sheet is
    rejected outright — never partially parsed (claude.md §3.1)."""


class DocumentParsingController(BaseController):
    """One generic function for every sheet, every client — driven entirely
    by the schema_registry row it's handed or auto-registers on the spot.
    Never branches on sheet identity (claude.md §1.3). Structural shape
    (merged cells, multi-row headers, transposed layouts) is trusted, never
    inferred — a genuinely malformed sheet just produces garbage columns
    from pandas, which is the correct failure mode per claude.md §3.1/§2.3:
    that class of guesswork is permanently out of scope, not a bug to fix
    here.

    Every sheet synced from OneDrive is Bucket A (architecture override —
    the data entry team never uploads Bucket B/C content; those are
    static, hardcoded templates entirely decoupled from this pipeline,
    see stores/llm/templates/static/). There is no bucket to route on
    anymore."""

    def __init__(self, schema_registry_model):
        super().__init__()
        self.schema_registry_model = schema_registry_model

    async def parse_workbook(self, client_id: str, workbook_bytes: bytes):
        """Yields (sheet_name: str, row_data: dict) for every non-empty
        row of every sheet in the workbook. Auto-registers any sheet not
        yet in schema_registry in the same call that reads it (claude.md
        §3.2)."""
        sheets = pd.read_excel(io.BytesIO(workbook_bytes), sheet_name=None, dtype=str)

        for sheet_name, df in sheets.items():
            discovered_columns = [str(column) for column in df.columns]

            schema_row = await self.schema_registry_model.get_or_register(
                client_id=client_id,
                sheet_name=sheet_name,
                discovered_columns=discovered_columns,
            )

            missing_fields = [
                field for field in schema_row.mandatory_fields
                if field not in discovered_columns
            ]
            if missing_fields:
                raise MissingMandatoryFieldError(
                    f"client_id={client_id!r} sheet_name={sheet_name!r} is missing mandatory "
                    f"field(s) {missing_fields} — rejecting the whole sheet, not partially parsing it."
                )

            for _, row in df.iterrows():
                row_data = {
                    column: (None if pd.isna(row[column]) else row[column])
                    for column in discovered_columns
                }

                # Skip fully-empty rows (e.g. trailing blank rows Excel keeps
                # around) — a purely mechanical check, not a per-sheet rule.
                if all(value is None for value in row_data.values()):
                    continue

                # claude.md §3.6 — every row stamped with the sheet it came
                # from, dynamically, from the same wb.sheetnames this loop
                # is already iterating over.
                row_data["sheet_name"] = sheet_name

                yield sheet_name, row_data
