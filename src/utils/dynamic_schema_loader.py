from models.SchemaRegistryModel import SchemaRegistryModel


async def discover_and_register_schema(
    schema_registry_model: SchemaRegistryModel,
    client_id: str,
    sheet_name: str,
    discovered_columns: list[str],
):
    """The Step 5 Sheet Dispatcher's call site for auto-discovery.

    Stateless and schema-agnostic: it never opens a file itself and never
    branches on sheet identity or client_id. Callers pass whatever columns
    they actually read off a sheet's header row (`df.columns`) — this
    function only forwards that list to the registry's upsert-on-discovery
    logic. Every synced sheet is Bucket A now (architecture override, see
    claude.md) — there is no bucket to classify.
    """
    return await schema_registry_model.get_or_register(
        client_id=client_id,
        sheet_name=sheet_name,
        discovered_columns=discovered_columns,
    )
