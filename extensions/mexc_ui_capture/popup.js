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

function persistState(state) {
  return new Promise((resolve) => {
    chrome.storage.local.set(state, () => {
      const err = chrome.runtime.lastError && chrome.runtime.lastError.message;
      if (err) {
        resolve({ ok: false, error: err });
        return;
      }
      resolve({ ok: true });
    });
  });
}

function isMissingReceiver(message) {
  return /Receiving end does not exist/i.test(message || "");
}

function missingReceiverOperatorError(tabUrl) {
  const where = tabUrl ? ` This tab is ${tabUrl}.` : "";
  return (
    "No capture script on this tab." +
    where +
    " Open a MEXC futures contract (for example /futures/TAO_USDT or /ru-RU/futures/TAO_USDT), reload the unpacked extension, then Start."
  );
}

function notifyActiveTab(state) {
  // Always consume chrome.runtime.lastError in the sendMessage callback so
  // "Receiving end does not exist" is never an uncaught extension error.
  return new Promise((resolve) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const queryErr = chrome.runtime.lastError && chrome.runtime.lastError.message;
      if (queryErr) {
        resolve({
          ok: false,
          no_receiver: false,
          error: queryErr,
          operator_error: `Could not read the active tab: ${queryErr}`,
        });
        return;
      }
      const tab = tabs && tabs[0];
      if (!tab || tab.id == null) {
        resolve({
          ok: false,
          no_receiver: true,
          error: "no active tab",
          operator_error: missingReceiverOperatorError(null),
        });
        return;
      }
      chrome.tabs.sendMessage(tab.id, { type: "CAPTURE_STATE", state }, (_response) => {
        const err = chrome.runtime.lastError && chrome.runtime.lastError.message;
        if (err) {
          const noReceiver = isMissingReceiver(err);
          resolve({
            ok: false,
            no_receiver: noReceiver,
            error: err,
            operator_error: noReceiver
              ? missingReceiverOperatorError(tab.url || "")
              : `Capture script error: ${err}`,
          });
          return;
        }
        resolve({ ok: true, no_receiver: false });
      });
    });
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
  const counts = ` n=${response.n || 0} chunks=${response.n_chunks || 0} sessions=${response.n_sessions || 0} last_seq=${response.last_sequence || "-"}`;
  if (response.capturing) {
    setStatus(`Capturing (read-only).${counts}${session}`);
  } else if (response.status === "failed") {
    setStatus(`Failed.${counts}${session}`);
  } else {
    setStatus(`Stopped.${counts}${session}`);
  }
}

document.getElementById("start").addEventListener("click", async () => {
  const intervalMs = Number(document.getElementById("interval").value);
  setError("");
  const persist = await persistState({ storageError: null, capturing: true, intervalMs });
  if (!persist.ok) {
    setError(`Could not persist Start: ${persist.error}`);
    setStatus("Stopped.");
    return;
  }
  const notified = await notifyActiveTab({ capturing: true, intervalMs });
  if (!notified.ok) {
    // Start must not leave capturing=true when the content script is absent.
    await persistState({ capturing: false });
    setError(notified.operator_error);
    setStatus("Stopped.");
    return;
  }
  setStatus("Capturing (read-only).");
  setTimeout(refreshStatus, 250);
});

document.getElementById("stop").addEventListener("click", async () => {
  const intervalMs = Number(document.getElementById("interval").value);
  const persist = await persistState({ capturing: false, intervalMs });
  if (!persist.ok) {
    setError(`Could not persist Stop: ${persist.error}`);
    return;
  }
  const notified = await notifyActiveTab({ capturing: false, intervalMs });
  // Stop is local-first: a missing receiver is not an operator failure.
  if (!notified.ok && !notified.no_receiver) {
    setError(`Stop persisted, but the page was not notified: ${notified.error}`);
  } else {
    setError("");
  }
  setStatus("Stopped.");
  setTimeout(refreshStatus, 250);
});

function downloadNdjson(filename, parts, statusText) {
  // Popup Blob reconstruction: never a data-URL. All-session export keeps
  // stop/start boundaries so a later session cannot hide the earlier one.
  const blob = new Blob(parts, { type: "application/x-ndjson" });
  const url = URL.createObjectURL(blob);
  chrome.downloads.download(
    {
      url,
      filename,
      saveAs: true,
    },
    () => {
      URL.revokeObjectURL(url);
      setStatus(statusText);
    }
  );
}

async function appendSessionParts(parts, session) {
  parts.push(`${JSON.stringify(session.session_start)}\n`);
  for (let index = 0; index < session.n_chunks; index += 1) {
    const chunk = await send({
      type: "EXPORT_CHUNK",
      session_id: session.session_id,
      chunk_index: index,
    });
    if (!chunk.ok) {
      setError(`Export failed at ${session.session_id} chunk ${index}: ${chunk.error || "unknown"}`);
      return false;
    }
    if (chunk.lines && chunk.lines.length) {
      parts.push(`${chunk.lines.join("\n")}\n`);
    }
  }
  parts.push(`${JSON.stringify(session.session_end)}\n`);
  return true;
}

document.getElementById("export").addEventListener("click", async () => {
  const begin = await send({ type: "EXPORT_BEGIN_ALL" });
  if (!begin.ok) {
    setError(`Export failed: ${begin.error || "unknown"}`);
    return;
  }
  const parts = [];
  let nSnapshots = 0;
  for (const session of begin.sessions || []) {
    nSnapshots += Number(session.n_snapshots || 0);
    const ok = await appendSessionParts(parts, session);
    if (!ok) return;
  }
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  downloadNdjson(
    `mexc_ui_capture_sessions_${stamp}.ndjson`,
    parts,
    `Export reconstructed ${begin.n_sessions || 0} sessions / ${nSnapshots} snapshots.`
  );
});

document.getElementById("export-last").addEventListener("click", async () => {
  const begin = await send({ type: "EXPORT_BEGIN" });
  if (!begin.ok) {
    setError(`Export failed: ${begin.error || "unknown"}`);
    return;
  }
  const parts = [];
  const ok = await appendSessionParts(parts, begin);
  if (!ok) return;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  downloadNdjson(
    `mexc_ui_capture_${begin.session_id}_${stamp}.ndjson`,
    parts,
    `Export reconstructed ${begin.n_snapshots} snapshots from last session.`
  );
});

chrome.storage.local.get(["capturing", "intervalMs"], (state) => {
  if (state.intervalMs) document.getElementById("interval").value = String(state.intervalMs);
  refreshStatus();
});
