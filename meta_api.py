"""Meta Graph API connector for Instagram content publishing.

Implements Instagram's two-step publish flow:

    1. POST /{ig-user-id}/media          -> returns a *container* id
    2. poll  /{container-id}?fields=status_code   until FINISHED
    3. POST /{ig-user-id}/media_publish  -> returns the real media id
    4. GET   /{media-id}?fields=permalink

Two constraints from Meta that shape everything here, and that you cannot
work around from the client side:

  * Meta *pulls* the file from a public URL. There is no file upload. Google
    Drive / Dropbox / OneDrive share links fail - they serve an HTML viewer
    page, not the bytes.
  * Video containers are processed asynchronously. FINISHED can take anywhere
    from a few seconds to several minutes, so publishing runs as a background
    job rather than inside one HTTP request.
"""

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import config

GRAPH_HOST = "https://graph.facebook.com"

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".heic", ".webp")
TIMEOUT = 30

# Container polling. Meta suggests polling no faster than once per second and
# giving video processing a generous ceiling.
POLL_INTERVAL = 4
POLL_MAX_SECONDS = 420


class MetaError(RuntimeError):
    """A Graph API error, already unwrapped into something readable."""

    def __init__(self, message, *, code=None, subcode=None, fbtrace=None, hint=""):
        super().__init__(message)
        self.message = message
        self.code = code
        self.subcode = subcode
        self.fbtrace = fbtrace
        self.hint = hint

    def as_dict(self):
        return {
            "message": self.message,
            "code": self.code,
            "subcode": self.subcode,
            "fbtrace_id": self.fbtrace,
            "hint": self.hint,
        }


# Error codes worth translating, because Meta's own text is not actionable.
HINTS = {
    190: "The access token is invalid or expired. Generate a fresh long-lived "
         "token and update IG_ACCESS_TOKEN in .env.",
    200: "The token is missing a permission. You need instagram_basic, "
         "instagram_content_publish, pages_show_list and pages_read_engagement.",
    2207026: "Instagram rejected the video format. Use MP4/MOV, H.264 video, "
             "AAC audio, under 1GB, 3-90s for a Reel.",
    2207003: "Meta could not download the file from that URL. It must be a "
             "direct, public link to the media file itself.",
    2207032: "Container creation failed on Meta's side - usually an "
             "unreachable or non-public media URL.",
    9007: "You have hit Instagram's publishing rate limit (25 posts / 24h).",
    4: "Application request limit reached. Wait and retry.",
}


def _appsecret_proof(token: str) -> str:
    """Signs API calls with the app secret. Meta recommends this whenever the
    secret is available; it stops a leaked token being used elsewhere."""
    return hmac.new(
        config.META_APP_SECRET.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _auth_params() -> dict:
    params = {"access_token": config.IG_ACCESS_TOKEN}
    if config.META_APP_SECRET:
        params["appsecret_proof"] = _appsecret_proof(config.IG_ACCESS_TOKEN)
    return params


def _request(method: str, path: str, params: dict) -> dict:
    if not config.meta_configured():
        raise MetaError(
            "Meta is not configured.",
            hint="Set IG_USER_ID and IG_ACCESS_TOKEN in .env, then restart the server.",
        )

    url = f"{GRAPH_HOST}/{config.GRAPH_VERSION}/{path.lstrip('/')}"
    payload = {**_auth_params(), **{k: v for k, v in params.items() if v not in (None, "")}}

    if method == "GET":
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(payload)}", method="GET"
        )
    else:
        request = urllib.request.Request(
            url, data=urllib.parse.urlencode(payload).encode("utf-8"), method="POST"
        )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            error = json.loads(raw).get("error", {})
        except ValueError:
            raise MetaError(f"Graph API HTTP {exc.code}: {raw[:300]}") from exc
        code = error.get("code")
        raise MetaError(
            error.get("error_user_msg") or error.get("message") or f"Graph API HTTP {exc.code}",
            code=code,
            subcode=error.get("error_subcode"),
            fbtrace=error.get("fbtrace_id"),
            hint=HINTS.get(code, ""),
        ) from exc
    except urllib.error.URLError as exc:
        raise MetaError(f"Could not reach Meta: {exc.reason}") from exc


