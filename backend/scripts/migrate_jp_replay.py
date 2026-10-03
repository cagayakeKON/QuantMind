"""Validate/import backed-up legacy JP replay sessions into the shared tables.

Run inside the configured backend container. Dry-run is the default. This does
not remove the source sessions, touch ordinary simulation accounts or cache,
or execute any new trade. The backup must match every live source record.
"""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.services.simulation.jp.replay_migration import (
    bind_saved_model,
    prepare_replay_import,
    source_digest,
)
from backend.services.simulation.replay.legacy_migration import stage_replay_import
from backend.services.simulation.models.replay_import import ReplayImportReceipt
from backend.shared.database_manager_v2 import DatabaseConfig


async def migrate(backup_path, *, apply=False):
    backup = json.loads(Path(backup_path).read_text(encoding="utf-8"))["sessions"]
    expected = {record["session_id"]: record for record in backup}
    if not expected or len(expected) != len(backup):
        raise ValueError("Backup requires distinct legacy session IDs")
    engine = create_async_engine(DatabaseConfig().get_master_url())
    summary = []
    try:
        async with async_sessionmaker(engine)() as db, db.begin():
            if not apply:
                await db.execute(text("SET TRANSACTION READ ONLY"))
            query = (
                "SELECT to_jsonb(s) FROM jp_simulation_sessions s ORDER BY session_id"
            )
            if apply:
                query += " FOR UPDATE NOWAIT"
            source = [row[0] for row in (await db.execute(text(query))).all()]
            if {record["session_id"] for record in source} != set(expected):
                raise ValueError("Live legacy session inventory differs from backup")
            for record in source:
                if source_digest(record) != source_digest(
                    expected[record["session_id"]]
                ):
                    raise ValueError("Live legacy session changed after backup")
            # Validate the entire inventory before adding any target row.
            plans = [
                await asyncio.to_thread(prepare_replay_import, row) for row in source
            ]
            plans = [await bind_saved_model(plan) for plan in plans]
            if apply:
                connection = await db.connection()
                await connection.run_sync(
                    lambda sync: ReplayImportReceipt.__table__.create(
                        sync, checkfirst=True
                    )
                )
            for plan in plans:
                inserted = await stage_replay_import(db, plan) if apply else None
                summary.append(
                    {
                        "session_id": str(plan["session"]["session_id"]),
                        "orders": len(plan["orders"]),
                        "trades": len(plan["trades"]),
                        "snapshots": len(plan["snapshots"]),
                        "inserted": inserted,
                    }
                )
    finally:
        await engine.dispose()
    return {"apply": apply, "sessions": summary, "source_deleted": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", help="Existing full JSON session backup")
    parser.add_argument(
        "--apply", action="store_true", help="Commit validated replay rows"
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(migrate(args.backup, apply=args.apply))))


if __name__ == "__main__":
    main()
