"""Apply db/schema.sql to DATABASE_URL (or --dsn)."""

import sys

from common.db import apply_schema

if __name__ == "__main__":
    dsn = sys.argv[1] if len(sys.argv) > 1 else None
    apply_schema(dsn)
    print("schema applied")
