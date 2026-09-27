const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const repo = path.resolve(__dirname, "..", "..");
const backgroundPath = path.join(repo, "extensions", "mexc_ui_capture", "background.js");
const source = fs.readFileSync(backgroundPath, "utf8") +
  "\nglobalThis.__handleMessage = handleMessage;";

const exported = [];
const forwarded = [];
const captureId = "capture-committed";
let sequence = 0;

const counters = () => ({ manual: 0, interval: 0, mutation: 0 });
const diagnostics = {
  EXTENSION_VERSION: "1.3.7",
  FORMAT_VERSION: 1,
  CHECKPOINT_MIN_GAP_MS: 60000,
  clipId: (value) => String(value),
  newSidecar: () => ({
    counters: {
      received: counters(),
      append_started: counters(),
      snapshots_persisted: counters(),
      failed: counters(),
      session_id_mismatch_detected: 0,
      stale_generation_detected: 0,
    },
    histograms: { background_queue_wait_ms: {}, append_duration_ms: {} },
    background_queue_depth_high_water: 0,
  }),
  noteWorkerBoot: () => {},
  recordLifecycle: () => {},
  recordCompletion: () => {},
  recordStage: () => {},
  noteProducer: () => {},
  observeHistogram: () => {},
  mergeCounters: () => {},
  sanitizeStage: (value) => ({ ...value }),
  queueWaitMs: (end, start) => Math.max(0, end - start),
  summary: () => ({}),
};

const durable = {
  appendSnapshot: async (snapshot) => {
    const committed = { ...snapshot, sequence: ++sequence, capture_id: captureId };
    exported.push(committed);
    return {
      committed,
      meta: { n_snapshots: sequence, session_id: captureId },
      append_timings: {},
      payload_bytes: JSON.stringify(committed).length,
      chunk_index: 0,
      chunk_occupancy: sequence,
      session_id_mismatch: false,
    };
  },
};

const context = {
  AbortController,
  Date,
  JSON,
  Math,
  Promise,
  clearTimeout,
  console,
  crypto,
  fetch: async (_url, options) => {
    forwarded.push(JSON.parse(options.body));
    return { ok: true, status: 200 };
  },
  globalThis: null,
  importScripts: () => {},
  performance,
  setTimeout,
  MexcDurable: durable,
  MexcStageDiagnostics: diagnostics,
  chrome: {
    runtime: {
      onInstalled: { addListener: () => {} },
      onMessage: { addListener: () => {} },
    },
    storage: { local: { set: async () => {}, get: async () => ({}) } },
    tabs: { query: async () => [], sendMessage: async () => {} },
  },
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: backgroundPath });

async function main() {
  for (let ordinal = 1; ordinal <= 2; ordinal += 1) {
    const response = await context.__handleMessage({
      type: "CAPTURE_SNAPSHOT",
      snapshot: {
        schema: "mexc_ui_raw_snapshot",
        schema_version: 1,
        sequence: 0,
        trigger: "interval",
        stage_diagnostics: { trigger: "interval", request_ordinal: ordinal },
      },
    });
    assert.equal(response.ok, true);
  }

  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(forwarded.map((row) => row.sequence), [1, 2]);
  assert.deepEqual(forwarded.map((row) => row.capture_id), [captureId, captureId]);
  assert.deepEqual(forwarded, exported);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
