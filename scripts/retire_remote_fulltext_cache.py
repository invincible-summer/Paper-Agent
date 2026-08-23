#!/usr/bin/env python3
"""Preview/execute retirement of legacy remote-paper full-text caches."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.remote_fulltext_retirement import retire_remote_fulltext


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--api-root", action="append", default=[], type=Path,
                        help="additional isolated /v1 storage root; may be repeated")
    parser.add_argument("--execute", action="store_true", help="perform deletion; default is dry-run")
    parser.add_argument("--force", action="store_true", help="rerun even if the version marker exists")
    args = parser.parse_args()
    result = retire_remote_fulltext(
        args.project_root, api_roots=args.api_root,
        dry_run=not args.execute, force=args.force,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
