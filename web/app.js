/* ============================================================================
   Content Tracker — client
   No framework, no build step. Talks to the Python server's JSON API.
   ========================================================================= */

const STATUS_LABEL = {
  draft: "Draft",
  in_review: "In review",
  rejected: "Rejected",
  published: "Published",
};
const STATUS_ORDER = ["draft", "in_review", "rejected", "published"];

const state = {
  items: [],
  filters: { status: "", content_type: "", search: "" },
  view: "table",
  editing: null,      // the item currently open in the drawer, or null for new
  config: null,
  calMonth: new Date(),
  pollTimer: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* ------------------------------------------------------------------- utils */

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function icon(id, cls = "") {
  return `<svg class="${cls}" aria-hidden="true"><use href="#${id}"/></svg>`;
}

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function prettyDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-");
  const month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][Number(m) - 1];
  return `${d} ${month}`;
}

function isOverdue(item) {
  return item.date_publish && item.date_publish < todayISO() && item.status !== "published";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let data = {};
  if (text) {
    try { data = JSON.parse(text); } catch { data = { error: text.slice(0, 200) }; }
  }
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    error.payload = data;
    throw error;
  }
  return data;
}

let toastSeq = 0;
function toast(message, kind = "ok", { html = false, ms = 5000 } = {}) {
  const el = document.createElement("div");
  el.className = `toast toast--${kind}`;
  el.id = `toast-${++toastSeq}`;
  el.innerHTML = `${icon(kind === "ok" ? "i-check" : "i-alert")}<div>${html ? message : esc(message)}</div>`;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), ms);
}

/* -------------------------------------------------------------- rendering */

function renderStats(stats) {
  $("#sTotal").textContent = stats.total;
  $("#sReview").textContent = stats.by_status.in_review ?? 0;
  $("#sPub").textContent = stats.by_status.published ?? 0;
  $("#sSoon").textContent = stats.upcoming_7d;
  $("#sLate").textContent = stats.overdue;
}

function typeTag(type) {
  const isReel = type === "reel";
  return `<span class="type-tag ${isReel ? "type-tag--reel" : ""}">${icon(isReel ? "i-reel" : "i-post")}${isReel ? "Reel" : "Post"}</span>`;
}

function statusPill(status) {
  return `<span class="pill pill--${status}"><i class="dot dot--${status}"></i>${STATUS_LABEL[status] || status}</span>`;
}

function dateCell(iso, flagLate = false) {
  if (!iso) return `<span class="date date--none">—</span>`;
  return `<span class="date ${flagLate ? "date--late" : ""}">${esc(prettyDate(iso))}</span>`;
}

function renderTable() {
  const tbody = $("#tbody");
  const empty = $("#emptyTable");

  if (!state.items.length) {
    tbody.innerHTML = "";
    empty.hidden = false;
    empty.textContent = state.filters.search || state.filters.status || state.filters.content_type
      ? "Nothing matches those filters."
      : "No content yet. Hit “New content” to add the first piece.";
    return;
  }
  empty.hidden = true;

  tbody.innerHTML = state.items.map((item) => `
    <tr data-id="${esc(item.id)}">
      <td>${typeTag(item.content_type)}</td>
      <td>
        <div class="cell-title">
          <button type="button" data-open="${esc(item.id)}">${esc(item.title)}</button>
          ${item.owner ? `<small>${esc(item.owner)}</small>` : ""}
        </div>
      </td>
      <td>${dateCell(item.date_received)}</td>
      <td>${dateCell(item.date_publish, isOverdue(item))}</td>
      <td>${statusPill(item.status)}</td>
      <td>
        <div class="media-links">
          <a class="mlink ${item.raw_url ? "" : "mlink--off"}" href="${esc(item.raw_url || "#")}"
             target="_blank" rel="noopener noreferrer"
             title="Raw video" aria-label="Open raw video for ${esc(item.title)}"${item.raw_url ? "" : ' tabindex="-1" aria-disabled="true"'}>R</a>
          <a class="mlink ${item.edited_url ? "" : "mlink--off"}" href="${esc(item.edited_url || "#")}"
             target="_blank" rel="noopener noreferrer"
             title="Edited video" aria-label="Open edited video for ${esc(item.title)}"${item.edited_url ? "" : ' tabindex="-1" aria-disabled="true"'}>E</a>
          ${item.ig_permalink
            ? `<a class="mlink" href="${esc(item.ig_permalink)}" target="_blank" rel="noopener noreferrer" title="View on Instagram" aria-label="View ${esc(item.title)} on Instagram">${icon("i-instagram")}</a>`
            : ""}
        </div>
      </td>
      <td><div class="notes-cell">${esc(item.notes || "")}</div></td>
      <td>
        <div class="row-actions">
          <button class="btn btn--ghost btn--sm" data-open="${esc(item.id)}">Open</button>
        </div>
      </td>
    </tr>
  `).join("");
}

