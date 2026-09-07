/* Persist read-only snapshots in IndexedDB. No trading, no remote upload. */

importScripts("stage_diagnostics.js");
importScripts("durable.js");

const D = globalThis.MexcStageDiagnostics;
const workerBootId = D.clipId(
  (globalThis.crypto && crypto.randomUUID && crypto.randomUUID()) || `boot-${Date.now()}`
);
const workerTimeOrigin = performance.timeOrigin;
let appendChain = Promise.resolve();
let closedError = null;
let sidecar = D.newSidecar();
let bgQueued = 0;
let bgActive = false;
let lastCheckpointMono = 0;

D.noteWorkerBoot(sidecar, workerBootId);
D.recordLifecycle(sidecar, {
  kind: "worker_boot",
  mono: performance.now(),
  worker_boot_id: workerBootId,
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({ capturing: false, intervalMs: 500, storageError: null });
});

function storageErrorText(error) {
  if (!error) return "unknown storage error";
  return String(error && error.message ? error.message : error);
}

function maybeCheckpoint() {
  // Fire-and-forget on an already-awake worker. Do not await and do not start
  // a keepalive interval: that would change service-worker lifetime.
  const now = performance.now();
  if (now - lastCheckpointMono < D.CHECKPOINT_MIN_GAP_MS) return;
  lastCheckpointMono = now;
  try {
    chrome.storage.local.set({ stageDiagnosticsCheckpoint: D.summary(sidecar) });
  } catch (_ignored) {
    // Diagnostic checkpoint must never fail the observation path.
  }
}

