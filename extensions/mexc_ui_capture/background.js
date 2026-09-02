/* Persist read-only snapshots in IndexedDB. No trading, no remote upload. */

importScripts("durable.js");

let appendChain = Promise.resolve();
let closedError = null;

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({ capturing: false, intervalMs: 500, storageError: null });
});

function storageErrorText(error) {
  if (!error) return "unknown storage error";
  return String(error && error.message ? error.message : error);
}

async function failClosed(error) {
  closedError = error;
  const message = storageErrorText(error);
  try {
    await MexcDurable.markFailed(message, new Date().toISOString());
  } catch (_ignored) {
    // Still surface the original error to the operator.
  }
  await chrome.storage.local.set({ capturing: false, storageError: message });
  const tabs = await chrome.tabs.query({
    url: ["https://www.mexc.com/futures/*", "https://futures.mexc.com/*"],
  });
  for (const tab of tabs) {
    if (!tab.id) continue;
    try {
      await chrome.tabs.sendMessage(tab.id, {
        type: "CAPTURE_STATE",
        state: { capturing: false, intervalMs: 500, storageError: message },
      });
    } catch (_ignored) {
      // Tab may not have the content script.
    }
  }
}

function enqueue(task) {
  const run = appendChain.then(task, task);
  appendChain = run.then(
    () => undefined,
    () => undefined
  );
  return run;
}

async function handleMessage(message) {
  if (!message || !message.type) return { ok: false, error: "missing message type" };
  if (message.type === "START_SESSION") {
    closedError = null;
    await chrome.storage.local.set({ storageError: null });
    const meta = await MexcDurable.startSession({
      started_at: new Date().toISOString(),
      interval_ms: Number(message.intervalMs || 500),
      page_host: message.page_host || null,
      page_path: message.page_path || null,
    });
    return {
      ok: true,
      session_id: meta.session_id,
      next_sequence: 1,
      chunk_size: meta.chunk_size,
    };
  }
  if (message.type === "STOP_SESSION") {
    const meta = await MexcDurable.stopSession(new Date().toISOString(), "stopped");
    return { ok: true, session_id: meta && meta.session_id, n: meta && meta.n_snapshots };
  }
  if (message.type === "CAPTURE_SNAPSHOT" && message.snapshot) {
    if (closedError) {
      return { ok: false, error: storageErrorText(closedError) };
    }
    try {
      const result = await enqueue(async () => {
        if (closedError) throw closedError;
        return MexcDurable.appendSnapshot(message.snapshot);
      });
      const meta = result.meta;
      return {
        ok: true,
        n: meta.n_snapshots,
        sequence: result.committed.sequence,
        session_id: meta.session_id,
      };
    } catch (error) {
      await failClosed(error);
      return { ok: false, error: storageErrorText(error) };
    }
  }
  if (message.type === "EXPORT_BEGIN") {
    try {
      const exported = await MexcDurable.exportMeta(message.session_id || null);
      return { ok: true, ...exported };
    } catch (error) {
      return { ok: false, error: storageErrorText(error) };
    }
  }
  if (message.type === "EXPORT_BEGIN_ALL") {
    try {
      const exported = await MexcDurable.exportMetaAll();
      return { ok: true, ...exported };
    } catch (error) {
      return { ok: false, error: storageErrorText(error) };
    }
  }
  if (message.type === "EXPORT_CHUNK") {
    try {
      const lines = await MexcDurable.exportChunk(message.session_id, Number(message.chunk_index));
      return { ok: true, lines };
    } catch (error) {
      return { ok: false, error: storageErrorText(error) };
    }
  }
  if (message.type === "CAPTURE_STATUS") {
    const capturing = await chrome.storage.local.get(["capturing", "storageError", "intervalMs"]);
    const activeId = await MexcDurable.getMeta("active_session_id");
    const lastId = await MexcDurable.getMeta("last_session_id");
    const sessionId = activeId || lastId;
    const meta = sessionId ? await MexcDurable.getSession(sessionId) : null;
    const sessions = await MexcDurable.listSessions();
    return {
      ok: true,
      capturing: Boolean(capturing.capturing),
      storage_error: capturing.storageError || (meta && meta.storage_error) || null,
      interval_ms: capturing.intervalMs || 500,
      session_id: sessionId,
      n: meta ? meta.n_snapshots : 0,
      n_chunks: meta ? meta.n_chunks : 0,
      n_sessions: sessions.length,
      last_sequence: meta ? meta.last_sequence : null,
      status: meta ? meta.status : "idle",
    };
  }
  return { ok: false, error: "unknown message type" };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  handleMessage(message)
    .then(sendResponse)
    .catch(async (error) => {
      await failClosed(error);
      sendResponse({ ok: false, error: storageErrorText(error) });
    });
  return true;
});
