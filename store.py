"""Data access for content items.

Two interchangeable backends behind one interface:

  MySQLStore   - the real one, backed by MySQL/MariaDB (Hostinger hPanel, or
                 any other MySQL host).
  MemoryStore  - a throwaway in-process store used only when the MySQL settings
                 are blank, so the UI can be explored before the database is
                 wired up. The UI shows a loud banner in this mode. Nothing
                 persists.

Dates and timestamps cross this boundary as strings ('YYYY-MM-DD' for calendar
dates, ISO-8601 UTC for timestamps) so that neither the HTTP layer nor the
frontend has to know which backend is in use.
"""

import re
import threading
import uuid
from datetime import date, datetime, timezone

import config

CONTENT_TYPES = ("reel", "post")
STATUSES = ("draft", "in_review", "rejected", "published")

# Fields a client is allowed to write. Anything else in a request body is
# ignored rather than trusted - this is the whole input allowlist.
WRITABLE = (
    "content_type",
    "title",
    "status",
    "date_received",
    "date_publish",
    "raw_url",
    "edited_url",
    "caption",
    "notes",
    "owner",
)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ValidationError(ValueError):
    """Raised for a bad client payload. Surfaces to the user as a 400."""


def clean(payload: dict, *, partial: bool) -> dict:
    """Validate and normalise an incoming item payload.

    partial=True is for PATCH: only the keys actually present are checked.
    """
    if not isinstance(payload, dict):
        raise ValidationError("Expected a JSON object.")

    out = {}
    for key in WRITABLE:
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            value = ""
        if not isinstance(value, str):
            value = str(value)
        out[key] = value.strip()

    if not partial:
        out.setdefault("content_type", "reel")
        out.setdefault("status", "draft")
        for key in WRITABLE:
            out.setdefault(key, "")

    if "content_type" in out and out["content_type"] not in CONTENT_TYPES:
        raise ValidationError(
            f"content_type must be one of {', '.join(CONTENT_TYPES)}."
        )
    if "status" in out and out["status"] not in STATUSES:
        raise ValidationError(f"status must be one of {', '.join(STATUSES)}.")

    for key in ("date_received", "date_publish"):
        if out.get(key) and not DATE_RE.match(out[key]):
            raise ValidationError(f"{key} must be YYYY-MM-DD (or empty).")

    for key in ("raw_url", "edited_url"):
        value = out.get(key)
        if value and not value.startswith(("http://", "https://")):
            raise ValidationError(f"{key} must start with http:// or https://.")

    if not partial and not out.get("title"):
        raise ValidationError("Title is required.")
    if partial and "title" in out and not out["title"]:
        raise ValidationError("Title cannot be blank.")

    return out


def _matches(doc: dict, query: dict) -> bool:
    """Filter predicate for MemoryStore (MySQL does this in the WHERE clause)."""
    if query.get("status") and doc.get("status") != query["status"]:
        return False
    if query.get("content_type") and doc.get("content_type") != query["content_type"]:
        return False
    search = (query.get("search") or "").lower()
    if search:
        haystack = " ".join(
            str(doc.get(k, "")) for k in ("title", "notes", "caption", "owner")
        ).lower()
        if search not in haystack:
            return False
    return True


class MemoryStore:
    """Volatile store. Exists so the UI is explorable without a database."""

    backend = "memory"

    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()

    def ping(self):
        return True

    def list(self, query):
        with self._lock:
            docs = [dict(d) for d in self._items.values() if _matches(d, query)]
        docs.sort(key=lambda d: (d.get("date_publish") or "9999-99-99", d["created_at"]))
        return docs

    def get(self, item_id):
        with self._lock:
            doc = self._items.get(item_id)
            return dict(doc) if doc else None

    def create(self, data):
        doc = dict(data)
        doc["id"] = uuid.uuid4().hex
        doc["created_at"] = doc["updated_at"] = now_iso()
        doc.setdefault("ig_media_id", "")
        doc.setdefault("ig_permalink", "")
        doc.setdefault("publish_error", "")
        doc.setdefault("published_at", "")
        with self._lock:
            self._items[doc["id"]] = doc
        return dict(doc)

    def update(self, item_id, data):
        with self._lock:
            doc = self._items.get(item_id)
            if not doc:
                return None
            doc.update(data)
            doc["updated_at"] = now_iso()
            return dict(doc)

    def delete(self, item_id):
        with self._lock:
            return self._items.pop(item_id, None) is not None

    def count_published_since(self, iso_ts):
        with self._lock:
            return sum(
                1
                for d in self._items.values()
                if d.get("published_at") and d["published_at"] >= iso_ts
            )


