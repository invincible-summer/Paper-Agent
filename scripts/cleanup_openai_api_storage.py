#!/usr/bin/env python3
"""Run API-only retention cleanup. Never scans outside OPENAI_API_STORAGE_ROOT."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.api_storage_cleanup import ApiStorageCleanup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--execute-token")
    parser.add_argument("--reconcile", action="store_true")
    args = parser.parse_args()
    cleanup = ApiStorageCleanup()
    if args.preview:
        output = cleanup.create_preview(action="immediate_cleanup", created_by="cli")
    elif args.execute_token:
        output = cleanup.execute_preview(args.execute_token).to_dict()
    else:
        output = cleanup.run(
            mode="scheduled" if args.scheduled else "manual",
            reconcile=args.reconcile,
        ).to_dict()
    # Contains aggregate counts only; never paths, user text, keys or filenames.
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
