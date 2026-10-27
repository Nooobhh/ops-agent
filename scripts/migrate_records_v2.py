#!/usr/bin/env python
"""Migrate legacy records sessions to the current lib.record schema."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.datastore import open_datastore
from lib.record import migrate_legacy_records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-root", default=str(open_datastore().troubleshoot.root))
    args = parser.parse_args(argv)
    migrated = migrate_legacy_records(Path(args.records_root))
    print(f"records schema v2 迁移完成: {len(migrated)} 个 session")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