def infer_media_kind(url: str) -> str:
    """'image' or 'video', guessed from the URL path.

    Deliberately ignores the query string - signed CDN URLs carry all kinds of
    junk after the '?' that would otherwise confuse the extension check.
    """
    path = urllib.parse.urlparse(url).path.lower()
    return "image" if path.endswith(IMAGE_EXTENSIONS) else "video"


def account_info() -> dict:
    return _request(
        "GET",
        config.IG_USER_ID,
        {"fields": "id,username,name,profile_picture_url,followers_count,media_count"},
    )


def publishing_quota() -> dict:
    """How many of the 25 daily publishes Meta thinks you have used."""
    return _request(
        "GET",
        f"{config.IG_USER_ID}/content_publishing_limit",
        {"fields": "config,quota_usage"},
    )


def create_container(*, content_type: str, media_url: str, caption: str) -> str:
    """Step 1. Returns the container id.

    content_type is the tracker's own 'reel' | 'post'. Note that Instagram no
    longer accepts feed *video* posts as a separate type - a video sent to the
    feed becomes a Reel - so a 'post' holding a video is published as a Reel
    with share_to_feed on, which is the closest equivalent.
    """
    kind = infer_media_kind(media_url)
    params = {"caption": caption}

    if kind == "image":
        if content_type == "reel":
            raise MetaError(
                "This item is marked as a Reel but the URL looks like an image.",
                hint="Reels need a video file (MP4/MOV). Switch the type to Post, "
                     "or point at the video.",
            )
        params["image_url"] = media_url
    else:
        params["media_type"] = "REELS"
        params["video_url"] = media_url
        params["share_to_feed"] = "true"

    response = _request("POST", f"{config.IG_USER_ID}/media", params)
    container_id = response.get("id")
    if not container_id:
        raise MetaError(f"Meta did not return a container id: {response}")
    return container_id


def container_status(container_id: str) -> dict:
    return _request("GET", container_id, {"fields": "status_code,status"})


def wait_for_container(container_id: str, on_progress=None) -> None:
    """Step 2. Blocks until the container is FINISHED, or raises."""
    deadline = time.time() + POLL_MAX_SECONDS
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        state = container_status(container_id)
        code = state.get("status_code", "UNKNOWN")

        if code == "FINISHED":
            return
        if code == "PUBLISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise MetaError(
                f"Instagram could not process the media ({code}). "
                f"{state.get('status', '')}".strip(),
                hint="Most often the file is not publicly reachable, or the "
                     "codec/length is outside Instagram's limits.",
            )

        if on_progress:
            elapsed = int(POLL_MAX_SECONDS - (deadline - time.time()))
            on_progress(f"Instagram is processing the media ({elapsed}s elapsed)...")
        time.sleep(POLL_INTERVAL)

    raise MetaError(
        f"Timed out after {POLL_MAX_SECONDS}s waiting for Instagram to process the media.",
        hint="The container may still finish. Check Instagram before retrying, "
             "so you don't publish twice.",
    )


def publish_container(container_id: str) -> str:
    """Step 3. Returns the published media id."""
    response = _request(
        "POST", f"{config.IG_USER_ID}/media_publish", {"creation_id": container_id}
    )
    media_id = response.get("id")
    if not media_id:
        raise MetaError(f"Meta did not return a media id: {response}")
    return media_id


def media_permalink(media_id: str) -> str:
    """Step 4. Best-effort - a missing permalink must not fail a real publish."""
    try:
        return _request("GET", media_id, {"fields": "permalink"}).get("permalink", "")
    except MetaError:
        return ""


def publish(*, content_type: str, media_url: str, caption: str, on_progress=None) -> dict:
    """The whole flow. Returns {media_id, permalink, container_id}."""

    def say(message):
        if on_progress:
            on_progress(message)

    say("Creating the media container on Instagram...")
    container_id = create_container(
        content_type=content_type, media_url=media_url, caption=caption
    )

    if infer_media_kind(media_url) == "video":
        wait_for_container(container_id, on_progress=on_progress)
    else:
        say("Image container ready.")

    say("Publishing to Instagram...")
    media_id = publish_container(container_id)

    say("Fetching the permalink...")
    return {
        "container_id": container_id,
        "media_id": media_id,
        "permalink": media_permalink(media_id),
    }
