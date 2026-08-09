# Content Tracker

A content pipeline tracker for Reels and Posts, with a direct Instagram publish
connector built on the Meta Graph API.

- **Track**: format (Reel / Post), date content came in, date of publish, status
  (Draft → In review → Rejected / Published), title, raw video URL, edited video
  URL, caption, feedback/notes, owner.
- **View**: dense table, drag-and-drop board, publishing calendar.
- **Publish**: one button per item posts the edited cut straight to Instagram and
  writes the permalink back onto the row.

Backend is Python (standard library + `PyMySQL`). Frontend is plain HTML/CSS/JS —
no build step, no bundler. There is no Node.js on this machine and none is needed.

> **Not deployable via a static-site / Node.js Git importer (Hostinger's included).**
> `package.json` exists here only so those importers stop flagging it as missing —
> there is nothing to `npm install`. If you use one of those flows, it will serve
> `web/` and nothing else: the page loads, but every feature — saving items,
> stats, Instagram publishing — depends on `server.py`, which is a Python process
> those importers don't run. To actually deploy this, run `server.py` somewhere
> that keeps a long-lived process (a VPS, or any Python host) pointed at your
> MySQL credentials in `.env`; `web/` alone is not a working deployment.

---

## Quick start

```bash
cd "content-tracker" && ./run.sh
```

First run creates `.venv`, installs `PyMySQL`, and copies `.env.example` → `.env`.
Fill in `.env`, run it again, then open <http://127.0.0.1:8787>.

Without the `MYSQL_*` values the app still runs, on a **volatile in-memory store**
— the UI shows an amber banner and nothing is saved. That mode exists so you can
look around before wiring the database up.

---

## What I need from you

Five values, all of which go into `.env` on your machine. **Do not paste them into
a chat, a ticket, or a screenshot** — a leaked Instagram token can post to your
account, and a leaked database password gives write access to everything else on
that Hostinger plan.

| Value | Where it comes from |
| --- | --- |
| `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DB` | Hostinger hPanel → Databases (see below). |
| `IG_USER_ID` | Your Instagram **Business account id** (see below). Not your @handle. |
| `IG_ACCESS_TOKEN` | A long-lived Page access token (see below). |
| `META_APP_SECRET` | developers.facebook.com → your app → Settings → Basic. Optional but recommended. |

### 1 · MySQL on Hostinger

1. **hPanel → Databases → Management → Create New MySQL Database.** Note the
   database name and username it assigns — Hostinger prefixes both with your
   account id, e.g. `u123456789_tracker` / `u123456789_admin`. Set a password
   there (not one you're reusing elsewhere).
2. **hPanel → Databases → Remote MySQL → add this Mac's IP.** This is the step
   almost everyone misses: by default the database only accepts connections
   from Hostinger's own servers, and a connection from your laptop just hangs
   until it times out, with no clearer error than that. If your ISP gives you a
   new IP periodically, you'll redo this.
3. The **host** for `MYSQL_HOST` is on that same Remote MySQL page — usually the
   server's hostname or an IP, *not* `localhost`. `localhost` only resolves
   correctly for code running on Hostinger's own server, which this app is not
   (it runs on your Mac, or wherever you deploy it).

The app creates its table (`content_items` by default, via `MYSQL_TABLE`) the
first time it connects, using `CREATE TABLE IF NOT EXISTS`. It never runs
`CREATE DATABASE` — on shared hosting the database itself is made in hPanel, and
the account typically doesn't have that grant anyway. It also never drops or
alters an existing table, so re-running it against a table you already have is
safe.

**If you'd rather run the app on Hostinger itself** (a VPS plan with Python, or
in a subprocess Cloud Startup can run), point `MYSQL_HOST` at `localhost` instead
and skip the Remote MySQL allowlist entirely — everything else is unchanged.

### 2 · Instagram prerequisites

All four must be true before publishing works at all:

1. The Instagram account is a **Professional** account (Business or Creator).
   Personal accounts cannot use the publishing API — no exceptions.
2. It is **linked to a Facebook Page** you administer.
3. You have a Meta app at [developers.facebook.com](https://developers.facebook.com)
   with the **Instagram Graph API** product added.
4. Your token carries these permissions:
   `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
   `pages_read_engagement`.

While the app is in Development mode you can publish to accounts whose users have
a role on the app (admin/developer/tester) — which covers your own account. Going
beyond that needs App Review.

### 3 · Getting `IG_USER_ID` and a long-lived token

In [Graph API Explorer](https://developers.facebook.com/tools/explorer/), select
your app, request the four permissions above, and generate a user token. Then:

```
GET /me/accounts
→ find your Page, note its "id" and "access_token"

GET /{page-id}?fields=instagram_business_account
→ the "id" it returns is your IG_USER_ID
```

The Explorer's token is short-lived (about an hour). Exchange it for a long-lived
one (about 60 days):

```
GET /oauth/access_token
  ?grant_type=fb_exchange_token
  &client_id={app-id}
  &client_secret={app-secret}
  &fb_exchange_token={short-lived-token}
```

Then re-run `GET /me/accounts` **with the long-lived user token** — the Page token
it hands back is what goes in `IG_ACCESS_TOKEN`.

The sidebar card shows `connected` with your handle and follower count once this
is right, and a specific error if it isn't.

**Tokens expire.** When publishing starts failing with Graph error code 190, the
token has lapsed — repeat this step.

### 4 · Video hosting — the constraint that catches everyone

Instagram does not accept a file upload. You give Meta a **URL and Meta downloads
the file itself**. That means:

- ✅ S3 / CloudFront, Bunny, Cloudinary, Mux, or any web server that returns the
  raw bytes at a public URL.
- ❌ Google Drive, Dropbox, OneDrive, WeTransfer. These serve an HTML preview page
  to Meta's fetcher, not the video, and the publish fails. The app detects these
  hosts and blocks the publish button before you waste an attempt.

Instagram's own media rules for Reels: MP4/MOV, H.264 + AAC, under 1 GB, 3–90
seconds, 9:16 recommended.

---

## How publishing works

Clicking publish runs Meta's two-step flow on a background thread:

```
POST /{ig-user-id}/media          → container id
GET  /{container-id}?status_code  → poll until FINISHED   (video only, can take minutes)
POST /{ig-user-id}/media_publish  → real media id
GET  /{media-id}?fields=permalink → link, written back to the row
```

Guards, because this posts to a live account and cannot be undone from here:

- A confirmation modal shows the exact account, format, media URL and caption.
- Items already carrying an `ig_media_id` are refused (409) rather than double-posted.
- Only one publish job per item can run at a time.
- A rolling 24h counter refuses to exceed `PUBLISH_DAILY_LIMIT` (Instagram's own
  cap is 25 posts / 24h).
- Setting status to "Published" by hand, or dragging a card into the Published
  column, records it in the tracker only. It never posts anything.
- A failed publish leaves the status untouched and writes the reason onto the row.

A **Post** holding a video is published as a Reel with `share_to_feed` on —
Instagram removed standalone feed video posts, so this is the real equivalent.

---

## API

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/config` | Redacted config + storage backend. No secrets. |
| `GET` | `/api/items` | `?status=&content_type=&search=` |
| `POST` | `/api/items` | Create. Only the writable field allowlist is accepted. |
| `GET` `PATCH` `DELETE` | `/api/items/{id}` | |
| `GET` | `/api/stats` | Totals, per-status, due-in-7-days, overdue. |
| `GET` | `/api/meta/account` | Connected handle + publishing quota. |
| `POST` | `/api/items/{id}/publish` | Returns `202 {job_id}`. Body `{"force":true}` re-publishes. |
| `GET` | `/api/jobs/{job_id}` | `running` \| `done` \| `error`, with progress message. |

---

## Notes and limits

- **`Draft` is an addition.** You specified Published / In review / Rejected;
  something has to hold a piece before review, so Draft is the intake state. If
  you don't want it, drop `"draft"` from `STATUSES` in `store.py` and the
  `<option>` in `web/index.html`.
- **Scheduling is a record, not an action.** "Date of publish" is a plan. Nothing
  fires on its own — publishing is always a click. Automatic publishing on the
  scheduled date would need a scheduler process; say the word and it's a small
  addition on top of what's here.
- **No auth on the server.** It binds `127.0.0.1`, so it is reachable only from
  this Mac. Do not change `HOST` to `0.0.0.0` without putting a login in front of
  it — anyone on the network would be able to post to your Instagram.
- **Uploads.** The tracker stores URLs; it does not host files. If you want to
  drag a video into the tracker and have it become a public URL, that needs an
  upload target (Cloudinary or S3) wiring in.
- **Connections and shared hosting.** Each request thread keeps its own MySQL
  connection and pings/reconnects before use, because shared hosts (Hostinger
  included) close idle connections well before the app would otherwise notice —
  the old symptom would be a `MySQL server has gone away` error mid-request.
  Hostinger Cloud Startup's own MySQL connection cap is modest; this app opens
  at most one connection per concurrent request, which fits comfortably under it
  for a small team.

## Files

```
server.py          HTTP server, JSON API, publish job runner
meta_api.py        Meta Graph API connector (the whole Instagram integration)
store.py           MySQL access, validation, in-memory fallback
config.py          .env loading
web/index.html     markup
web/app.css        design tokens + styles (dark and light)
web/app.js         client logic
```
