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

  source.addEventListener("activity", (event) => {
    const snapshot = JSON.parse(event.data);
    for (const run of snapshot.runs) {
      const row = document.querySelector(`[data-run-id="${run.id}"]`);
      if (!row) continue;
      const status = row.querySelector('[data-field="status"]');
      status.textContent = run.status;
      status.className = `status status-${run.status}`;
      row.querySelector('[data-field="worker_id"]').textContent = run.worker_id || "—";
      row.querySelector('[data-field="message"]').textContent = run.message || "—";
    }
  });
})();

