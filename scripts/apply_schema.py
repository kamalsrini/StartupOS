"""Apply db/schema.sql to DATABASE_URL (or the DSN given as argv[1])."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common.db import apply_schema  # noqa: E402

if __name__ == "__main__":
    apply_schema(sys.argv[1] if len(sys.argv) > 1 else None)
    print("schema applied")
