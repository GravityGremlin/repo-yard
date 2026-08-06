// deeznutz — minimal client-side JS

// CSRF token helper — reads from meta tag injected by flask-seasurf
function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') : '';
}

// HTMX extensions and helpers
document.body.addEventListener("htmx:afterRequest", (evt) => {
  if (evt.detail.xhr.status === 503) {
    console.warn("Deezer not connected");
  }
});

// Download a track or album from search results.
// `ev` is the DOM event from the onclick handler (used to flash the button).
function downloadItem(url, title, artist, type, ev) {
  const btn = ev ? ev.target : null;
  const formData = new URLSearchParams();
  formData.append("url", url);
  formData.append("title", title || "");
  formData.append("artist", artist || "");
  formData.append("type", type || "track");

  const overrideCheckbox = document.getElementById("override-existing");
  if (overrideCheckbox && overrideCheckbox.checked) {
    formData.append("override_existing", "1");
  }

  fetch("/download/enqueue", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": csrfToken() },
    body: formData.toString()
  }).then(r => {
    if (r.ok && btn) {
      btn.textContent = "Queued";
      btn.disabled = true;
    }
  }).catch(err => {
    console.error("Download enqueue failed:", err);
    if (btn) {
      btn.textContent = "Failed";
      btn.disabled = false;
    }
  });
}

// Wrapper — download a single track from search results / album track list.
// Called via onclick="downloadTrack(event, id, title, artist)" in templates.
function downloadTrack(ev, id, title, artist) {
  const url = "https://deezer.com/track/" + id;
  downloadItem(url, title, artist, "track", ev);
}

// Wrapper — download an entire album from search results / album track list.
// Called via onclick="downloadAlbum(event, id, title, artist)" in templates.
function downloadAlbum(ev, id, title, artist) {
  const url = "https://deezer.com/album/" + id;
  downloadItem(url, title, artist, "album", ev);
}

// Download an artist's discography — queues one job per album.
// Immediately disables all discography buttons and shows "Resolving…" to prevent double-clicks.
function downloadDiscography(ev, artistId, artistName) {
  const btn = ev ? ev.target : null;

  // IMMEDIATE feedback — disable ALL discography buttons
  document.querySelectorAll('.btn-discography').forEach(b => { b.disabled = true; });
  if (btn) btn.textContent = 'Resolving…';

  const formData = new URLSearchParams();
  formData.append('artist_id', artistId);
  formData.append('artist_name', artistName);

  const row = btn ? btn.closest('.artist-row') : null;
  const singlesCheck = row ? row.querySelector('.include-singles') : null;
  if (singlesCheck && singlesCheck.checked) {
    formData.append('include_singles', '1');
  }

  const overrideCheck = document.getElementById('override-existing');
  if (overrideCheck && overrideCheck.checked) {
    formData.append('override_existing', '1');
  }

  fetch('/download/discography', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': csrfToken() },
    body: formData.toString()
  }).then(r => {
    if (r.ok) return r.json();
    throw new Error('HTTP ' + r.status);
  }).then(data => {
    if (btn) btn.textContent = 'Queued ' + data.queued + ' albums';
  }).catch(err => {
    console.error('Discography enqueue failed:', err);
    if (btn) btn.textContent = 'Failed';
    document.querySelectorAll('.btn-discography').forEach(b => { b.disabled = false; });
  });
}
  const btn = ev ? ev.target : null;
  const formData = new URLSearchParams();
  formData.append("artist_id", artistId);
  formData.append("artist_name", artistName);

  // Read include-singles checkbox (near the button, inside the artist-row)
  const row = btn ? btn.closest('.artist-row') : null;
  const singlesCheck = row ? row.querySelector('.include-singles') : null;
  if (singlesCheck && singlesCheck.checked) {
    formData.append("include_singles", "1");
  }

  // Read global override-existing checkbox
  const overrideCheck = document.getElementById("override-existing");
  if (overrideCheck && overrideCheck.checked) {
    formData.append("override_existing", "1");
  }


// Subscribe to SSE for a job and update progress.
// Selectors match the CSS classes in partials/job_row.html:
//   .progress-fill  — the filled portion of the progress bar
//   .status-badge   — the status label badge
function subscribeJob(jobId, rowEl) {
  const evtSource = new EventSource(`/download/events/${jobId}`);
  const fillEl = rowEl.querySelector(".progress-fill");
  const statusEl = rowEl.querySelector(".status-badge");

  evtSource.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === "progress" && fillEl) {
      fillEl.style.width = (data.progress * 100) + "%";
    }
      if (data.type === "status") {
      if (data.phase === "duplicate-skipped") {
        const badge = rowEl.querySelector(".duplicate-skipped-badge");
        if (badge) badge.style.display = "";
      }
      if (data.phase === "overrode-existing") {
        const badge = rowEl.querySelector(".overrode-existing-badge");
        if (badge) badge.style.display = "";
      }
      if (data.status === "completed") {
        if (statusEl) { statusEl.className = "status-badge status-completed"; statusEl.textContent = data.status; }
        if (fillEl) fillEl.style.width = "100%";
        evtSource.close();
      } else if (data.status === "error") {
        if (statusEl) { statusEl.className = "status-badge status-error"; statusEl.textContent = data.status; }
        evtSource.close();
      } else if (data.status === "cancelled") {
        if (statusEl) { statusEl.className = "status-badge status-cancelled"; statusEl.textContent = data.status; }
        evtSource.close();
      }
    }
    if (data.type === "error") {
      if (statusEl) statusEl.className = "status-badge status-error";
      evtSource.close();
    }
  };

  evtSource.onerror = () => {
    // Connection dropped — show stale indicator rather than silently freezing
    if (statusEl && !statusEl.classList.contains("status-completed")
        && !statusEl.classList.contains("status-error")) {
      statusEl.textContent = statusEl.textContent + " (disconnected)";
    }
    evtSource.close();
  };
}

// Re-scan after HTMX swaps the jobs list on its 3s poll — new rows arrive after DOMContentLoaded.
const _subscribedJobs = new Set();

function _subscribeActiveRows(root = document) {
  root.querySelectorAll(".job-row[data-job-id]").forEach(row => {
    const id = row.dataset.jobId;
    if (!id || _subscribedJobs.has(id)) return;
    const status = row.querySelector(".status-badge");
    if (status && (status.classList.contains("status-running") || status.classList.contains("status-queued"))) {
      _subscribedJobs.add(id);
      subscribeJob(id, row);
    }
  });
}

document.addEventListener("DOMContentLoaded", () => _subscribeActiveRows());
document.body.addEventListener("htmx:afterSwap", (e) => {
  if (e.target && e.target.id === "jobs-list") _subscribeActiveRows(e.target);
});
