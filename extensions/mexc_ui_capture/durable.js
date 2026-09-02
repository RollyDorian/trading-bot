/* Local IndexedDB capture store. No network. Fail closed on errors. */

const DB_NAME = "mexc_ui_capture_v1";
const DB_VERSION = 1;
const DEFAULT_CHUNK_SIZE = 250;
const SESSION_RECORD_SCHEMA = "mexc_ui_capture_session";
const SESSION_RECORD_VERSION = 1;

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onerror = () => reject(req.error || new Error("indexedDB open failed"));
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("sessions")) {
        db.createObjectStore("sessions", { keyPath: "session_id" });
      }
      if (!db.objectStoreNames.contains("chunks")) {
        const chunks = db.createObjectStore("chunks", {
          keyPath: ["session_id", "chunk_index"],
        });
        chunks.createIndex("by_session", "session_id", { unique: false });
      }
      if (!db.objectStoreNames.contains("meta")) {
        db.createObjectStore("meta", { keyPath: "key" });
      }
    };
    req.onsuccess = () => resolve(req.result);
  });
}

function reqAsPromise(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error("indexedDB request failed"));
  });
}

function waitTx(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error || new Error("indexedDB transaction failed"));
    tx.onabort = () => reject(tx.error || new Error("indexedDB transaction aborted"));
  });
}

function emptySession(fields) {
  return {
    session_id: fields.session_id,
    started_at: fields.started_at,
    interval_ms: fields.interval_ms,
    page_host: fields.page_host || null,
    page_path: fields.page_path || null,
    ended_at: null,
    status: "running",
    n_snapshots: 0,
    n_chunks: 0,
    first_sequence: null,
    last_sequence: null,
    chunk_size: fields.chunk_size || DEFAULT_CHUNK_SIZE,
    storage_error: null,
    sequence_gaps: [],
    client_sequence_mismatches: [],
  };
}

function sessionStartRecord(meta) {
  return {
    record_type: "session_start",
    schema: SESSION_RECORD_SCHEMA,
    schema_version: SESSION_RECORD_VERSION,
    session_id: meta.session_id,
    started_at: meta.started_at,
    interval_ms: meta.interval_ms,
    page_host: meta.page_host,
    page_path: meta.page_path,
    chunk_size: meta.chunk_size,
    status: meta.status,
  };
}

function sessionEndRecord(meta) {
  return {
    record_type: "session_end",
    schema: SESSION_RECORD_SCHEMA,
    schema_version: SESSION_RECORD_VERSION,
    session_id: meta.session_id,
    started_at: meta.started_at,
    ended_at: meta.ended_at,
    interval_ms: meta.interval_ms,
    page_host: meta.page_host,
    page_path: meta.page_path,
    status: meta.status,
    n_snapshots: meta.n_snapshots,
    n_chunks: meta.n_chunks,
    first_sequence: meta.first_sequence,
    last_sequence: meta.last_sequence,
    chunk_size: meta.chunk_size,
    storage_error: meta.storage_error,
    sequence_gaps: meta.sequence_gaps || [],
    client_sequence_mismatches: meta.client_sequence_mismatches || [],
  };
}

