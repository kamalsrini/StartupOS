"""Apply db/schema.sql to DATABASE_URL (or the DSN given as argv[1])."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from common.db import apply_schema  # noqa: E402

if __name__ == "__main__":
    import os

    dsn = sys.argv[1] if len(sys.argv) > 1 else None
    apply_schema(dsn)
    pw = os.environ.get("STARTUPOS_APP_PASSWORD")
    if pw:  # set the RLS role's password from the environment (rls.sql creates it with a placeholder)
        from psycopg import sql

        from common.db import admin_conn

        with admin_conn(dsn) as conn:  # utility statements take no bind params; quote as a literal
            conn.execute(sql.SQL("ALTER ROLE startupos_app WITH PASSWORD {}").format(sql.Literal(pw)))
    print("schema applied")
