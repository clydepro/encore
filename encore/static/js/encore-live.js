/* Encore's live layer: SSE in, DOM out (SAPRS 9.8, 9.10, 10.5, ADR-012).
 *
 * Deliberately small and deliberately not the `sse.js` extension. Three reasons, in the
 * order they were found:
 *
 *  1. `EventSource` cannot send a header, so an authenticated admin stream would need a
 *     cookie-bearing same-origin connection and a query-parameter fallback anyway; the
 *     extension solves a problem Encore will have in milestone 5 and not the one it has
 *     now (a guest's phone on a LAN with nothing to authenticate).
 *  2. The extension reconnects on its own and replays from a `Last-Event-ID` the server
 *     would have to honour. Encore's answer to a gap is a `snapshot` frame and a redraw
 *     (see `/events`), which cannot drift, and the difference between a replayed event and
 *     a stale one is the bug report nobody files.
 *  3. Eleven lines of this file are the whole behaviour; the extension is 200 of theirs.
 *
 * What it does, precisely:
 *
 *  - Opens `/events`. On a `swap:<region>` frame it replaces that region's element with
 *    the HTML the server rendered. It never runs a script from the frame, never parses the
 *    HTML, and never asks the server for a region it was not told about.
 *  - On `snapshot`, it decides whether the appliance is describing something this screen
 *    has already drawn. If it is not, it re-fetches the regions it can see — which is the
 *    reconnect path, and the reason there is no backfill timer on the server.
 *  - On `progress`, it moves the bar and the clock without touching the DOM structure,
 *    because SAPRS 1.8's SSE budget is about a *fact* arriving within a second and a
 *    once-a-second repaint of a whole panel would be a flicker.
 *  - If the stream dies, it waits, with a growing pause, and retries. A phone that walks
 *    out of range should cost nothing until it comes back, and nothing here may make
 *    playback worse for anyone else (SAPRS 11.9's failure isolation).
 */

(() => {
  "use strict";

  const ENDPOINT = "/events";
  const REGIONS = ["player", "alerts"];
  const BACKOFF = [1000, 2000, 5000, 15000, 30000];

  let source = null;
  let attempts = 0;
  let seen_song = null;

  const replace = (region, html) => {
    const target = document.getElementById(region);
    if (!target) return; // this page has no such region; the next one may
    const holder = document.createElement("template");
    holder.innerHTML = html;
    const fresh = holder.content.firstElementChild;
    if (!fresh) return;
    // outerHTML, not innerHTML: the server's fragment carries the element's own
    // attributes (`hx-post`, `aria-label`), and a swap that dropped them would silently
    // stop the region's buttons working.
    target.replaceWith(fresh);
  };

  const refresh = () => {
    for (const region of REGIONS) {
      const target = document.getElementById(region);
      if (!target) continue;
      fetch(`/fragments/${region}`, {
        headers: { "HX-Request": "true" },
        credentials: "same-origin",
      })
        .then((response) => (response.ok ? response.text() : ""))
        .then((html) => replace(region, html))
        .catch(() => {}); // a failed redraw is retried by the next event, or by a reload
    }
  };

  const progress = (data) => {
    const bar = document.querySelector(".progress__bar");
    const times = document.querySelectorAll(".progress__times span");
    if (!bar) return;
    // A frame for a song this screen has not drawn yet is ignored: the swap that names the
    // song arrives on its own event, and moving a bar to a third of "Nothing playing" is
    // the kind of flicker that gets reported as "the queue is broken".
    if (data.song_id !== seen_song) return;
    const fraction = Number(data.fraction);
    const duration = Number(data.duration_ms) || 0;
    const position = Number(data.position_ms) || 0;
    bar.style.width = `${Number.isFinite(fraction) ? Math.min(100, fraction * 100) : 0}%`;
    if (times.length === 2) {
      times[0].textContent = clock(position / 1000);
      times[1].textContent = clock(duration / 1000);
    }
  };

  const clock = (seconds) => {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const minutes = Math.floor(total / 60);
    const remainder = total % 60;
    return `${minutes}:${String(remainder).padStart(2, "0")}`;
  };

  const connect = () => {
    source = new EventSource(ENDPOINT);

    source.onopen = () => {
      attempts = 0;
    };

    source.onerror = () => {
      // `readyState` is CONNECTING while the browser retries by itself, which is the right
      // behaviour for a hiccup. Once it stops retrying, we start again with a pause — the
      // difference between forty phones hammering a rebooting appliance and forty phones
      // that notice when it is back.
      if (source.readyState === EventSource.CLOSED) {
        window.setTimeout(connect, BACKOFF[Math.min(attempts++, BACKOFF.length - 1)]);
      }
      source.close();
    };

    for (const region of REGIONS) {
      source.addEventListener(`swap:${region}`, (message) => replace(region, message.data));
    }

    source.addEventListener("snapshot", (message) => {
      const data = JSON.parse(message.data);
      seen_song = null; // the swap that follows carries the real length
      if (document.hidden) return; // a phone in a pocket will redraw when it wakes
      refresh();
    });

    source.addEventListener("progress", (message) => progress(JSON.parse(message.data)));

    source.addEventListener("resync", () => refresh());

    source.addEventListener("ping", () => {
      /* the transport's own heartbeat; nothing to draw */
    });
  };

  if (typeof EventSource === "undefined") return; // no SSE: HTMX's swaps and reloads still work

  connect();

  // A tab that was frozen to save a battery has a dead socket; the browser does not always
  // report it as an error. Reconnect on wake, and only then ask for what changed.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (!source || source.readyState === EventSource.CLOSED) connect();
    else refresh();
  });
})();
