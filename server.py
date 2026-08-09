"""Content Tracker - HTTP server, JSON API and Instagram publish orchestration.

Runs on the Python standard library alone apart from pymongo. Start it with:

    ./run.sh          (or)          python3 server.py

Then open http://127.0.0.1:8787
"""

import json
import mimetypes
import re
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import config
import meta_api
import store as store_module

STORE = None
STORE_NOTE = ""

# --- Background publish jobs -------------------------------------------------
# Video containers can take minutes to process, so a publish runs on its own
# thread and the browser polls /api/jobs/<id> for progress.
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = timedelta(hours=6)


def new_job(item_id):
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        _reap_jobs()
        JOBS[job_id] = {
            "id": job_id,
            "item_id": item_id,
            "state": "running",
            "message": "Starting...",
            "result": None,
            "error": None,
            "started_at": store_module.now_iso(),
        }
    return job_id


def _reap_jobs():
    """Caller must hold JOBS_LOCK. Drops finished jobs older than the TTL."""
    cutoff = datetime.now(timezone.utc) - JOB_TTL
    for job_id, job in list(JOBS.items()):
        if job["state"] == "running":
            continue
        try:
            started = datetime.fromisoformat(job["started_at"])
        except ValueError:
            continue
        if started < cutoff:
            JOBS.pop(job_id, None)


