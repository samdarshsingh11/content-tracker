# Content Tracker

A content pipeline tracker for Reels and Posts, with a direct Instagram publish
connector built on the Meta Graph API.

- **Track**: format (Reel / Post), date content came in, date of publish, status
  (Draft → In review → Rejected / Published), title, raw video URL, edited video
  URL, caption, feedback/notes, owner.
- **View**: dense table, drag-and-drop board, publishing calendar.
- **Publish**: one button per item posts the edited cut straight to Instagram and
  writes the permalink back onto the row.

Backend is Python (standard library + `pymongo`). Frontend is plain HTML/CSS/JS —
no build step, no bundler. There is no Node.js on this machine and none is needed.

---

## Quick start

```bash
cd "content-tracker" && ./run.sh
```

First run creates `.venv`, installs `pymongo`, and copies `.env.example` → `.env`.
Fill in `.env`, run it again, then open <http://127.0.0.1:8787>.

Without `MONGODB_URI` the app still runs, on a **volatile in-memory store** — the
UI shows an amber banner and nothing is saved. That mode exists so you can look
around before wiring the database up.

---

## What I need from you

Four values, all of which go into `.env` on your machine. **Do not paste them into
a chat, a ticket, or a screenshot** — a leaked Instagram token can post to your
account.

| Value | Where it comes from |
| --- | --- |
| `MONGODB_URI` | Atlas → Database → Connect → Drivers. Or `mongodb://127.0.0.1:27017` for a local server. |
| `IG_USER_ID` | Your Instagram **Business account id** (see below). Not your @handle. |
| `IG_ACCESS_TOKEN` | A long-lived Page access token (see below). |
| `META_APP_SECRET` | developers.facebook.com → your app → Settings → Basic. Optional but recommended. |

### 1 · MongoDB

Atlas free tier (M0) is enough. Two things trip people up:

- **Network Access** — add your current IP to the allowlist, or the driver hangs
  and then times out. If your IP changes, this breaks and the symptom is a
  connection timeout with no other explanation.
- **Password encoding** — if the DB password contains `@ : / ? # [ ]`, percent-encode
  those characters inside the URI, or the connection string parses wrong.

The app creates the database, collection and indexes on first connect. Nothing to
set up by hand.

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

## Files

```
server.py          HTTP server, JSON API, publish job runner
meta_api.py        Meta Graph API connector (the whole Instagram integration)
store.py           MongoDB access, validation, in-memory fallback
config.py          .env loading
web/index.html     markup
web/app.css        design tokens + styles (dark and light)
web/app.js         client logic
```
