/* Popup controls capture start/stop/export. No trading actions. */

function setStatus(text) {
  const node = document.getElementById("status");
  if (node) node.textContent = text;
}

function setError(text) {
  const node = document.getElementById("error");
  if (!node) return;
  node.textContent = text || "";
}

function send(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) {
        resolve({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      resolve(response || { ok: false, error: "empty response" });
    });
  });
}

function broadcast(state) {
  chrome.storage.local.set(state);
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs[0];
    if (!tab || !tab.id) return;
    chrome.tabs.sendMessage(tab.id, { type: "CAPTURE_STATE", state });
  });
}

async function refreshStatus() {
  const response = await send({ type: "CAPTURE_STATUS" });
  if (response.storage_error) {
    setError(`Storage error: ${response.storage_error}`);
  } else {
    setError("");
  }
  const session = response.session_id ? ` session ${response.session_id}` : "";
  const counts = ` n=${response.n || 0} chunks=${response.n_chunks || 0} last_seq=${response.last_sequence || "-"}`;
  if (response.capturing) {
    setStatus(`Capturing (read-only).${counts}${session}`);
  } else if (response.status === "failed") {
    setStatus(`Failed.${counts}${session}`);
  } else {
    setStatus(`Stopped.${counts}${session}`);
  }
}

document.getElementById("start").addEventListener("click", () => {
  const intervalMs = Number(document.getElementById("interval").value);
  chrome.storage.local.set({ storageError: null });
  broadcast({ capturing: true, intervalMs });
  setStatus("Capturing (read-only).");
  setTimeout(refreshStatus, 250);
});

document.getElementById("stop").addEventListener("click", () => {
  broadcast({ capturing: false, intervalMs: Number(document.getElementById("interval").value) });
  setStatus("Stopped.");
  setTimeout(refreshStatus, 250);
});

document.getElementById("export").addEventListener("click", async () => {
  const begin = await send({ type: "EXPORT_BEGIN" });
  if (!begin.ok) {
    setError(`Export failed: ${begin.error || "unknown"}`);
    return;
  }
  const parts = [`${JSON.stringify(begin.session_start)}\n`];
  for (let index = 0; index < begin.n_chunks; index += 1) {
    const chunk = await send({
      type: "EXPORT_CHUNK",
      session_id: begin.session_id,
      chunk_index: index,
    });
    if (!chunk.ok) {
      setError(`Export failed at chunk ${index}: ${chunk.error || "unknown"}`);
      return;
    }
    if (chunk.lines && chunk.lines.length) {
      parts.push(`${chunk.lines.join("\n")}\n`);
    }
  }
  parts.push(`${JSON.stringify(begin.session_end)}\n`);
  const blob = new Blob(parts, { type: "application/x-ndjson" });
  const url = URL.createObjectURL(blob);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  chrome.downloads.download(
    {
      url,
      filename: `mexc_ui_capture_${begin.session_id}_${stamp}.ndjson`,
      saveAs: true,
    },
    () => {
      URL.revokeObjectURL(url);
      setStatus(`Export reconstructed ${begin.n_snapshots} snapshots.`);
    }
  );
});

chrome.storage.local.get(["capturing", "intervalMs"], (state) => {
  if (state.intervalMs) document.getElementById("interval").value = String(state.intervalMs);
  refreshStatus();
});