function renderBoard() {
  $("#board").innerHTML = STATUS_ORDER.map((status) => {
    const items = state.items.filter((i) => i.status === status);
    return `
      <section class="col" data-status="${status}">
        <header class="col__head">
          <i class="dot dot--${status}"></i>
          <span class="col__name">${STATUS_LABEL[status]}</span>
          <span class="col__count">${items.length}</span>
        </header>
        ${items.map((item) => `
          <article class="card" draggable="true" data-id="${esc(item.id)}" tabindex="0"
                   role="button" aria-label="Open ${esc(item.title)}">
            <div class="card__top">${typeTag(item.content_type)}</div>
            <h3 class="card__title">${esc(item.title)}</h3>
            <div class="card__meta">
              <span>${item.date_publish ? esc(prettyDate(item.date_publish)) : "no date"}</span>
              ${item.edited_url ? icon("i-link") : ""}
              ${item.ig_permalink ? icon("i-instagram") : ""}
            </div>
          </article>
        `).join("") || `<p class="empty" style="padding:20px 0;font-size:12.5px">Empty</p>`}
      </section>
    `;
  }).join("");

  wireBoardDnd();
}

function wireBoardDnd() {
  let draggedId = null;

  $$(".card").forEach((card) => {
    card.addEventListener("dragstart", (e) => {
      draggedId = card.dataset.id;
      card.classList.add("is-dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", draggedId);
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("is-dragging");
      draggedId = null;
    });
    card.addEventListener("click", () => openDrawer(card.dataset.id));
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDrawer(card.dataset.id); }
    });
  });

  $$(".col").forEach((col) => {
    col.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("is-over"); });
    col.addEventListener("dragleave", () => col.classList.remove("is-over"));
    col.addEventListener("drop", async (e) => {
      e.preventDefault();
      col.classList.remove("is-over");
      const id = draggedId || e.dataTransfer.getData("text/plain");
      const status = col.dataset.status;
      const item = state.items.find((i) => i.id === id);
      if (!item || item.status === status) return;

      // Dragging to Published only records the status. It never posts to
      // Instagram — that has to be a deliberate click in the publish panel.
      try {
        await api(`/api/items/${id}`, { method: "PATCH", body: JSON.stringify({ status }) });
        if (status === "published" && !item.ig_media_id) {
          toast("Marked as published in the tracker. Nothing was posted to Instagram.", "ok");
        }
        await refresh();
      } catch (err) {
        toast(err.message, "err");
      }
    });
  });
}

function renderCalendar() {
  const base = state.calMonth;
  const year = base.getFullYear();
  const month = base.getMonth();

  $("#calTitle").textContent = base.toLocaleString("en", { month: "long", year: "numeric" });

  const first = new Date(year, month, 1);
  // Monday-first grid.
  const offset = (first.getDay() + 6) % 7;
  const start = new Date(year, month, 1 - offset);

  const byDate = {};
  state.items.forEach((item) => {
    if (!item.date_publish) return;
    (byDate[item.date_publish] ||= []).push(item);
  });

  const today = todayISO();
  let html = "";
  for (let n = 0; n < 42; n++) {
    const day = new Date(start.getFullYear(), start.getMonth(), start.getDate() + n);
    const iso = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
    const outside = day.getMonth() !== month;
    const items = byDate[iso] || [];
    html += `
      <div class="cal__day ${outside ? "cal__day--out" : ""} ${iso === today ? "cal__day--today" : ""}">
        <span class="cal__num">${day.getDate()}</span>
        ${items.map((item) => `
          <button class="cal__item" data-open="${esc(item.id)}" title="${esc(item.title)}">
            <i class="dot dot--${item.status}"></i>${esc(item.title)}
          </button>
        `).join("")}
      </div>`;
  }
  $("#cal").innerHTML = html;
}

