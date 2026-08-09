"""Data access for content items.

Two interchangeable backends behind one interface:

  MongoStore   - the real one, backed by MongoDB (Atlas or self-hosted).
  MemoryStore  - a throwaway in-process store used only when MONGODB_URI is
                 blank, so the UI can be explored before Mongo is wired up.
                 The UI shows a loud banner in this mode. Nothing persists.
"""

import re
import threading
import uuid
from datetime import datetime, timezone

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
    """Filter predicate shared by MemoryStore (Mongo does this server-side)."""
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


class MongoStore:
    backend = "mongodb"

    def __init__(self, uri, db_name, collection_name):
        from pymongo import ASCENDING, MongoClient

        self._client = MongoClient(uri, serverSelectionTimeoutMS=8000, appname="content-tracker")
        self._col = self._client[db_name][collection_name]
        for field in ("status", "content_type", "date_publish", "created_at"):
            self._col.create_index([(field, ASCENDING)])

    def ping(self):
        self._client.admin.command("ping")
        return True

    @staticmethod
    def _out(doc):
        if not doc:
            return None
        doc = dict(doc)
        doc["id"] = str(doc.pop("_id"))
        return doc

    @staticmethod
    def _oid(item_id):
        from bson import ObjectId
        from bson.errors import InvalidId

        try:
            return ObjectId(item_id)
        except (InvalidId, TypeError):
            return None

    def list(self, query):
        mongo_query = {}
        if query.get("status"):
            mongo_query["status"] = query["status"]
        if query.get("content_type"):
            mongo_query["content_type"] = query["content_type"]
        if query.get("search"):
            rx = {"$regex": re.escape(query["search"]), "$options": "i"}
            mongo_query["$or"] = [
                {"title": rx},
                {"notes": rx},
                {"caption": rx},
                {"owner": rx},
            ]
        cursor = self._col.find(mongo_query).sort(
            [("date_publish", 1), ("created_at", 1)]
        )
        return [self._out(d) for d in cursor]

    def get(self, item_id):
        oid = self._oid(item_id)
        return self._out(self._col.find_one({"_id": oid})) if oid else None

    def create(self, data):
        doc = dict(data)
        doc["created_at"] = doc["updated_at"] = now_iso()
        doc.update(ig_media_id="", ig_permalink="", publish_error="", published_at="")
        result = self._col.insert_one(doc)
        return self._out(self._col.find_one({"_id": result.inserted_id}))

    def update(self, item_id, data):
        oid = self._oid(item_id)
        if not oid:
            return None
        patch = dict(data)
        patch["updated_at"] = now_iso()
        self._col.update_one({"_id": oid}, {"$set": patch})
        return self._out(self._col.find_one({"_id": oid}))

    def delete(self, item_id):
        oid = self._oid(item_id)
        if not oid:
            return False
        return self._col.delete_one({"_id": oid}).deleted_count == 1

    def count_published_since(self, iso_ts):
        return self._col.count_documents({"published_at": {"$gte": iso_ts}})


def build_store():
    """Pick a backend. Returns (store, note) where note explains any fallback."""
    if not config.mongo_configured():
        return MemoryStore(), (
            "MONGODB_URI is not set - running on a volatile in-memory store. "
            "Nothing you enter is saved."
        )
    store = MongoStore(config.MONGODB_URI, config.MONGODB_DB, config.MONGODB_COLLECTION)
    store.ping()
    return store, ""
