import os
import io
import json
import decimal
import datetime
import subprocess
import tempfile
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateTable
from sqlalchemy import MetaData

from .config import get_settings
from .database import engine



router = APIRouter(tags=["db-export"])
settings = get_settings()


def _parse_mysql_url(url: str) -> dict:
    """
    Parses e.g. mysql+pymysql://root:password@localhost:3306/aliexpress_shopify
    into connection parts usable by mysqldump.
    """
    clean_url = url.replace("mysql+pymysql://", "mysql://").replace("mysql+mysqldb://", "mysql://")
    parsed = urlparse(clean_url)
    return {
        "user": parsed.username or "root",
        "password": parsed.password or "",
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 3306),
        "database": (parsed.path or "").lstrip("/"),
    }


def _cleanup_file(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _json_safe(value):
    """Convert non-JSON-serializable SQL types to plain values."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


@router.get("/admin/download-database")
def download_database(
    background_tasks: BackgroundTasks,
    key: str = Query(..., description="Secret key required to authorize the dump"),
):
    """
    Dumps the full MySQL database using mysqldump and returns it as a
    downloadable .sql file. Requires DB_DUMP_SECRET to match the `key` param.

    Usage: GET /api/admin/download-database?key=YOUR_SECRET
    """
    if not settings.DB_DUMP_SECRET:
        raise HTTPException(500, "DB_DUMP_SECRET is not configured on the server — refusing to export.")
    if key != settings.DB_DUMP_SECRET:
        raise HTTPException(403, "Invalid key")

    conn = _parse_mysql_url(settings.MYSQL_URL)
    if not conn["database"]:
        raise HTTPException(500, "Could not determine database name from MYSQL_URL")

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    tmp_dir = tempfile.gettempdir()
    dump_path = os.path.join(tmp_dir, f"{conn['database']}_backup_{timestamp}.sql")

    cmd = [
        "mysqldump",
        "-h", conn["host"],
        "-P", conn["port"],
        "-u", conn["user"],
        f"--password={conn['password']}",
        "--single-transaction",
        "--routines",
        "--triggers",
        "--result-file", dump_path,
        conn["database"],
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        raise HTTPException(
            500,
            "mysqldump is not installed on this server. "
            "Install MySQL client tools or use /admin/download-database-json instead."
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Database dump timed out after 5 minutes")

    if result.returncode != 0:
        raise HTTPException(502, f"mysqldump failed: {result.stderr.strip()}")

    if not os.path.exists(dump_path) or os.path.getsize(dump_path) == 0:
        raise HTTPException(502, "Dump file was not created or is empty")

    background_tasks.add_task(_cleanup_file, dump_path)

    return FileResponse(
        path=dump_path,
        media_type="application/sql",
        filename=f"{conn['database']}_backup_{timestamp}.sql",
        background=background_tasks,
    )


@router.get("/admin/download-database-json")
def download_database_json(key: str = Query(...)):
    """
    Fallback export that doesn't require mysqldump — dumps every table's
    rows to a single JSON file using SQLAlchemy's own connection.
    Not a restorable SQL dump, but a full data snapshot.

    Usage: GET /api/admin/download-database-json?key=YOUR_SECRET
    """
    if not settings.DB_DUMP_SECRET:
        raise HTTPException(500, "DB_DUMP_SECRET is not configured on the server.")
    if key != settings.DB_DUMP_SECRET:
        raise HTTPException(403, "Invalid key")

    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    export = {}
    with engine.connect() as conn:
        for table in table_names:
            result = conn.execute(text(f"SELECT * FROM `{table}`"))
            columns = result.keys()
            rows = []
            for row in result:
                row_dict = {col: _json_safe(val) for col, val in zip(columns, row)}
                rows.append(row_dict)
            export[table] = rows

    payload = json.dumps(export, indent=2, default=str)
    buffer = io.BytesIO(payload.encode("utf-8"))

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"database_backup_{timestamp}.json"

    return StreamingResponse(
        buffer,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


 


def _sql_literal(value) -> str:
    """Convert a Python value into a safe SQL literal for an INSERT statement."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, decimal.Decimal)):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return f"'{value.isoformat(sep=' ')}'"
    if isinstance(value, (dict, list)):
        # JSON columns — dump as JSON string, escaped
        text_val = json.dumps(value)
        escaped = text_val.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    # Strings and everything else — escape quotes and backslashes
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


@router.get("/admin/download-database-sql")
def download_database_sql(key: str = Query(...)):
    """
    Generates a restorable .sql dump (CREATE TABLE + INSERT statements)
    using pure Python/SQLAlchemy — no mysqldump binary required.

    Usage: GET /api/admin/download-database-sql?key=YOUR_SECRET
    """
    if not settings.DB_DUMP_SECRET:
        raise HTTPException(500, "DB_DUMP_SECRET is not configured on the server.")
    if key != settings.DB_DUMP_SECRET:
        raise HTTPException(403, "Invalid key")

    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    sql_parts = [
        "-- Database export generated via FastAPI (pure Python, no mysqldump)",
        f"-- Generated at: {datetime.datetime.utcnow().isoformat()} UTC",
        "SET FOREIGN_KEY_CHECKS=0;",
        "",
    ]

    metadata_obj = MetaData()
    metadata_obj.reflect(bind=engine)

    with engine.connect() as conn:
        for table_name in table_names:
            table = metadata_obj.tables.get(table_name)
            if table is None:
                continue

            # ── Schema ──
            sql_parts.append(f"-- Table structure for `{table_name}`")
            sql_parts.append(f"DROP TABLE IF EXISTS `{table_name}`;")
            create_stmt = str(CreateTable(table).compile(engine)).strip()
            if not create_stmt.endswith(";"):
                create_stmt += ";"
            sql_parts.append(create_stmt)
            sql_parts.append("")

            # ── Data ──
            result = conn.execute(text(f"SELECT * FROM `{table_name}`"))
            columns = list(result.keys())
            rows = result.fetchall()

            if rows:
                sql_parts.append(f"-- Data for `{table_name}` ({len(rows)} row(s))")
                col_list = ", ".join(f"`{c}`" for c in columns)
                for row in rows:
                    values = ", ".join(_sql_literal(v) for v in row)
                    sql_parts.append(
                        f"INSERT INTO `{table_name}` ({col_list}) VALUES ({values});"
                    )
                sql_parts.append("")

    sql_parts.append("SET FOREIGN_KEY_CHECKS=1;")
    sql_content = "\n".join(sql_parts)

    buffer = io.BytesIO(sql_content.encode("utf-8"))
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"database_backup_{timestamp}.sql"

    return StreamingResponse(
        buffer,
        media_type="application/sql",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )