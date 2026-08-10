#!/usr/bin/env python3
"""Preview or execute migration of historical Feishu insight reports."""

from __future__ import annotations

import argparse
import asyncio
import json

from dotenv import load_dotenv

load_dotenv()

from publishers.feishu_archive import FeishuArchiveError, FeishuArchiveManager
from publishers.feishu_publisher import FeishuPublisher


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform moves and ownership transfers; default is read-only preview",
    )
    return parser.parse_args()


async def run(execute: bool) -> int:
    publisher = FeishuPublisher()
    if not publisher.is_configured():
        print("Feishu credentials are missing.")
        return 1

    archive = FeishuArchiveManager(publisher)
    try:
        folders = await archive.resolve_report_folders()
        candidates = await archive.migration_candidates()
    except FeishuArchiveError as exc:
        print(f"Archive discovery failed: {exc}")
        return 1

    print(
        json.dumps(
            {
                "mode": "execute" if execute else "dry-run",
                "root_folder_token": archive.root_folder_token,
                "report_folders": folders,
                "candidate_count": len(candidates),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    failures = 0
    summary: dict[str, int] = {}
    for candidate in candidates:
        planned = []
        if candidate.current_folder_token != candidate.target_folder_token:
            planned.append("move")
        if candidate.owner_id != archive.admin_open_id:
            planned.append("transfer_owner")
        if not planned:
            planned.append("already_complete")

        print(
            json.dumps(
                {
                    "name": candidate.name,
                    "token": candidate.token,
                    "type": candidate.resource_type,
                    "report_kind": candidate.report_kind,
                    "planned": planned,
                },
                ensure_ascii=False,
            )
        )
        if not execute:
            summary["previewed"] = summary.get("previewed", 0) + 1
            continue

        try:
            result = await archive.migrate_candidate(candidate)
            summary[result] = summary.get(result, 0) + 1
        except FeishuArchiveError as exc:
            failures += 1
            print(f"FAILED {candidate.token}: {exc}")

    print(json.dumps({"summary": summary, "failures": failures}, ensure_ascii=False))
    return 1 if failures else 0


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(run(execute=args.execute)))


if __name__ == "__main__":
    main()

