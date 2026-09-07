/* Bounded stage diagnostics. Numeric/enum only. No market prices, no private DOM. */

globalThis.MexcStageDiagnostics = (function stageDiagnostics() {
  const FORMAT_VERSION = 1;
  const EXTENSION_VERSION = "1.3.5";
  const ID_MAX = 64;
  const ENUM_MAX = 32;
  const INTERVAL_DETAIL_CAP = 2048;
  const MUTATION_DETAIL_CAP = 512;
  const MUTATION_DETAIL_FIRST = 128;
  const MUTATION_DETAIL_EVERY = 16;
  const SLOW_EXAMPLE_CAP = 256;
  const LIFECYCLE_CAP = 128;
  const SLOW_STAGE_MS = 500;
  const CHECKPOINT_MIN_GAP_MS = 5000;
  const HISTOGRAM_BOUNDS_MS = [
    1, 2, 4, 8, 16, 32, 64, 128, 250, 500, 1000, 2000, 4000, 8000, 16000, 32000,
  ];
  const ALLOWED_TRIGGERS = { interval: true, mutation: true, manual: true };
  const ALLOWED_VISIBILITY = {
    visible: true,
    hidden: true,
    prerender: true,
    unloaded: true,
    unknown: true,
  };
  const ALLOWED_ACK = {
    ok: true,
    fail: true,
    timeout: true,
    unknown: true,
    abandoned: true,
  };
  const PRIVATE_KEY =
    /account|balance|wallet|position|\borders?\b|html|inner_?text|text_content|raw_text|selector|cookie|credential|password|email|\buid\b|token|authorization/i;
  const ALLOWED_STAGE_KEYS = {
    diagnostic_format_version: true,
    extension_version: true,
    request_ordinal: true,
    trigger: true,
    producer_epoch: true,
    session_generation: true,
    session_generation_now: true,
    session_id: true,
    stale_generation: true,
    session_id_mismatch: true,
    active_session_id: true,
    worker_boot_id: true,
    interval_callback_ordinal: true,
    mutation_callback_ordinal: true,
    expected_deadline_mono: true,
    elapsed_ideal_slot_ordinal: true,
    callback_mono: true,
    callback_delay_ms: true,
    content_enqueue_mono: true,
    content_queue_depth: true,
    content_oldest_wait_ms: true,
    content_queue_wait_ms: true,
    extract_start_mono: true,
    extract_end_mono: true,
    extract_duration_ms: true,
    send_start_mono: true,
    ack_end_mono: true,
    total_ack_latency_ms: true,
    visibility_state: true,
    visibility_state_at_extract: true,
    background_receive_mono: true,
    background_queue_depth: true,
    background_queue_wait_ms: true,
    append_start_mono: true,
    append_end_mono: true,
    append_duration_ms: true,
    payload_bytes: true,
    chunk_index: true,
    chunk_occupancy: true,
    ack_outcome: true,
    append_timings: true,
    dirty_since_last_interval: true,
  };
  const ALLOWED_APPEND_TIMING_KEYS = {
    meta_lookup_ms: true,
    db_open_ms: true,
    session_read_ms: true,
    chunk_read_ms: true,
    stringify_put_ms: true,
    tx_wait_ms: true,
    close_ms: true,
  };
  const INT_KEYS = {
    request_ordinal: true,
    session_generation: true,
    session_generation_now: true,
    interval_callback_ordinal: true,
    elapsed_ideal_slot_ordinal: true,
    mutation_callback_ordinal: true,
    content_queue_depth: true,
    background_queue_depth: true,
    payload_bytes: true,
    chunk_index: true,
    chunk_occupancy: true,
  };

  function clipId(value) {
    if (value == null) return null;
    const text = String(value).trim();
    if (!text) return null;
    return text.slice(0, ID_MAX);
  }

  function clipEnum(value, allowed, fallback) {
    const text = clipId(value);
    const key = text ? text.slice(0, ENUM_MAX) : fallback;
    return allowed[key] ? key : fallback;
  }

  function finiteNumber(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
    return value;
  }

  function emptyTriggerCounts() {
    return { interval: 0, mutation: 0, manual: 0 };
  }

  function emptyCounters() {
    return {
      expected_interval_opportunities: 0,
      timer_callbacks: 0,
      mutation_callbacks: 0,
      mutation_dirty_sets: 0,
      manual_callbacks: 0,
      callbacks_enqueued: emptyTriggerCounts(),
      tasks_started: emptyTriggerCounts(),
      extracted: emptyTriggerCounts(),
      sent: emptyTriggerCounts(),
      received: emptyTriggerCounts(),
      append_started: emptyTriggerCounts(),
      snapshots_persisted: emptyTriggerCounts(),
      acked: emptyTriggerCounts(),
      failed: emptyTriggerCounts(),
      abandoned: emptyTriggerCounts(),
      stale_generation_detected: 0,
      session_id_mismatch_detected: 0,
      producer_epochs_seen: 0,
      outstanding_at_stop: 0,
      diagnostic_truncated: false,
      suppressed_interval_details: 0,
      suppressed_mutation_details: 0,
      suppressed_lifecycle: 0,
      suppressed_slow_examples: 0,
      suppressed_completions: 0,
    };
  }

  function emptyHistogram() {
    return {
      bounds_ms: HISTOGRAM_BOUNDS_MS.slice(),
      counts: new Array(HISTOGRAM_BOUNDS_MS.length + 1).fill(0),
      min_ms: null,
      max_ms: null,
      sum_ms: 0,
      n: 0,
    };
  }

  function observeHistogram(hist, value) {
    const number = finiteNumber(value);
    if (number == null || !hist) return;
    hist.n += 1;
    hist.sum_ms += number;
    hist.min_ms = hist.min_ms == null ? number : Math.min(hist.min_ms, number);
    hist.max_ms = hist.max_ms == null ? number : Math.max(hist.max_ms, number);
    let bucket = HISTOGRAM_BOUNDS_MS.length;
    for (let index = 0; index < HISTOGRAM_BOUNDS_MS.length; index += 1) {
      if (number <= HISTOGRAM_BOUNDS_MS[index]) {
        bucket = index;
        break;
      }
    }
    hist.counts[bucket] += 1;
  }

  function expectedDeadlineMono(timerRegisteredMono, intervalCallbackOrdinal, intervalMs) {
    // Ideal deadline t0 + n*interval from timer registration, never from a late callback.
    return timerRegisteredMono + intervalCallbackOrdinal * intervalMs;
  }

  function elapsedIdealSlotOrdinal(timerRegisteredMono, callbackMono, intervalMs) {
    if (!(intervalMs > 0)) return 0;
    return Math.floor((callbackMono - timerRegisteredMono) / intervalMs);
  }

  function expectedIntervalOpportunities(timerRegisteredMono, stopMono, intervalMs) {
    if (!(intervalMs > 0) || timerRegisteredMono == null || stopMono == null) return 0;
    return Math.max(0, Math.floor((stopMono - timerRegisteredMono) / intervalMs));
  }

  function queueWaitMs(startMono, callbackMono) {
    if (startMono == null || callbackMono == null) return null;
    return startMono - callbackMono;
  }

  function shouldSampleMutation(ordinal) {
    if (ordinal <= MUTATION_DETAIL_FIRST) return true;
    return ordinal % MUTATION_DETAIL_EVERY === 0;
  }

  function sanitizeAppendTimings(raw) {
    if (!raw || typeof raw !== "object") return null;
    const out = {};
    for (const [key, value] of Object.entries(raw)) {
      if (!ALLOWED_APPEND_TIMING_KEYS[key] || PRIVATE_KEY.test(key)) continue;
      const number = finiteNumber(value);
      if (number == null) continue;
      out[key] = number;
    }
    return Object.keys(out).length ? out : null;
  }

  function sanitizeStage(raw) {
    if (!raw || typeof raw !== "object") return null;
    const out = {};
    for (const [key, value] of Object.entries(raw)) {
      if (!ALLOWED_STAGE_KEYS[key] || PRIVATE_KEY.test(key)) continue;
      if (
        key === "producer_epoch" ||
        key === "session_id" ||
        key === "active_session_id" ||
        key === "worker_boot_id" ||
        key === "extension_version"
      ) {
        const clipped = clipId(value);
        if (clipped) out[key] = clipped;
        continue;
      }
      if (key === "trigger") {
        out[key] = clipEnum(value, ALLOWED_TRIGGERS, "manual");
        continue;
      }
      if (key === "visibility_state" || key === "visibility_state_at_extract") {
        out[key] = clipEnum(value, ALLOWED_VISIBILITY, "unknown");
        continue;
      }
      if (key === "ack_outcome") {
        out[key] = clipEnum(value, ALLOWED_ACK, "unknown");
        continue;
      }
      if (
        key === "stale_generation" ||
        key === "session_id_mismatch" ||
        key === "dirty_since_last_interval"
      ) {
        out[key] = Boolean(value);
        continue;
      }
      if (key === "append_timings") {
        const timings = sanitizeAppendTimings(value);
        if (timings) out[key] = timings;
        continue;
      }
      const number = finiteNumber(typeof value === "number" ? value : Number(value));
      if (number == null) continue;
      out[key] = INT_KEYS[key] ? Math.trunc(number) : number;
    }
    if (!Object.keys(out).length) return null;
    if (out.diagnostic_format_version == null) out.diagnostic_format_version = FORMAT_VERSION;
    return out;
  }

  function newSidecar() {
    return {
      interval_details: [],
      mutation_details: [],
      slow_examples: [],
      lifecycle: [],
      completions: [],
      histograms: {
        callback_delay_ms: emptyHistogram(),
        content_queue_wait_ms: emptyHistogram(),
        extract_duration_ms: emptyHistogram(),
        background_queue_wait_ms: emptyHistogram(),
        append_duration_ms: emptyHistogram(),
        total_ack_latency_ms: emptyHistogram(),
      },
      counters: emptyCounters(),
      producer_epochs: [],
      worker_boot_ids: [],
      content_queue_depth_high_water: 0,
      background_queue_depth_high_water: 0,
      content_oldest_wait_high_water_ms: 0,
    };
  }

  function noteProducer(sidecar, epoch) {
    const clipped = clipId(epoch);
    if (!clipped) return;
    if (sidecar.producer_epochs.indexOf(clipped) === -1) {
      sidecar.producer_epochs.push(clipped);
      sidecar.counters.producer_epochs_seen = sidecar.producer_epochs.length;
    }
  }

  function noteWorkerBoot(sidecar, bootId) {
    const clipped = clipId(bootId);
    if (!clipped) return;
    if (sidecar.worker_boot_ids.indexOf(clipped) === -1) {
      sidecar.worker_boot_ids.push(clipped);
    }
  }

  function recordLifecycle(sidecar, event) {
    const kind = event && event.kind;
    if (!kind) return;
    if (sidecar.lifecycle.length >= LIFECYCLE_CAP) {
      sidecar.counters.suppressed_lifecycle += 1;
      sidecar.counters.diagnostic_truncated = true;
      return;
    }
    const row = {
      kind: String(kind).slice(0, ENUM_MAX),
      mono: finiteNumber(event.mono),
    };
    if (event.from_state) row.from_state = clipEnum(event.from_state, ALLOWED_VISIBILITY, "unknown");
    if (event.to_state) row.to_state = clipEnum(event.to_state, ALLOWED_VISIBILITY, "unknown");
    if (event.producer_epoch) row.producer_epoch = clipId(event.producer_epoch);
    if (event.session_generation != null) row.session_generation = event.session_generation;
    if (event.worker_boot_id) row.worker_boot_id = clipId(event.worker_boot_id);
    sidecar.lifecycle.push(row);
  }

  function recordStage(sidecar, diag, extras) {
    const clean = sanitizeStage(diag);
    if (!clean) return;
    const trigger = clean.trigger || "manual";
    noteProducer(sidecar, clean.producer_epoch);
    noteWorkerBoot(sidecar, clean.worker_boot_id);
    if (typeof clean.content_queue_depth === "number") {
      sidecar.content_queue_depth_high_water = Math.max(
        sidecar.content_queue_depth_high_water,
        clean.content_queue_depth
      );
    }
    if (typeof clean.background_queue_depth === "number") {
      sidecar.background_queue_depth_high_water = Math.max(
        sidecar.background_queue_depth_high_water,
        clean.background_queue_depth
      );
    }
    if (typeof clean.content_queue_wait_ms === "number") {
      sidecar.content_oldest_wait_high_water_ms = Math.max(
        sidecar.content_oldest_wait_high_water_ms,
        clean.content_queue_wait_ms
      );
    }
    observeHistogram(sidecar.histograms.callback_delay_ms, clean.callback_delay_ms);
    observeHistogram(sidecar.histograms.content_queue_wait_ms, clean.content_queue_wait_ms);
    observeHistogram(sidecar.histograms.extract_duration_ms, clean.extract_duration_ms);
    observeHistogram(sidecar.histograms.background_queue_wait_ms, clean.background_queue_wait_ms);
    observeHistogram(sidecar.histograms.append_duration_ms, clean.append_duration_ms);
    observeHistogram(sidecar.histograms.total_ack_latency_ms, clean.total_ack_latency_ms);
    if (trigger === "interval") {
      if (sidecar.interval_details.length < INTERVAL_DETAIL_CAP) {
        sidecar.interval_details.push(clean);
      } else {
        sidecar.counters.suppressed_interval_details += 1;
        sidecar.counters.diagnostic_truncated = true;
      }
    } else if (trigger === "mutation") {
      const ordinal = extras && extras.mutation_callback_ordinal != null
        ? extras.mutation_callback_ordinal
        : clean.request_ordinal;
      if (shouldSampleMutation(ordinal) && sidecar.mutation_details.length < MUTATION_DETAIL_CAP) {
        sidecar.mutation_details.push(clean);
      } else {
        sidecar.counters.suppressed_mutation_details += 1;
        sidecar.counters.diagnostic_truncated = true;
      }
    }
    const slowCandidates = [
      clean.callback_delay_ms,
      clean.content_queue_wait_ms,
      clean.extract_duration_ms,
      clean.background_queue_wait_ms,
      clean.append_duration_ms,
      clean.total_ack_latency_ms,
    ].filter((item) => typeof item === "number");
    const slowMs = slowCandidates.length ? Math.max.apply(null, slowCandidates) : 0;
    if (slowMs >= SLOW_STAGE_MS) {
      if (sidecar.slow_examples.length < SLOW_EXAMPLE_CAP) {
        sidecar.slow_examples.push(clean);
      } else {
        sidecar.counters.suppressed_slow_examples += 1;
        sidecar.counters.diagnostic_truncated = true;
      }
    }
  }

  function recordCompletion(sidecar, completion) {
    if (!completion) return;
    if (sidecar.completions.length >= INTERVAL_DETAIL_CAP + MUTATION_DETAIL_CAP) {
      sidecar.counters.suppressed_completions += 1;
      sidecar.counters.diagnostic_truncated = true;
      return;
    }
    sidecar.completions.push({
      request_ordinal: completion.request_ordinal,
      trigger: completion.trigger || null,
      append_end_mono: finiteNumber(completion.append_end_mono),
      ack_end_mono: finiteNumber(completion.ack_end_mono),
      append_duration_ms: finiteNumber(completion.append_duration_ms),
      total_ack_latency_ms: finiteNumber(completion.total_ack_latency_ms),
      ack_outcome: completion.ack_outcome || "unknown",
      worker_boot_id: clipId(completion.worker_boot_id),
      persisted_sequence: completion.persisted_sequence != null ? completion.persisted_sequence : null,
    });
  }

  function mergeCounters(target, incoming) {
    if (!incoming || typeof incoming !== "object") return;
    const scalar = [
      "expected_interval_opportunities",
      "timer_callbacks",
      "mutation_callbacks",
      "mutation_dirty_sets",
      "manual_callbacks",
      "stale_generation_detected",
      "session_id_mismatch_detected",
      "outstanding_at_stop",
    ];
    for (const key of scalar) {
      if (typeof incoming[key] === "number") target[key] = incoming[key];
    }
    const nested = [
      "callbacks_enqueued",
      "tasks_started",
      "extracted",
      "sent",
      "acked",
      "failed",
      "abandoned",
    ];
    for (const key of nested) {
      if (!incoming[key]) continue;
      target[key] = Object.assign(emptyTriggerCounts(), incoming[key]);
    }
  }

  function summary(sidecar) {
    return {
      schema: "mexc_ui_stage_diagnostics",
      diagnostic_format_version: FORMAT_VERSION,
      extension_version: EXTENSION_VERSION,
      counters: sidecar.counters,
      histograms: sidecar.histograms,
      high_water: {
        content_queue_depth: sidecar.content_queue_depth_high_water,
        background_queue_depth: sidecar.background_queue_depth_high_water,
        content_oldest_wait_ms: sidecar.content_oldest_wait_high_water_ms,
      },
      producer_epochs: sidecar.producer_epochs.slice(),
      worker_boot_ids: sidecar.worker_boot_ids.slice(),
      lifecycle: sidecar.lifecycle.slice(),
      interval_details: sidecar.interval_details.slice(),
      mutation_details: sidecar.mutation_details.slice(),
      slow_examples: sidecar.slow_examples.slice(),
      completions: sidecar.completions.slice(),
    };
  }

  return {
    FORMAT_VERSION,
    EXTENSION_VERSION,
    INTERVAL_DETAIL_CAP,
    MUTATION_DETAIL_CAP,
    MUTATION_DETAIL_FIRST,
    MUTATION_DETAIL_EVERY,
    SLOW_EXAMPLE_CAP,
    LIFECYCLE_CAP,
    CHECKPOINT_MIN_GAP_MS,
    HISTOGRAM_BOUNDS_MS,
    clipId,
    clipEnum,
    finiteNumber,
    emptyCounters,
    emptyHistogram,
    observeHistogram,
    expectedDeadlineMono,
    elapsedIdealSlotOrdinal,
    expectedIntervalOpportunities,
    queueWaitMs,
    shouldSampleMutation,
    sanitizeStage,
    newSidecar,
    noteProducer,
    noteWorkerBoot,
    recordLifecycle,
    recordStage,
    recordCompletion,
    mergeCounters,
    summary,
  };
})();
