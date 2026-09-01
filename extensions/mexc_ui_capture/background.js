/* Persist read-only snapshots locally. No trading, no remote upload. */

const MAX_LINES = 20000;
let lines = [];

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({ capturing: false, intervalMs: 500 });
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || !message.type) return;
  if (message.type === "CAPTURE_SNAPSHOT" && message.snapshot) {
    const line = JSON.stringify(message.snapshot);
    lines.push(line);
    if (lines.length > MAX_LINES) lines = lines.slice(-MAX_LINES);
    chrome.storage.session.set({ nLines: lines.length, lastSequence: message.snapshot.sequence });
    sendResponse({ ok: true, n: lines.length });
    return true;
  }
  if (message.type === "EXPORT_CAPTURE") {
    const body = lines.join("\n") + (lines.length ? "\n" : "");
    const url = "data:application/x-ndjson;charset=utf-8," + encodeURIComponent(body);
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    chrome.downloads.download({
      url,
      filename: `mexc_ui_capture_v1_${stamp}.ndjson`,
      saveAs: true,
    });
    sendResponse({ ok: true, n: lines.length });
    return true;
  }
  if (message.type === "CAPTURE_STATUS") {
    sendResponse({ n: lines.length, capturing: false });
    return true;
  }
  return false;
});