# --- MySQL type conversion ---------------------------------------------------
# The rest of the app speaks strings. MySQL speaks date/datetime. Everything
# crossing that line goes through these four, and empty string means NULL.

def _to_date(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _from_date(value):
    if not value:
        return ""
    return value.isoformat() if isinstance(value, date) else str(value)


def _to_dt(value):
    """ISO-8601 string -> naive UTC datetime, which is what DATETIME stores."""
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _from_dt(value):
    if not value:
        return ""
    if isinstance(value, str):
        return value
    return value.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


DATE_FIELDS = ("date_received", "date_publish")
DT_FIELDS = ("published_at", "created_at", "updated_at")

COLUMNS = (
    "content_type", "title", "status", "date_received", "date_publish",
    "raw_url", "edited_url", "caption", "notes", "owner",
    "ig_media_id", "ig_permalink", "publish_error", "published_at",
    "created_at", "updated_at",
)

# %s and _ are LIKE wildcards; a user searching for "50_off" means the literal.
LIKE_ESCAPE = str.maketrans({"\\": r"\\", "%": r"\%", "_": r"\_"})


class MySQLStore:
    """MySQL / MariaDB backend.

    Connections are per-thread and re-pinged before use. Shared hosts (Hostinger
    included) drop idle MySQL connections well before the app would notice, and
    a stale socket surfaces as a confusing 'server has gone away' mid-request.
    """

    backend = "mysql"

    def __init__(self, *, host, port, user, password, database, table, connect_timeout=10):
        import pymysql
        from pymysql.cursors import DictCursor

        # A table name cannot be a bound parameter, so it is validated as a
        # bare identifier instead of ever being trusted verbatim.
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", table):
            raise ValueError(f"Invalid table name: {table!r}")

        self._pymysql = pymysql
        self._settings = dict(
            host=host, port=port, user=user, password=password, database=database,
            charset="utf8mb4",          # emoji in captions need utf8mb4, not utf8
            autocommit=True,
            connect_timeout=connect_timeout,
            cursorclass=DictCursor,
        )
        self._table = table
        self._local = threading.local()
        self._ensure_schema()

    # -- connection ----------------------------------------------------------

    def _connect(self):
        return self._pymysql.connect(**self._settings)

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
            return conn
        try:
            conn.ping(reconnect=True)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _query(self, sql, params=(), *, fetch=None):
        with self._conn().cursor() as cur:
            cur.execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.rowcount, cur.lastrowid

    def ping(self):
        self._conn().ping(reconnect=True)
        return True

    # -- schema --------------------------------------------------------------

    def _ensure_schema(self):
        """Creates the table if absent. Never drops or alters an existing one.

        CREATE DATABASE is deliberately not attempted - on shared hosting the
        database is made in the control panel and the user has no such grant.
        """
        self._query(f"""
            CREATE TABLE IF NOT EXISTS `{self._table}` (
              id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              content_type  VARCHAR(16)  NOT NULL DEFAULT 'reel',
              title         VARCHAR(200) NOT NULL,
              status        VARCHAR(16)  NOT NULL DEFAULT 'draft',
              date_received DATE         NULL,
              date_publish  DATE         NULL,
              raw_url       VARCHAR(1024) NOT NULL DEFAULT '',
              edited_url    VARCHAR(1024) NOT NULL DEFAULT '',
              caption       TEXT         NULL,
              notes         TEXT         NULL,
              owner         VARCHAR(80)  NOT NULL DEFAULT '',
              ig_media_id   VARCHAR(64)  NOT NULL DEFAULT '',
              ig_permalink  VARCHAR(512) NOT NULL DEFAULT '',
              publish_error TEXT         NULL,
              published_at  DATETIME     NULL,
              created_at    DATETIME     NOT NULL,
              updated_at    DATETIME     NOT NULL,
              PRIMARY KEY (id),
              KEY idx_status (status),
              KEY idx_content_type (content_type),
              KEY idx_date_publish (date_publish),
              KEY idx_created_at (created_at),
              KEY idx_ig_media_id (ig_media_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

    # -- conversion ----------------------------------------------------------

    @staticmethod
    def _out(row):
        if not row:
            return None
        item = {"id": str(row["id"])}
        for column in COLUMNS:
            value = row.get(column)
            if column in DATE_FIELDS:
                item[column] = _from_date(value)
            elif column in DT_FIELDS:
                item[column] = _from_dt(value)
            else:
                item[column] = "" if value is None else value
        return item

    @staticmethod
    def _encode(column, value):
        if column in DATE_FIELDS:
            return _to_date(value)
        if column in DT_FIELDS:
            return _to_dt(value)
        return value

    # -- reads ---------------------------------------------------------------

    def list(self, query):
        where, params = [], []
        if query.get("status"):
            where.append("status = %s")
            params.append(query["status"])
        if query.get("content_type"):
            where.append("content_type = %s")
            params.append(query["content_type"])
        if query.get("search"):
            needle = f"%{query['search'].translate(LIKE_ESCAPE)}%"
            where.append(
                "(title LIKE %s ESCAPE '\\\\' OR notes LIKE %s ESCAPE '\\\\' "
                "OR caption LIKE %s ESCAPE '\\\\' OR owner LIKE %s ESCAPE '\\\\')"
            )
            params.extend([needle] * 4)

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._query(
            # date_publish IS NULL first in the sort keeps undated items last,
            # since MySQL would otherwise sort NULL to the top.
            f"SELECT * FROM `{self._table}` {clause} "
            f"ORDER BY (date_publish IS NULL), date_publish, created_at, id",
            tuple(params),
            fetch="all",
        )
        return [self._out(r) for r in rows]

    def get(self, item_id):
        if not str(item_id).isdigit():
            return None
        row = self._query(
            f"SELECT * FROM `{self._table}` WHERE id = %s", (int(item_id),), fetch="one"
        )
        return self._out(row)

    def count_published_since(self, iso_ts):
        row = self._query(
            f"SELECT COUNT(*) AS n FROM `{self._table}` WHERE published_at >= %s",
            (_to_dt(iso_ts),),
            fetch="one",
        )
        return int(row["n"]) if row else 0

    # -- writes --------------------------------------------------------------

    def create(self, data):
        now = _to_dt(now_iso())
        values = {column: "" for column in COLUMNS}
        values.update(data)
        values.update(
            ig_media_id="", ig_permalink="", publish_error="", published_at="",
            created_at=now, updated_at=now,
        )
        payload = {c: self._encode(c, values[c]) for c in COLUMNS}
        placeholders = ", ".join(["%s"] * len(COLUMNS))
        columns = ", ".join(f"`{c}`" for c in COLUMNS)
        _, new_id = self._query(
            f"INSERT INTO `{self._table}` ({columns}) VALUES ({placeholders})",
            tuple(payload[c] for c in COLUMNS),
        )
        return self.get(new_id)

    def update(self, item_id, data):
        if not str(item_id).isdigit():
            return None
        patch = {c: v for c, v in data.items() if c in COLUMNS}
        patch["updated_at"] = _to_dt(now_iso())
        assignments = ", ".join(f"`{c}` = %s" for c in patch)
        params = [self._encode(c, v) for c, v in patch.items()] + [int(item_id)]
        rows, _ = self._query(
            f"UPDATE `{self._table}` SET {assignments} WHERE id = %s", tuple(params)
        )
        return self.get(item_id)

    def delete(self, item_id):
        if not str(item_id).isdigit():
            return False
        rows, _ = self._query(
            f"DELETE FROM `{self._table}` WHERE id = %s", (int(item_id),)
        )
        return rows == 1


def build_store():
    """Pick a backend. Returns (store, note) where note explains any fallback."""
    if not config.mysql_configured():
        return MemoryStore(), (
            "MySQL is not configured - running on a volatile in-memory store. "
            "Nothing you enter is saved. Set MYSQL_HOST, MYSQL_USER, "
            "MYSQL_PASSWORD and MYSQL_DB in .env."
        )
    store = MySQLStore(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DB,
        table=config.MYSQL_TABLE,
        connect_timeout=config.MYSQL_CONNECT_TIMEOUT,
    )
    store.ping()
    return store, ""