function applyContentDelta(delta) {
  if (!delta || typeof delta !== "object") return;
  if (delta.counters) D.mergeCounters(sidecar.counters, delta.counters);
  if (Array.isArray(delta.lifecycle)) {
    for (const event of delta.lifecycle) D.recordLifecycle(sidecar, event);
  }
  if (delta.ack_previous) {
    D.recordCompletion(sidecar, delta.ack_previous);
    if (typeof delta.ack_previous.total_ack_latency_ms === "number") {
      D.observeHistogram(
        sidecar.histograms.total_ack_latency_ms,
        delta.ack_previous.total_ack_latency_ms
      );
    }
  }
  D.noteProducer(sidecar, delta.producer_epoch);
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
  // Same futures scope as content_scripts: locale paths such as /ru-RU/futures/*.
  const tabs = await chrome.tabs.query({
    url: [
      "https://www.mexc.com/futures/*",
      "https://www.mexc.com/*/futures/*",
      "https://futures.mexc.com/*",
    ],
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
    sidecar = D.newSidecar();
    D.noteWorkerBoot(sidecar, workerBootId);
    D.recordLifecycle(sidecar, {
      kind: "worker_boot",
      mono: performance.now(),
      worker_boot_id: workerBootId,
    });
    D.noteProducer(sidecar, message.producer_epoch);
    D.recordLifecycle(sidecar, {
      kind: "session_start",
      mono: performance.now(),
      producer_epoch: message.producer_epoch,
      session_generation: message.session_generation,
      worker_boot_id: workerBootId,
    });
    await chrome.storage.local.set({ storageError: null });
    const meta = await MexcDurable.startSession({
      started_at: new Date().toISOString(),
      interval_ms: Number(message.intervalMs || 500),
      page_host: message.page_host || null,
      page_path: message.page_path || null,
      producer_epoch: message.producer_epoch || null,
      session_generation: message.session_generation == null ? null : message.session_generation,
      worker_boot_id: workerBootId,
      content_time_origin: message.content_time_origin == null ? null : message.content_time_origin,
      worker_time_origin: workerTimeOrigin,
      extension_version: D.EXTENSION_VERSION,
      diagnostic_format_version: D.FORMAT_VERSION,
    });
    return {
      ok: true,
      session_id: meta.session_id,
      next_sequence: 1,
      chunk_size: meta.chunk_size,
      worker_boot_id: workerBootId,
    };
  }
  if (message.type === "STOP_SESSION") {
    applyContentDelta(message.stage_content_delta);
    const summary = D.summary(sidecar);
    const meta = await MexcDurable.stopSession(new Date().toISOString(), "stopped", summary);
    maybeCheckpoint();
    return { ok: true, session_id: meta && meta.session_id, n: meta && meta.n_snapshots };
  }
  if (message.type === "DIAGNOSTIC_FLUSH") {
    applyContentDelta(message.stage_content_delta);
    maybeCheckpoint();
    return { ok: true, worker_boot_id: workerBootId };
  }
  if (message.type === "CAPTURE_SNAPSHOT" && message.snapshot) {
    if (closedError) {
      return { ok: false, error: storageErrorText(closedError) };
    }
    const receiveMono = performance.now();
    const snapshot = message.snapshot;
    const waitDepth = bgQueued;
    applyContentDelta(message.stage_content_delta);
    const incoming = snapshot.stage_diagnostics || {};
    const trigger = incoming.trigger || snapshot.trigger || "manual";
    sidecar.counters.received[trigger] = (sidecar.counters.received[trigger] || 0) + 1;
    D.noteProducer(sidecar, incoming.producer_epoch || message.producer_epoch);
    if (incoming.stale_generation) sidecar.counters.stale_generation_detected += 1;
    bgQueued += 1;
    sidecar.background_queue_depth_high_water = Math.max(
      sidecar.background_queue_depth_high_water,
      waitDepth
    );
    try {
      const result = await enqueue(async () => {
        if (closedError) throw closedError;
        bgQueued = Math.max(0, bgQueued - 1);
        bgActive = true;
        const appendStart = performance.now();
        sidecar.counters.append_started[trigger] = (sidecar.counters.append_started[trigger] || 0) + 1;
        const merged = Object.assign({}, incoming, {
          worker_boot_id: workerBootId,
          background_receive_mono: receiveMono,
          background_queue_depth: waitDepth,
          background_queue_wait_ms: D.queueWaitMs(appendStart, receiveMono),
          append_start_mono: appendStart,
        });
        snapshot.stage_diagnostics = D.sanitizeStage(merged);
        try {
          return await MexcDurable.appendSnapshot(snapshot);
        } finally {
          bgActive = false;
        }
      });
      const appendEnd = performance.now();
      const meta = result.meta;
      const appendStart = snapshot.stage_diagnostics && snapshot.stage_diagnostics.append_start_mono;
      const appendDuration = D.queueWaitMs(appendEnd, appendStart);
      sidecar.counters.snapshots_persisted[trigger] =
        (sidecar.counters.snapshots_persisted[trigger] || 0) + 1;
      if (result.session_id_mismatch) sidecar.counters.session_id_mismatch_detected += 1;
      if (snapshot.stage_diagnostics) {
        snapshot.stage_diagnostics.payload_bytes = result.payload_bytes;
        snapshot.stage_diagnostics.chunk_index = result.chunk_index;
        snapshot.stage_diagnostics.chunk_occupancy = result.chunk_occupancy;
        snapshot.stage_diagnostics.append_timings = result.append_timings;
      }
      D.observeHistogram(sidecar.histograms.background_queue_wait_ms, D.queueWaitMs(appendStart, receiveMono));
      D.observeHistogram(sidecar.histograms.append_duration_ms, appendDuration);
      D.recordStage(sidecar, snapshot.stage_diagnostics, {
        mutation_callback_ordinal: incoming.mutation_callback_ordinal,
      });
      D.recordCompletion(sidecar, {
        request_ordinal: incoming.request_ordinal,
        trigger,
        append_end_mono: appendEnd,
        append_duration_ms: appendDuration,
        ack_outcome: "ok",
        worker_boot_id: workerBootId,
        persisted_sequence: result.committed && result.committed.sequence,
      });
      maybeCheckpoint();
      return {
        ok: true,
        n: meta.n_snapshots,
        sequence: result.committed.sequence,
        session_id: meta.session_id,
        stage: {
          worker_boot_id: workerBootId,
          background_receive_mono: receiveMono,
          background_queue_wait_ms: D.queueWaitMs(appendStart, receiveMono),
          append_start_mono: appendStart,
          append_end_mono: appendEnd,
          append_duration_ms: appendDuration,
          append_timings: result.append_timings,
          payload_bytes: result.payload_bytes,
          chunk_index: result.chunk_index,
          chunk_occupancy: result.chunk_occupancy,
          ack_outcome: "ok",
        },
      };
    } catch (error) {
      sidecar.counters.failed[trigger] = (sidecar.counters.failed[trigger] || 0) + 1;
      await failClosed(error);
      return { ok: false, error: storageErrorText(error), stage: { ack_outcome: "fail", worker_boot_id: workerBootId } };
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
      worker_boot_id: workerBootId,
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