function renderCurrentView() {
  if (state.view === "table") renderTable();
  else if (state.view === "board") renderBoard();
  else renderCalendar();
}

/* ------------------------------------------------------------------ drawer */

let lastFocused = null;

function fillForm(item) {
  const form = $("#form");
  form.reset();
  const values = item || { content_type: "reel", status: "draft", date_received: todayISO() };
  form.querySelectorAll("[name]").forEach((field) => {
    if (field.type === "radio") field.checked = field.value === (values.content_type || "reel");
    else field.value = values[field.name] ?? "";
  });
  $("#capCount").textContent = (values.caption || "").length;
  form.querySelectorAll('[aria-invalid="true"]').forEach((f) => f.removeAttribute("aria-invalid"));
  form.querySelectorAll(".err").forEach((e) => e.remove());
}

function openDrawer(itemId) {
  lastFocused = document.activeElement;
  state.editing = itemId ? state.items.find((i) => i.id === itemId) || null : null;

  $("#drawerTitle").textContent = state.editing ? "Edit content" : "New content";
  $("#btnDelete").hidden = !state.editing;
  fillForm(state.editing);
  renderPublishPanel();

  $("#scrim").hidden = false;
  $("#drawer").hidden = false;
  setTimeout(() => $("#f_title").focus(), 60);
  document.addEventListener("keydown", onDrawerKey);
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#scrim").hidden = true;
  state.editing = null;
  document.removeEventListener("keydown", onDrawerKey);
  if (lastFocused && lastFocused.isConnected) lastFocused.focus();
}

function onDrawerKey(e) {
  if (e.key === "Escape" && $("#modal").hidden) { e.preventDefault(); closeDrawer(); }
}

/* ----------------------------------------------------- publish panel */

function publishBlockers(item) {
  const blockers = [];
  if (!state.config?.meta_configured) {
    blockers.push("Instagram is not connected. Fill IG_USER_ID and IG_ACCESS_TOKEN in .env, then restart the server.");
  }
  if (!item.edited_url) {
    blockers.push("No edited video URL. Instagram publishes the edited cut.");
  } else {
    const host = (() => { try { return new URL(item.edited_url).hostname; } catch { return ""; } })();
    if (/(drive|docs)\.google\.com|dropbox\.com|onedrive|1drv\.ms|wetransfer/i.test(host)) {
      blockers.push(`${host} share links cannot be published. Meta downloads the file directly and these hosts serve a preview page, not the video.`);
    }
  }
  if (!item.caption && !item.title) blockers.push("No caption and no title to fall back on.");
  return blockers;
}

function renderPublishPanel() {
  const panel = $("#pubPanel");
  const body = $("#pubPanelBody");
  const item = state.editing;

  if (!item) { panel.hidden = true; return; }
  panel.hidden = false;

  if (item.ig_media_id) {
    body.innerHTML = `
      <div class="published-box">
        ${icon("i-check")}
        <div>Live on Instagram${item.published_at ? ` since ${esc(item.published_at.slice(0, 10))}` : ""}.
        ${item.ig_permalink ? `<a href="${esc(item.ig_permalink)}" target="_blank" rel="noopener noreferrer">View post</a>` : ""}</div>
      </div>
      <p class="hint">Media ID <span style="font-family:var(--mono)">${esc(item.ig_media_id)}</span></p>`;
    return;
  }

  const blockers = publishBlockers(item);
  body.innerHTML = `
    ${item.publish_error ? `<div class="err-note">${icon("i-alert")}<div><strong>Last attempt failed.</strong><br>${esc(item.publish_error)}</div></div>` : ""}
    ${blockers.length
      ? `<ul class="blockers">${blockers.map((b) => `<li>${icon("i-alert")}<span>${esc(b)}</span></li>`).join("")}</ul>`
      : `<p class="hint">Ready. Save any edits first — publishing uses what is stored, not what is on screen.</p>`}
    <button type="button" class="btn btn--ig" id="btnPublish" ${blockers.length ? "disabled" : ""}>
      ${icon("i-instagram")} Publish ${item.content_type === "reel" ? "Reel" : "Post"} to Instagram
    </button>`;

  const button = $("#btnPublish");
  if (button) button.addEventListener("click", () => openPublishModal(item));
}