const MexcDurable = {
  chunkSize: DEFAULT_CHUNK_SIZE,

  async getMeta(key) {
    const db = await openDb();
    try {
      const tx = db.transaction("meta", "readonly");
      const row = await reqAsPromise(tx.objectStore("meta").get(key));
      await waitTx(tx);
      return row ? row.value : null;
    } finally {
      db.close();
    }
  },

  async setMeta(key, value) {
    const db = await openDb();
    try {
      const tx = db.transaction("meta", "readwrite");
      tx.objectStore("meta").put({ key, value });
      await waitTx(tx);
    } finally {
      db.close();
    }
  },

  async getSession(sessionId) {
    const db = await openDb();
    try {
      const tx = db.transaction("sessions", "readonly");
      const row = await reqAsPromise(tx.objectStore("sessions").get(sessionId));
      await waitTx(tx);
      return row || null;
    } finally {
      db.close();
    }
  },

  async putSession(meta) {
    const db = await openDb();
    try {
      const tx = db.transaction("sessions", "readwrite");
      tx.objectStore("sessions").put(meta);
      await waitTx(tx);
    } finally {
      db.close();
    }
  },

  async startSession(fields) {
    const activeId = await this.getMeta("active_session_id");
    if (activeId) {
      const active = await this.getSession(activeId);
      if (active && active.status === "running") {
        active.status = "stopped";
        active.ended_at = fields.started_at;
        await this.putSession(active);
      }
    }
    const meta = emptySession({
      session_id: fields.session_id || crypto.randomUUID(),
      started_at: fields.started_at,
      interval_ms: fields.interval_ms,
      page_host: fields.page_host,
      page_path: fields.page_path,
      chunk_size: this.chunkSize,
    });
    await this.putSession(meta);
    await this.setMeta("active_session_id", meta.session_id);
    await this.setMeta("last_session_id", meta.session_id);
    return meta;
  },

  async appendSnapshot(snapshot) {
    const activeId = await this.getMeta("active_session_id");
    if (!activeId) {
      throw new Error("no active capture session");
    }
    const db = await openDb();
    try {
      const tx = db.transaction(["sessions", "chunks", "meta"], "readwrite");
      const sessions = tx.objectStore("sessions");
      const chunks = tx.objectStore("chunks");
      const metaStore = tx.objectStore("meta");
      const meta = await reqAsPromise(sessions.get(activeId));
      if (!meta || meta.status !== "running") {
        throw new Error("no running capture session");
      }
      if (meta.storage_error) {
        throw new Error(meta.storage_error);
      }
      const assigned = (meta.last_sequence || 0) + 1;
      const clientSeq = snapshot.sequence;
      if (clientSeq !== undefined && clientSeq !== null && clientSeq !== 0 && clientSeq !== assigned) {
        meta.client_sequence_mismatches.push({
          expected: assigned,
          got: clientSeq,
          assigned,
        });
      }
      const committed = Object.assign({}, snapshot, {
        sequence: assigned,
        capture_id: meta.session_id,
      });
      const chunkIndex = Math.floor(meta.n_snapshots / meta.chunk_size);
      const chunkKey = [meta.session_id, chunkIndex];
      let chunk = await reqAsPromise(chunks.get(chunkKey));
      if (!chunk) {
        chunk = { session_id: meta.session_id, chunk_index: chunkIndex, lines: [] };
      }
      if (chunk.lines.length >= meta.chunk_size) {
        throw new Error("chunk overflow");
      }
      chunk.lines.push(JSON.stringify(committed));
      chunks.put(chunk);
      meta.n_snapshots += 1;
      meta.n_chunks = chunkIndex + 1;
      if (meta.first_sequence === null) meta.first_sequence = assigned;
      if (meta.last_sequence !== null && assigned !== meta.last_sequence + 1) {
        meta.sequence_gaps.push({ expected: meta.last_sequence + 1, got: assigned });
      }
      meta.last_sequence = assigned;
      sessions.put(meta);
      metaStore.put({ key: "last_session_id", value: meta.session_id });
      await waitTx(tx);
      return { committed, meta };
    } finally {
      db.close();
    }
  },

  async markFailed(message, endedAt) {
    const activeId = await this.getMeta("active_session_id");
    if (!activeId) return null;
    const meta = await this.getSession(activeId);
    if (!meta) return null;
    meta.status = "failed";
    meta.storage_error = String(message);
    meta.ended_at = endedAt || new Date().toISOString();
    await this.putSession(meta);
    await this.setMeta("active_session_id", null);
    return meta;
  },

  async stopSession(endedAt, status) {
    const activeId = await this.getMeta("active_session_id");
    if (!activeId) return null;
    const meta = await this.getSession(activeId);
    if (!meta) return null;
    meta.status = status || "stopped";
    meta.ended_at = endedAt || new Date().toISOString();
    await this.putSession(meta);
    await this.setMeta("active_session_id", null);
    await this.setMeta("last_session_id", meta.session_id);
    return meta;
  },

  async exportMeta(sessionId) {
    const id = sessionId || (await this.getMeta("last_session_id")) || (await this.getMeta("active_session_id"));
    if (!id) throw new Error("no capture session to export");
    const meta = await this.getSession(id);
    if (!meta) throw new Error("unknown capture session");
    return {
      session_id: id,
      n_chunks: meta.n_chunks,
      n_snapshots: meta.n_snapshots,
      session_start: sessionStartRecord(meta),
      session_end: sessionEndRecord(meta),
    };
  },

  async listSessions() {
    // Oldest-first so export-all preserves stop/start and reload boundaries.
    const db = await openDb();
    try {
      const tx = db.transaction("sessions", "readonly");
      const rows = await reqAsPromise(tx.objectStore("sessions").getAll());
      await waitTx(tx);
      const list = Array.isArray(rows) ? rows.slice() : [];
      list.sort((left, right) => {
        const started = String(left.started_at || "").localeCompare(String(right.started_at || ""));
        if (started !== 0) return started;
        return String(left.session_id || "").localeCompare(String(right.session_id || ""));
      });
      return list;
    } finally {
      db.close();
    }
  },

  async exportMetaAll() {
    const sessions = await this.listSessions();
    if (!sessions.length) throw new Error("no capture session to export");
    return {
      n_sessions: sessions.length,
      sessions: sessions.map((meta) => ({
        session_id: meta.session_id,
        n_chunks: meta.n_chunks,
        n_snapshots: meta.n_snapshots,
        session_start: sessionStartRecord(meta),
        session_end: sessionEndRecord(meta),
      })),
    };
  },

  async exportChunk(sessionId, chunkIndex) {
    const db = await openDb();
    try {
      const tx = db.transaction("chunks", "readonly");
      const chunk = await reqAsPromise(tx.objectStore("chunks").get([sessionId, chunkIndex]));
      await waitTx(tx);
      return chunk && chunk.lines ? chunk.lines : [];
    } finally {
      db.close();
    }
  },
};
