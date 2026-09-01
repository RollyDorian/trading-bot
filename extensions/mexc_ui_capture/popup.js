/* Popup controls capture start/stop/export. No trading actions. */

function setStatus(text) {
  const node = document.getElementById("status");
  if (node) node.textContent = text;
}

function broadcast(state) {
  chrome.storage.local.set(state);
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs[0];
    if (!tab || !tab.id) return;
    chrome.tabs.sendMessage(tab.id, { type: "CAPTURE_STATE", state });
  });
}

document.getElementById("start").addEventListener("click", () => {
  const intervalMs = Number(document.getElementById("interval").value);
  broadcast({ capturing: true, intervalMs });
  setStatus("Capturing (read-only).");
});

document.getElementById("stop").addEventListener("click", () => {
  broadcast({ capturing: false, intervalMs: Number(document.getElementById("interval").value) });
  setStatus("Stopped.");
});

document.getElementById("export").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "EXPORT_CAPTURE" }, (response) => {
    setStatus(`Export requested (${response && response.n ? response.n : 0} lines).`);
  });
});

chrome.storage.local.get(["capturing", "intervalMs"], (state) => {
  if (state.intervalMs) document.getElementById("interval").value = String(state.intervalMs);
  setStatus(state.capturing ? "Capturing (read-only)." : "Stopped.");
});