def update_job(job_id, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def run_publish(job_id, item):
    """Executes the Instagram publish for one item and writes the result back."""
    item_id = item["id"]
    media_url = item.get("edited_url") or ""

    def progress(message):
        update_job(job_id, message=message)

    try:
        result = meta_api.publish(
            content_type=item.get("content_type", "reel"),
            media_url=media_url,
            caption=item.get("caption") or item.get("title") or "",
            on_progress=progress,
        )
        published_at = store_module.now_iso()
        STORE.update(
            item_id,
            {
                "status": "published",
                "ig_media_id": result["media_id"],
                "ig_permalink": result["permalink"],
                "published_at": published_at,
                "publish_error": "",
                "date_publish": item.get("date_publish") or published_at[:10],
            },
        )
        update_job(
            job_id,
            state="done",
            message="Published to Instagram.",
            result={**result, "item": STORE.get(item_id)},
        )
    except meta_api.MetaError as exc:
        STORE.update(item_id, {"publish_error": exc.message})
        update_job(job_id, state="error", message=exc.message, error=exc.as_dict())
    except Exception as exc:  # noqa: BLE001 - a job thread must never die silently
        traceback.print_exc()
        message = f"Unexpected error: {exc}"
        STORE.update(item_id, {"publish_error": message})
        update_job(job_id, state="error", message=message, error={"message": message})


# --- Stats -------------------------------------------------------------------

def build_stats():
    items = STORE.list({})
    today = datetime.now(timezone.utc).date()
    week_ahead = today + timedelta(days=7)

    counts = {s: 0 for s in store_module.STATUSES}
    types = {t: 0 for t in store_module.CONTENT_TYPES}
    upcoming = 0
    overdue = 0

    for item in items:
        counts[item.get("status", "draft")] = counts.get(item.get("status", "draft"), 0) + 1
        types[item.get("content_type", "reel")] = types.get(item.get("content_type", "reel"), 0) + 1
        date_publish = item.get("date_publish")
        if not date_publish:
            continue
        try:
            scheduled = datetime.strptime(date_publish, "%Y-%m-%d").date()
        except ValueError:
            continue
        if item.get("status") != "published":
            if scheduled < today:
                overdue += 1
            elif scheduled <= week_ahead:
                upcoming += 1

    return {
        "total": len(items),
        "by_status": counts,
        "by_type": types,
        "upcoming_7d": upcoming,
        "overdue": overdue,
    }


# --- HTTP --------------------------------------------------------------------

ITEM_PATH = re.compile(r"^/api/items/([A-Za-z0-9_-]+)$")
PUBLISH_PATH = re.compile(r"^/api/items/([A-Za-z0-9_-]+)/publish$")
JOB_PATH = re.compile(r"^/api/jobs/([A-Za-z0-9]+)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "ContentTracker/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------------

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def _send(self, status, body=b"", content_type="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, payload, status=HTTPStatus.OK):
        self._send(status, json.dumps(payload, default=str).encode("utf-8"))

    def fail(self, status, message, **extra):
        self.json({"error": message, **extra}, status=status)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise store_module.ValidationError("Request body too large.")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError as exc:
            raise store_module.ValidationError(f"Invalid JSON: {exc}") from exc

    # -- static --------------------------------------------------------------

    def serve_static(self, path):
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (config.WEB_DIR / unquote(relative)).resolve()
        # Path containment check - stops ../ escapes out of web/.
        if not str(target).startswith(str(config.WEB_DIR.resolve())) or not target.is_file():
            return self.fail(HTTPStatus.NOT_FOUND, "Not found.")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)

    # -- routing -------------------------------------------------------------

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        parsed = urlparse(self.path)
        path = parsed.path

        if not path.startswith("/api/"):
            return self.serve_static(path)

        try:
            if path == "/api/config":
                return self.json(
                    {
                        **config.redacted_summary(),
                        "backend": STORE.backend,
                        "backend_note": STORE_NOTE,
                        "statuses": list(store_module.STATUSES),
                        "content_types": list(store_module.CONTENT_TYPES),
                    }
                )

            if path == "/api/items":
                query = parse_qs(parsed.query)
                return self.json(
                    {
                        "items": STORE.list(
                            {
                                "status": (query.get("status") or [""])[0],
                                "content_type": (query.get("content_type") or [""])[0],
                                "search": (query.get("search") or [""])[0],
                            }
                        )
                    }
                )

            if path == "/api/stats":
                return self.json(build_stats())

            if path == "/api/meta/account":
                if not config.meta_configured():
                    return self.fail(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "Meta is not configured. Add IG_USER_ID and IG_ACCESS_TOKEN to .env.",
                    )
                account = meta_api.account_info()
                quota = {}
                try:
                    quota = meta_api.publishing_quota()
                except meta_api.MetaError:
                    pass  # quota is a nice-to-have, never block on it
                return self.json({"account": account, "quota": quota})

            match = ITEM_PATH.match(path)
            if match:
                item = STORE.get(match.group(1))
                return self.json(item) if item else self.fail(HTTPStatus.NOT_FOUND, "No such item.")

            match = JOB_PATH.match(path)
            if match:
                with JOBS_LOCK:
                    job = JOBS.get(match.group(1))
                    job = dict(job) if job else None
                return self.json(job) if job else self.fail(HTTPStatus.NOT_FOUND, "No such job.")

            return self.fail(HTTPStatus.NOT_FOUND, "Unknown endpoint.")

        except meta_api.MetaError as exc:
            return self.fail(HTTPStatus.BAD_GATEWAY, exc.message, meta=exc.as_dict())
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/items":
                data = store_module.clean(self.read_json(), partial=False)
                return self.json(STORE.create(data), status=HTTPStatus.CREATED)

            match = PUBLISH_PATH.match(path)
            if match:
                return self.start_publish(match.group(1))

            return self.fail(HTTPStatus.NOT_FOUND, "Unknown endpoint.")

        except store_module.ValidationError as exc:
            return self.fail(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_PATCH(self):  # noqa: N802
        path = urlparse(self.path).path
        match = ITEM_PATH.match(path)
        if not match:
            return self.fail(HTTPStatus.NOT_FOUND, "Unknown endpoint.")
        try:
            data = store_module.clean(self.read_json(), partial=True)
            if not data:
                return self.fail(HTTPStatus.BAD_REQUEST, "Nothing to update.")
            item = STORE.update(match.group(1), data)
            return self.json(item) if item else self.fail(HTTPStatus.NOT_FOUND, "No such item.")
        except store_module.ValidationError as exc:
            return self.fail(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_DELETE(self):  # noqa: N802
        match = ITEM_PATH.match(urlparse(self.path).path)
        if not match:
            return self.fail(HTTPStatus.NOT_FOUND, "Unknown endpoint.")
        if STORE.delete(match.group(1)):
            return self.json({"deleted": True})
        return self.fail(HTTPStatus.NOT_FOUND, "No such item.")

    # -- publish -------------------------------------------------------------

    def start_publish(self, item_id):
        """Preflight, then hand the actual publish to a background thread.

        Every check here is a guard against posting something irreversible to a
        live Instagram account by accident.
        """
        item = STORE.get(item_id)
        if not item:
            return self.fail(HTTPStatus.NOT_FOUND, "No such item.")

        if not config.meta_configured():
            return self.fail(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Meta is not configured. Add IG_USER_ID and IG_ACCESS_TOKEN to .env "
                "and restart the server.",
            )

        if not item.get("edited_url"):
            return self.fail(
                HTTPStatus.BAD_REQUEST,
                "This item has no edited video URL. Instagram publishes the edited "
                "cut, never the raw file.",
            )

        body = self.read_json() if self.headers.get("Content-Length") else {}
        force = bool(body.get("force"))

        if item.get("ig_media_id") and not force:
            return self.fail(
                HTTPStatus.CONFLICT,
                "This item was already published to Instagram.",
                permalink=item.get("ig_permalink", ""),
                already_published=True,
            )

        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        if STORE.count_published_since(since) >= config.PUBLISH_DAILY_LIMIT:
            return self.fail(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"Daily publish limit reached ({config.PUBLISH_DAILY_LIMIT} in 24h). "
                "Instagram rejects posts beyond this.",
            )

        with JOBS_LOCK:
            for job in JOBS.values():
                if job["item_id"] == item_id and job["state"] == "running":
                    return self.fail(
                        HTTPStatus.CONFLICT,
                        "A publish is already running for this item.",
                        job_id=job["id"],
                    )

        job_id = new_job(item_id)
        threading.Thread(
            target=run_publish, args=(job_id, item), daemon=True, name=f"publish-{item_id}"
        ).start()
        return self.json({"job_id": job_id}, status=HTTPStatus.ACCEPTED)


def main():
    global STORE, STORE_NOTE

    try:
        STORE, STORE_NOTE = store_module.build_store()
    except Exception as exc:  # noqa: BLE001
        print(f"\n  Could not connect to MongoDB: {exc}\n")
        print("  Check MONGODB_URI in .env, and that this machine's IP is on the")
        print("  Atlas Network Access allowlist.\n")
        raise SystemExit(1)

    print("\n  Content Tracker")
    print(f"  http://{config.HOST}:{config.PORT}\n")
    print(f"  storage : {STORE.backend}" + (f"  <-- {STORE_NOTE}" if STORE_NOTE else ""))
    print(f"  meta    : {'connected' if config.meta_configured() else 'not configured'}")
    print(f"  graph   : {config.GRAPH_VERSION}\n")

    httpd = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")
        httpd.server_close()


if __name__ == "__main__":
    main()