/* ------------------------------------------------------------------- modal */

function openModal(title, bodyHtml, footHtml) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = bodyHtml;
  $("#modalFoot").innerHTML = footHtml;
  $("#modal").hidden = false;
}

function closeModal() {
  $("#modal").hidden = true;
  $("#modalBody").innerHTML = "";
  $("#modalFoot").innerHTML = "";
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

function openPublishModal(item) {
  const caption = item.caption || item.title;
  openModal(
    "Publish to Instagram",
    `<div class="warn-note">${icon("i-alert")}<div>This posts to the live Instagram account immediately. It cannot be undone from here — you would have to delete the post in the Instagram app.</div></div>
     <dl class="summary">
       <div class="summary__row"><dt>Account</dt><dd>${esc(state.config?.ig_username ? "@" + state.config.ig_username : "IG user " + (state.config?.ig_user_id || "—"))}</dd></div>
       <div class="summary__row"><dt>Format</dt><dd>${item.content_type === "reel" ? "Reel" : "Post"}</dd></div>
       <div class="summary__row"><dt>Title</dt><dd>${esc(item.title)}</dd></div>
       <div class="summary__row"><dt>Media</dt><dd class="mono">${esc(item.edited_url)}</dd></div>
     </dl>
     <div>
       <p class="hint" style="margin-bottom:6px">Caption</p>
       <div class="caption-preview">${esc(caption)}</div>
     </div>`,
    `<button class="btn btn--ghost" id="pubCancel">Cancel</button>
     <button class="btn btn--ig" id="pubGo">${icon("i-instagram")} Publish now</button>`
  );

  $("#pubCancel").addEventListener("click", closeModal);
  $("#pubGo").addEventListener("click", () => startPublish(item));
}

async function startPublish(item) {
  openModal(
    "Publishing…",
    `<div class="progress">
       <div class="progress__bar"><div class="progress__fill"></div></div>
       <p class="progress__msg" id="pubMsg">Contacting Instagram…</p>
       <div class="log" id="pubLog"></div>
     </div>
     <p class="hint">Video processing on Instagram's side can take a few minutes. You can leave this open.</p>`,
    ``
  );

  const log = (line) => {
    const el = $("#pubLog");
    if (!el) return;
    if (el.lastElementChild?.textContent === line) return;
    const row = document.createElement("div");
    row.textContent = line;
    el.append(row);
    el.scrollTop = el.scrollHeight;
  };

  let job;
  try {
    job = await api(`/api/items/${item.id}/publish`, { method: "POST", body: "{}" });
  } catch (err) {
    return showPublishError(err.message, err.payload);
  }

  state.pollTimer = setInterval(async () => {
    let status;
    try {
      status = await api(`/api/jobs/${job.job_id}`);
    } catch {
      return; // transient; keep polling
    }

    const msg = $("#pubMsg");
    if (msg) msg.textContent = status.message;
    log(status.message);

    if (status.state === "running") return;

    clearInterval(state.pollTimer);
    state.pollTimer = null;

    if (status.state === "done") {
      const link = status.result?.permalink;
      closeModal();
      closeDrawer();
      await refresh();
      toast(
        `Published to Instagram.${link ? ` <a href="${esc(link)}" target="_blank" rel="noopener noreferrer">View post</a>` : ""}`,
        "ok", { html: true, ms: 12000 }
      );
    } else {
      await refresh();
      showPublishError(status.message, status.error);
    }
  }, 2000);
}

function showPublishError(message, payload) {
  openModal(
    "Publish failed",
    `<div class="err-note">${icon("i-alert")}<div>${esc(message)}</div></div>
     ${payload?.hint ? `<p class="hint">${esc(payload.hint)}</p>` : ""}
     ${payload?.code ? `<p class="hint">Graph error code ${esc(payload.code)}${payload.fbtrace_id ? ` · trace ${esc(payload.fbtrace_id)}` : ""}</p>` : ""}`,
    `<button class="btn btn--ghost" id="errClose">Close</button>`
  );
  $("#errClose").addEventListener("click", closeModal);
}

/* --------------------------------------------------------- connection card */

async function loadConnection() {
  const config = await api("/api/config");
  state.config = config;

  $("#brandBackend").textContent =
    config.backend === "mysql" ? `mysql · ${config.db_name}` : "in-memory (not saved)";

  const banner = $("#storeBanner");
  if (config.backend_note) {
    banner.hidden = false;
    banner.innerHTML = `${icon("i-alert")}<span>${esc(config.backend_note)}</span>`;
  } else {
    banner.hidden = true;
  }

  const badge = $("#connBadge");
  const body = $("#connBody");

  if (!config.meta_configured) {
    badge.textContent = "not set up";
    badge.className = "conn__badge is-off";
    body.innerHTML = `<p class="conn__msg">Add <span style="font-family:var(--mono)">IG_USER_ID</span> and
      <span style="font-family:var(--mono)">IG_ACCESS_TOKEN</span> to <span style="font-family:var(--mono)">.env</span>,
      then restart the server.</p>`;
    return;
  }

  badge.textContent = "checking";
  badge.className = "conn__badge";
  try {
    const { account, quota } = await api("/api/meta/account");
    state.config.ig_username = account.username;
    badge.textContent = "connected";
    badge.className = "conn__badge is-live";

    const usage = quota?.data?.[0]?.quota_usage;
    const cap = quota?.data?.[0]?.config?.quota_total ?? 25;

    body.innerHTML = `
      <div class="conn__handle">
        ${account.profile_picture_url
          ? `<img class="conn__avatar" src="${esc(account.profile_picture_url)}" alt="">`
          : `<span class="conn__avatar"></span>`}
        <div style="min-width:0">
          <div style="font-size:12.5px;font-weight:600">@${esc(account.username || "")}</div>
          <div style="font-size:11px;color:var(--text-3)">${Number(account.followers_count || 0).toLocaleString()} followers</div>
        </div>
      </div>
      ${usage !== undefined
        ? `<dl class="conn__row"><dt>Publishes today</dt><dd>${usage} / ${cap}</dd></dl>`
        : ""}
      <dl class="conn__row"><dt>Graph</dt><dd>${esc(config.graph_version)}</dd></dl>`;
  } catch (err) {
    badge.textContent = "error";
    badge.className = "conn__badge is-off";
    body.innerHTML = `<p class="conn__msg" style="color:var(--rejected)">${esc(err.message)}</p>
      ${err.payload?.meta?.hint ? `<p class="conn__msg">${esc(err.payload.meta.hint)}</p>` : ""}`;
  }
}

/* ------------------------------------------------------------------ loading */

async function refresh() {
  const params = new URLSearchParams();
  Object.entries(state.filters).forEach(([key, value]) => { if (value) params.set(key, value); });

  const [{ items }, stats] = await Promise.all([
    api(`/api/items?${params}`),
    api("/api/stats"),
  ]);

  state.items = items;
  renderStats(stats);
  renderCurrentView();

  // Keep an open drawer in sync with what the server now holds.
  if (state.editing) {
    const fresh = items.find((i) => i.id === state.editing.id);
    if (fresh) { state.editing = fresh; renderPublishPanel(); }
  }
}

/* -------------------------------------------------------------------- wire */

function setView(view) {
  state.view = view;
  $$(".nav__item").forEach((b) => {
    const on = b.dataset.view === view;
    b.classList.toggle("is-active", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  $("#viewTable").hidden = view !== "table";
  $("#viewBoard").hidden = view !== "board";
  $("#viewCalendar").hidden = view !== "calendar";
  renderCurrentView();
}

function fieldError(field, message) {
  field.setAttribute("aria-invalid", "true");
  const existing = field.parentElement.querySelector(".err");
  if (existing) existing.remove();
  const p = document.createElement("p");
  p.className = "err";
  p.innerHTML = `${icon("i-alert")}<span>${esc(message)}</span>`;
  field.parentElement.append(p);
  field.focus();
}

function wire() {
  $$(".nav__item").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));

  $$(".chip").forEach((chip) => {
    chip.addEventListener("click", async () => {
      const { filter, value } = chip.dataset;
      $$(`.chip[data-filter="${filter}"]`).forEach((c) => c.classList.toggle("is-on", c === chip));
      state.filters[filter] = value;
      await refresh();
    });
  });

  let searchTimer;
  $("#search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      state.filters.search = e.target.value.trim();
      await refresh();
    }, 250);
  });

  $("#btnNew").addEventListener("click", () => openDrawer(null));
  $("#drawerClose").addEventListener("click", closeDrawer);
  $("#btnCancel").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  $("#modalClose").addEventListener("click", closeModal);

  document.addEventListener("click", (e) => {
    const opener = e.target.closest("[data-open]");
    if (opener) openDrawer(opener.dataset.open);
  });

  $("#f_caption").addEventListener("input", (e) => {
    $("#capCount").textContent = e.target.value.length;
  });

  $("#form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    form.querySelectorAll(".err").forEach((el) => el.remove());
    form.querySelectorAll('[aria-invalid="true"]').forEach((f) => f.removeAttribute("aria-invalid"));

    const payload = Object.fromEntries(new FormData(form).entries());

    if (!payload.title?.trim()) return fieldError($("#f_title"), "Give this a title.");
    for (const [field, name] of [[$("#f_raw"), "raw_url"], [$("#f_edited"), "edited_url"]]) {
      if (payload[name] && !/^https?:\/\//i.test(payload[name])) {
        return fieldError(field, "Must start with http:// or https://");
      }
    }

    const save = $("#btnSave");
    save.disabled = true;
    save.textContent = "Saving…";
    try {
      if (state.editing) {
        await api(`/api/items/${state.editing.id}`, { method: "PATCH", body: JSON.stringify(payload) });
      } else {
        await api("/api/items", { method: "POST", body: JSON.stringify(payload) });
      }
      await refresh();
      closeDrawer();
      toast("Saved.");
    } catch (err) {
      toast(err.message, "err");
    } finally {
      save.disabled = false;
      save.textContent = "Save";
    }
  });

  $("#btnDelete").addEventListener("click", () => {
    const item = state.editing;
    if (!item) return;
    openModal(
      "Delete this item?",
      `<p style="margin:0;font-size:13.5px">“${esc(item.title)}” will be removed from the tracker. This cannot be undone.</p>
       ${item.ig_media_id ? `<div class="warn-note">${icon("i-alert")}<div>The Instagram post itself stays live. Deleting here only removes the tracking row.</div></div>` : ""}`,
      `<button class="btn btn--ghost" id="delCancel">Cancel</button>
       <button class="btn btn--danger" id="delGo">Delete</button>`
    );
    $("#delCancel").addEventListener("click", closeModal);
    $("#delGo").addEventListener("click", async () => {
      try {
        await api(`/api/items/${item.id}`, { method: "DELETE" });
        closeModal();
        closeDrawer();
        await refresh();
        toast("Deleted.");
      } catch (err) {
        closeModal();
        toast(err.message, "err");
      }
    });
  });

  $("#calPrev").addEventListener("click", () => {
    state.calMonth = new Date(state.calMonth.getFullYear(), state.calMonth.getMonth() - 1, 1);
    renderCalendar();
  });
  $("#calNext").addEventListener("click", () => {
    state.calMonth = new Date(state.calMonth.getFullYear(), state.calMonth.getMonth() + 1, 1);
    renderCalendar();
  });
  $("#calToday").addEventListener("click", () => {
    state.calMonth = new Date();
    renderCalendar();
  });

  $("#themeToggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(next);
    localStorage.setItem("ct-theme", next);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#modal").hidden) { e.preventDefault(); closeModal(); }
    // "n" for new, as long as the user is not typing into something.
    if (e.key.toLowerCase() === "n" && !e.metaKey && !e.ctrlKey && !e.altKey
        && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)
        && $("#drawer").hidden) {
      e.preventDefault();
      openDrawer(null);
    }
  });
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const toggle = $("#themeToggle");
  const dark = theme === "dark";
  toggle.innerHTML = `${icon(dark ? "i-sun" : "i-moon")}<span>${dark ? "Light" : "Dark"}</span>`;
  toggle.setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} theme`);
}

async function init() {
  applyTheme(localStorage.getItem("ct-theme") || "dark");
  wire();
  try {
    await loadConnection();
    await refresh();
  } catch (err) {
    toast(`Could not load: ${err.message}`, "err", { ms: 12000 });
  }
}

init();
