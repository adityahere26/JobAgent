// jobagent dashboard. Progressive enhancement only: every action also works without this file.
// All text from the server is written with textContent, never as HTML.
"use strict";

(function () {
  const csrf = document.querySelector('meta[name="csrf-token"]').content;

  // Copy buttons: <button data-copy="textarea-id">
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.dataset.copy;
      const field = document.getElementById(id);
      const status = document.querySelector(`[data-copy-status="${id}"]`);
      try {
        await navigator.clipboard.writeText(field.value);
        status.textContent = "Copied.";
      } catch {
        field.focus();
        field.select();
        status.textContent = "Selected. Press Ctrl+C or Cmd+C to copy.";
      }
    });
  });

  // Refresh jobs with live progress.
  const form = document.getElementById("refresh-form");
  const panel = document.getElementById("refresh-panel");
  const text = document.getElementById("refresh-text");
  const bar = document.getElementById("refresh-progress");
  const log = document.getElementById("refresh-log");
  const reload = document.getElementById("refresh-reload");
  const button = form.querySelector("button");

  function show(p) {
    panel.hidden = false;
    bar.max = Math.max(p.total, 1);
    bar.value = p.done;
    log.replaceChildren(
      ...p.lines.map((line) => {
        const li = document.createElement("li");
        li.textContent = line;
        return li;
      })
    );
    if (p.running) {
      text.textContent = p.total
        ? `Fetching boards: ${p.done} of ${p.total} done, ${p.new} new jobs so far.`
        : "Starting…";
    } else if (p.finished) {
      if (p.error) {
        text.textContent = `Refresh stopped: ${p.error}`;
      } else {
        const failed = p.failed ? ` ${p.failed} board(s) failed; their jobs were kept.` : "";
        text.textContent = `Done. ${p.new} new jobs from ${p.total} boards.${failed}`;
      }
      reload.hidden = false;
    }
    button.disabled = p.running;
  }

  async function poll() {
    const res = await fetch("/refresh/status", { headers: { Accept: "application/json" } });
    const p = await res.json();
    show(p);
    if (p.running) setTimeout(poll, 1000);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    try {
      const res = await fetch("/refresh", {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRF-Token": csrf,
        },
        body: "",
      });
      if (!res.ok) throw new Error(String(res.status));
      show(await res.json());
      poll();
    } catch {
      panel.hidden = false;
      text.textContent = "Could not start the refresh. Reload the page and try again.";
      button.disabled = false;
    }
  });

  // A refresh started on another page (or before a reload) keeps showing its progress.
  fetch("/refresh/status", { headers: { Accept: "application/json" } })
    .then((res) => res.json())
    .then((p) => {
      if (p.running) poll();
    })
    .catch(() => {});

  // After a failed add, move focus to the error summary.
  const errors = document.getElementById("form-errors");
  if (errors) errors.focus();
})();
