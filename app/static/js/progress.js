/* repo-yard — download progress poller
 *
 * Works with htmx's swap model: the download form POSTs to /download and htmx
 * replaces the form with a `.dl-chip` carrying the job id. We listen for that
 * swap (htmx:afterSwap on the results region), pull the service + job id out
 * of the rendered chip, and poll GET /download/<service>/<job_id> roughly
 * every 1.8s until the job settles (completed/failed/cancelled) or the
 * endpoint returns an error / is unreachable. The chip is updated in place.
 *
 * Statuses returned by the backend lane:
 *   queued | running | completed | failed | cancelled
 * JSON envelope: { service, job_id, status, progress, phase, files, error }
 */

(function () {
  'use strict';

  var POLL_MS = 1800;
  var MAX_POLLS = 600; // ~18min safety cap; long downloads still in flight server-side

  var PHASE_LABELS = {
    queued: 'queued',
    running: 'running',
    completed: 'done',
    failed: 'failed',
    cancelled: 'cancelled'
  };

  function isHtmxAvailable() {
    return typeof window.htmx !== 'undefined';
  }

  // Extract {service, jobId} from a rendered chip's data attributes; returns
  // null if the chip isn't a job we can poll (e.g. an immediate error chip).
  function chipTarget(chip) {
    if (!chip || !chip.classList.contains('dl-chip')) return null;
    var service = chip.getAttribute('data-service');
    var jobId = chip.getAttribute('data-job');
    if (!service || !jobId) return null;
    return { service: service, jobId: jobId };
  }

  // Live detail for the msg span — the label already carries the state word,
  // so this returns only the phase / percent / file count (no "running" prefix).
  function describe(status, phase, progress, files) {
    if (status === 'running') {
      var parts = [];
      if (phase) parts.push(phase);
      if (typeof progress === 'number' && progress >= 0) {
        parts.push(Math.min(99, Math.round(progress)) + '%');
      }
      return parts.join(' · ') || 'working';
    } else if (status === 'completed') {
      if (files && files.length) return files.length + (files.length === 1 ? ' file' : ' files') + ' ✓';
      return '✓';
    }
    return '';
  }

  function setState(chip, status, opts) {
    // opts: { progress, phase, files, error, statusText }
    chip.classList.remove('queued', 'running', 'completed', 'failed', 'cancelled', 'err', 'pending', 'ok');

    var bar = chip.querySelector('.dl-chip-bar');
    var label = chip.querySelector('.dl-chip-label');
    var msg = chip.querySelector('.dl-chip-msg');

    // First takeover: the backend renders the chip as `<span class="dl-chip …">
    // queued job <id></span>` — plain text, no bar/label/msg children. The moment
    // we start polling we own the chip's children, so normalize once: drop the
    // raw text, build the bar + label + msg structure the CSS expects.
    if (!msg) {
      chip.textContent = '';
      label = document.createElement('span');
      label.className = 'dl-chip-label';
      msg = document.createElement('span');
      msg.className = 'dl-chip-msg';
      chip.appendChild(label);
      chip.appendChild(msg);
    }

    // The label pins to a short state word (queued / running / done / failed);
    // the msg carries the live phase + percent or error detail.
    label.textContent = PHASE_LABELS[status] || status;

    if (status === 'queued') {
      chip.classList.add('queued');
      bar && (bar.style.width = '0%');
      msg.textContent = 'waiting for a worker';
    } else if (status === 'running') {
      chip.classList.add('running');
      if (!bar) {
        bar = document.createElement('span');
        bar.className = 'dl-chip-bar';
        chip.insertBefore(bar, label);
      }
      var pct = (typeof opts.progress === 'number' && opts.progress >= 0) ? Math.min(99, Math.round(opts.progress)) : 0;
      bar.style.width = pct + '%';
      msg.textContent = describe(status, opts.phase, opts.progress, opts.files);
    } else if (status === 'completed') {
      chip.classList.add('completed', 'ok');
      if (bar) { bar.style.width = '100%'; }
      msg.textContent = describe(status, null, null, opts.files);
    } else if (status === 'failed') {
      chip.classList.add('failed', 'err');
      if (bar) { bar.style.width = '100%'; bar.classList.add('err'); }
      msg.textContent = opts.error ? ('— ' + opts.error) : '';
    } else if (status === 'cancelled') {
      chip.classList.add('cancelled', 'err');
      if (bar) { bar.style.width = '100%'; }
      msg.textContent = opts.error ? ('— ' + opts.error) : '';
    } else {
      // unknown / unreachable — stop polling, surface a short error.
      chip.classList.add('err');
      if (bar) { bar.style.width = '100%'; bar.classList.add('err'); }
      msg.textContent = opts.statusText || ('unreachable — ' + status);
    }
  }

  function stopPolling(chip) {
    chip.removeAttribute('data-polling');
    if (chip._pollTimer) {
      clearTimeout(chip._pollTimer);
      chip._pollTimer = null;
    }
  }

  function poll(chip, service, jobId, count) {
    if (chip.getAttribute('data-polling') !== '1') return;
    if (count > MAX_POLLS) {
      setState(chip, 'unknown', { statusText: 'timed out — check the tool' });
      stopPolling(chip);
      return;
    }

    var url = '/download/' + encodeURIComponent(service) + '/' + encodeURIComponent(jobId);
    fetch(url, { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
      .then(function (resp) {
        if (!resp.ok) {
          // 4xx/5xx — stop gracefully. 404 specifically likely means the job
          // was reaped; surface a friendly message.
          setState(chip, 'unknown', {
            statusText: resp.status === 404 ? 'job not found' : ('unreachable (HTTP ' + resp.status + ')')
          });
          stopPolling(chip);
          return null;
        }
        return resp.json();
      })
      .then(function (data) {
        if (!data) return;
        if (data.error) {
          setState(chip, 'failed', { error: data.error });
          stopPolling(chip);
          return;
        }
        var status = data.status || 'unknown';
        setState(chip, status, {
          progress: data.progress,
          phase: data.phase,
          files: data.files,
          error: data.error
        });

        if (status === 'completed' || status === 'failed' || status === 'cancelled') {
          stopPolling(chip);
          return;
        }
        chip._pollTimer = setTimeout(function () {
          poll(chip, service, jobId, count + 1);
        }, POLL_MS);
      })
      .catch(function () {
        // network failure — stop, don't hammer.
        setState(chip, 'unknown', { statusText: 'unreachable' });
        stopPolling(chip);
      });
  }

  function startPolling(chip) {
    var tgt = chipTarget(chip);
    if (!tgt) return; // not a pollable chip (e.g. immediate error)
    if (chip.getAttribute('data-polling') === '1') return; // already polling
    chip.setAttribute('data-polling', '1');
    chip.setAttribute('data-service', tgt.service);
    chip.setAttribute('data-job', tgt.jobId);
    setState(chip, 'queued', {});
    // first poll quickly so the user sees motion, then settle to POLL_MS.
    chip._pollTimer = setTimeout(function () { poll(chip, tgt.service, tgt.jobId, 1); }, 350);
  }

  // Re-anchor the chip to the finished form: the rendered chip from
  // /download carries data-service + data-job. Walk new chips into polling.
  function scanForChips(root) {
    var chips = (root || document).querySelectorAll('.dl-chip[data-service][data-job]:not([data-polling])');
    for (var i = 0; i < chips.length; i++) startPolling(chips[i]);
  }

  function init() {
    // htmx fires afterSwap on the document; the form's hx-target="this"
    // replaces itself with the chip, so the chip lands in #results.
    document.body.addEventListener('htmx:afterSwap', function (evt) {
      var target = evt.detail && evt.detail.target;
      if (!target) return;
      scanForChips(target);
    });
    // also catch chips already present on initial load (e.g. back/forward).
    scanForChips(document);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
