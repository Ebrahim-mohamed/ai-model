#!/usr/bin/env python3
"""Admin CLI: onboard a new client end-to-end.

Replaces the manual two-step process this project used previously — a
psql UPDATE/INSERT typed by hand, plus a throwaway `python <<EOF ... EOF`
heredoc for the MSAL Device Code Flow. Both were error-prone in the same
way: shell double-quoting around a value containing `!` (a real Graph
composite item ID looks like `{driveId}!{suffix}`) is subject to bash
history expansion, which has previously corrupted a stored
`onedrive_item_id` into `{driveId}\\!{suffix}` — a literal backslash that
made every Graph API call 400. Argument values here arrive via argv, not
an interpolated shell string, so that failure mode cannot happen.

Usage:
    python scripts/onboard_client.py \\
        --client raylab \\
        --drive-id FC04A7AF2B9235EE \\
        --item-id "FC04A7AF2B9235EE!s5a9a50eefc4d481fbf61ea87425b6c0e" \\
        --api-key raylab-admin-test-key

Runs from anywhere — it locates src/ relative to its own file location.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# This script lives in scripts/, one level above src/, but every import
# below is an src/-relative module (helpers.config, models.*, stores.*).
# Make src/ importable, and make it the process cwd so Settings' relative
# ".env" resolves exactly the way it does for every other entrypoint in
# this project (main.py, celery_app.py) — regardless of where this script
# was actually invoked from.
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
os.chdir(SRC_DIR)

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from helpers.config import get_settings  # noqa: E402
from models.TokenCacheModel import TokenCacheModel  # noqa: E402
from models.ClientConfigModel import ClientConfigModel  # noqa: E402
from stores.onedrive.OneDriveProviderFactory import OneDriveProviderFactory  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("onboard_client")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Onboard a client: run the one-time MSAL Device Code Flow "
                    "login and seed client_config in a single, atomic admin action.",
    )
    parser.add_argument("--client", required=True, help="client_id, e.g. raylab")
    parser.add_argument(
        "--drive-id", required=True, dest="drive_id",
        help="OneDrive driveId the client's shared sync folder lives on",
    )
    parser.add_argument(
        "--item-id", required=True, dest="item_id",
        help="The shared sync folder's own Item ID (not a workbook's) — "
             "pass it exactly as given by Graph, including any '!' characters",
    )
    parser.add_argument(
        "--api-key", required=True, dest="api_key",
        help="Admin API key this client will authenticate POST /api/sync with",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    settings = get_settings()

    conn = (
        f"postgresql+asyncpg://{settings.POSTGRES_USERNAME}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_MAIN_DATABASE}"
    )
    engine = create_async_engine(conn, pool_pre_ping=True)
    db_client = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        token_cache_model = await TokenCacheModel.create_instance(db_client)
        client_config_model = await ClientConfigModel.create_instance(db_client)

        # --- Phase A: MSAL Device Code Flow ---------------------------------
        logger.info(f"[1/2] Starting MSAL Device Code Flow for client_id={args.client!r}...")

        factory = OneDriveProviderFactory(config=settings, token_cache_model=token_cache_model)
        provider = factory.create(provider=settings.ONEDRIVE_AUTH_BACKEND)
        if provider is None:
            logger.error(
                f"No OneDrive provider registered for "
                f"ONEDRIVE_AUTH_BACKEND={settings.ONEDRIVE_AUTH_BACKEND!r}."
            )
            return 1

        # Delegates to MSALGraphProvider's own device-flow implementation
        # rather than re-driving msal.PublicClientApplication here — that
        # method already owns the correct authority/scope constants for
        # this project (OneDrive Personal: the /consumers authority,
        # Files.Read — not Files.Read.All, which this app registration was
        # never granted) and already retries the token-cache DB write
        # against transient connection errors. stores/onedrive owns all
        # MSAL logic; this script only orchestrates it.
        await provider.register_client_via_device_flow(args.client)
        logger.info(f"[1/2] Token cache saved for client_id={args.client!r}.")

        # --- Phase B: client_config upsert -----------------------------------
        logger.info(f"[2/2] Writing client_config for client_id={args.client!r}...")

        # args.drive_id / args.item_id / args.api_key arrived via argv, not
        # an interpolated shell -c "..." string — there is no bash quoting
        # or history-expansion layer here that could mangle a literal '!'
        # into '\!' the way a hand-typed psql UPDATE did previously.
        await client_config_model.upsert_client_config(
            client_id=args.client,
            onedrive_drive_id=args.drive_id,
            onedrive_item_id=args.item_id,
            admin_api_key=args.api_key,
        )
        logger.info(
            f"[2/2] client_config upserted for client_id={args.client!r}: "
            f"onedrive_drive_id={args.drive_id!r}, onedrive_item_id={args.item_id!r}, "
            f"admin_api_key set."
        )

        print(f"\nDone — client_id={args.client!r} is fully onboarded. Trigger a sync with:")
        print(f'  curl -X POST http://localhost:8000/api/sync -H "X-Admin-Api-Key: {args.api_key}"')
        return 0

    except Exception as e:
        logger.error(f"Onboarding failed for client_id={args.client!r}: {e!r}")
        return 1

    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
