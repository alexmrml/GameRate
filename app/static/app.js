(() => {
  if (!window.GameRateActivity || !window.EventSource) return;

  const liveState = document.querySelector("#live-state");
  const source = new EventSource("/activity/events");

  source.addEventListener("open", () => {
    liveState.textContent = "Live";
    liveState.classList.add("is-live");
  });

  source.addEventListener("error", () => {
    liveState.textContent = "Reconnecting…";
    liveState.classList.remove("is-live");
  });

  const knownRuns = new Set(
    Array.from(document.querySelectorAll("[data-run-id]"), (row) => row.dataset.runId),
  );

  source.addEventListener("activity", (event) => {
    const snapshot = JSON.parse(event.data);
    for (const run of snapshot.runs) {
      const row = document.querySelector(`[data-run-id="${run.id}"]`);
      if (!row) {
        // A run queued elsewhere (scheduled crawl) needs the server-rendered table.
        if (!knownRuns.has(run.id)) window.location.reload();
        continue;
      }
      const status = row.querySelector('[data-field="status"]');
      status.textContent = run.status;
      status.className = `status status-${run.status}`;
      const progress =
        run.progress_total > 0
          ? `${run.progress_current} / ${run.progress_total}`
          : String(run.progress_current ?? 0);
      row.querySelector('[data-field="progress"]').textContent = progress;
      row.querySelector('[data-field="current_game"]').textContent = run.current_game || "—";
      row.querySelector('[data-field="worker_id"]').textContent = run.worker_id || "—";
      row.querySelector('[data-field="message"]').textContent = run.message || "—";
    }
  });
})();

