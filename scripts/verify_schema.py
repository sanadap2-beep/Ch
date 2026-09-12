#!/usr/bin/env python3
"""Verify the live database schema matches what the app expects.

Used by CI (after `alembic upgrade head`) and useful after any deploy:

    python scripts/verify_schema.py            # connect + list tables + assert count
    python scripts/verify_schema.py --ping     # connectivity only
    python scripts/verify_schema.py --expect 21

Reads DATABASE_URL from the environment, falling back to app.config.settings.
Exits non-zero with a readable diagnosis rather than a traceback.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EXPECTED_TABLE_COUNT = 21  # keep in sync with migrations/versions/*_initial_schema.py


def _database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    from app.config import settings

    return settings.database_url


async def _inspect(url: str, ping_only: bool, expected: int) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    dialect = engine.dialect.name
    # Both the version probe and the table listing are dialect-specific SQL.
    version_sql = "select version()" if dialect == "postgresql" else "select sqlite_version()"
    tables_sql = (
        "select table_name from information_schema.tables "
        "where table_schema = 'public' order by table_name"
        if dialect == "postgresql"
        else "select name from sqlite_master where type='table' "
             "and name not like 'sqlite_%' order by name"
    )
    try:
        async with engine.connect() as conn:
            version = (await conn.execute(text(version_sql))).scalar()
            print(f"  dialect : {dialect}")
            print(f"  server  : {str(version).splitlines()[0][:80]}")
            if ping_only:
                print("  reachable ✔")
                return 0
            tables = (await conn.execute(text(tables_sql))).scalars().all()
    finally:
        await engine.dispose()
    print(f"  tables  : {len(tables)}")
    for name in tables:
        print(f"    {name}")

    if len(tables) < expected:
        missing_hint = ""
        if "alembic_version" not in tables:
            missing_hint = " — alembic_version is absent, so no migration has run"
        print(f"\n✖ expected at least {expected} tables, found {len(tables)}{missing_hint}")
        print("  → run: alembic upgrade head")
        return 1

    print("\n✔ schema looks complete")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ping", action="store_true", help="only check connectivity")
    parser.add_argument("--expect", type=int, default=EXPECTED_TABLE_COUNT,
                        help=f"minimum table count (default {EXPECTED_TABLE_COUNT})")
    args = parser.parse_args()

    url = _database_url()
    print(f"→ verifying schema at {url.split('@')[-1]}")
    try:
        return asyncio.run(_inspect(url, args.ping, args.expect))
    except Exception as exc:  # noqa: BLE001 — report, don't traceback
        print(f"\n✖ {type(exc).__name__}: {exc}")
        print("  → check DATABASE_URL, and that the server is up and migrated")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
