/* Read-only MEXC UI capture. Observes rendered text only. Never trades. */

(() => {
  const ATTR = "data-mexc-capture";
  const IGNORE = "data-mexc-capture-ignore";
  let catalog = null;
  let capturing = false;
  let sequence = 0;
  let previous = {};
  let intervalMs = 500;
  let intervalId = null;
  let observer = null;
  let lastEmitKey = "";

  function collapse(text) {
    return String(text || "")
      .replace(/\u00a0/g, " ")
      .replace(/\u200e/g, "")
      .trim()
      .replace(/\s+/g, " ");
  }

  function missingText(text) {
    const value = collapse(text).toLowerCase();
    return !value || value === "--" || value === "-" || value === "—" || value === "n/a";
  }

  function parseNumber(text) {
    if (missingText(text)) return { value: null, unit: null };
    const compact = collapse(text);
    const stripped = compact.replace(/,/g, "");
    const match = stripped.match(/[-+]?(?:\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)/);
    if (!match) return { value: null, unit: null };
    const value = Number(match[0]);
    if (!Number.isFinite(value)) return { value: null, unit: null };
    return { value, unit: compact.includes("%") ? "percent" : null };
  }

  function parsePrice(text) {
    const parsed = parseNumber(text);
    if (parsed.value === null || parsed.value <= 0) return null;
    return parsed.value;
  }

  function parseSymbol(text) {
    if (missingText(text)) return null;
    const compact = collapse(text).toUpperCase().replace(/[-_/ ]/g, "");
    if (!/^[A-Z0-9]{6,}$/.test(compact)) return null;
    return compact;
  }

  function ignored(node) {
    return Boolean(node && node.closest && node.closest(`[${IGNORE}]`));
  }

  function fieldNodes(attrValue) {
    return [...document.querySelectorAll(`[${ATTR}="${attrValue}"]`)].filter(
      (node) => !ignored(node)
    );
  }

  function labelNodes(labels) {
    if (!labels || !labels.length) return [];
    const wanted = labels.map((item) => item.toLowerCase());
    const hits = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let node = walker.currentNode;
    while (node) {
      if (node !== document.body && !ignored(node)) {
        const text = collapse(node.childNodes.length === 1 ? node.textContent : node.firstChild && node.firstChild.nodeType === Node.TEXT_NODE ? node.firstChild.textContent : "");
        // Prefer exact own-text labels, not giant containers.
        const own = collapse(
          [...node.childNodes]
            .filter((child) => child.nodeType === Node.TEXT_NODE)
            .map((child) => child.textContent)
            .join(" ")
        );
        if (own && wanted.includes(own.toLowerCase())) hits.push(node);
      }
      node = walker.nextNode();
    }
    return hits;
  }

  function followingText(labelNode) {
    let sibling = labelNode.nextElementSibling;
    if (sibling) return collapse(sibling.textContent);
    const parent = labelNode.parentElement;
    if (!parent) return "";
    const all = collapse(parent.textContent);
    const label = collapse(labelNode.textContent);
    if (all.toLowerCase().startsWith(label.toLowerCase())) {
      return collapse(all.slice(label.length));
    }
    return "";
  }

  function decode(name, spec, nodes, selectorId, raws) {
    const values = [];
    let unit = null;
    for (const raw of raws) {
      let value = null;
      if (spec.kind === "symbol") value = parseSymbol(raw);
      else if (spec.kind === "timestamp") value = missingText(raw) ? null : collapse(raw);
      else if (spec.kind === "size" || spec.kind === "price") value = parsePrice(raw);
      else {
        const parsed = parseNumber(raw);
        value = parsed.value;
        unit = parsed.unit;
      }
      if (value === null) {
        return {
          name,
          raw_text: raws[0] || null,
          value: null,
          selector_id: selectorId,
          parse_status: "unparsable",
          match_count: nodes.length,
          age_ms: null,
          unit,
        };
      }
      values.push(value);
    }
    const unique = [...new Set(values.map((item) => String(item)))];
    if (unique.length > 1) {
      return {
        name,
        raw_text: raws[0],
        value: null,
        selector_id: selectorId,
        parse_status: "ambiguous",
        match_count: nodes.length,
        age_ms: null,
        unit,
      };
    }
    return {
      name,
      raw_text: raws[0],
      value: values[0],
      selector_id: selectorId,
      parse_status: nodes.length > 1 ? "ok_redundant" : "ok",
      match_count: nodes.length,
      age_ms: 0,
      unit,
    };
  }

  function extractField(name, spec) {
    const attrNodes = fieldNodes(spec.data_attr_value);
    if (attrNodes.length) {
      const raws = attrNodes.map((node) => collapse(node.textContent) || node.getAttribute("data-value") || "");
      return decode(name, spec, attrNodes, `data_attr:${name}`, raws);
    }
    const nodes = labelNodes(spec.labels || []);
    if (!nodes.length) {
      return {
        name,
        raw_text: null,
        value: null,
        selector_id: null,
        parse_status: "missing",
        match_count: 0,
        age_ms: null,
        unit: null,
      };
    }
    const raws = nodes.map((node) => followingText(node));
    return decode(name, spec, nodes, `label:${name}`, raws);
  }

  function parseLevels(root, side, spec) {
    const nodes = [...root.querySelectorAll(`[${spec.level_attr}="${side}"], [${ATTR}="${side}"]`)].filter(
      (node) => !ignored(node)
    );
    const levels = [];
    for (const node of nodes) {
      if (levels.length >= spec.max_levels) break;
      const price = parsePrice(node.getAttribute(spec.price_attr) || node.textContent);
      const size = parsePrice(node.getAttribute(spec.size_attr) || node.getAttribute("data-qty"));
      if (price && size) levels.push([price, size]);
    }
    return levels.length ? levels : null;
  }

  function orderbook() {
    const spec = catalog.orderbook;
    const roots = fieldNodes(spec.root_attr_value);
    if (roots.length > 1) return { bids: null, asks: null, selector: "data_attr:orderbook", problems: ["ambiguous_orderbook_root"] };
    if (!roots.length) return { bids: null, asks: null, selector: null, problems: [] };
    const root = roots[0];
    return {
      bids: parseLevels(root, "bid", spec) || parseLevels(root, spec.bids_attr_value, spec),
      asks: parseLevels(root, "ask", spec) || parseLevels(root, spec.asks_attr_value, spec),
      selector: "data_attr:orderbook",
      problems: [],
    };
  }

  function symbolHint() {
    const parts = location.pathname.split("/").filter(Boolean);
    if (parts[0] === "futures") return parseSymbol(parts[1] || "");
    return null;
  }

  function extract(trigger) {
    if (!catalog) return null;
    const fields = {};
    for (const [name, spec] of Object.entries(catalog.fields)) {
      fields[name] = extractField(name, spec);
    }
    const book = orderbook();
    const hint = symbolHint();
    if (fields.symbol.parse_status === "missing" && hint) {
      fields.symbol = {
        name: "symbol",
        raw_text: hint,
        value: hint,
        selector_id: "page_path",
        parse_status: "ok",
        match_count: 1,
        age_ms: 0,
        unit: null,
      };
    }
    if (
      fields.symbol.value &&
      hint &&
      (fields.symbol.parse_status === "ok" || fields.symbol.parse_status === "ok_redundant") &&
      fields.symbol.value !== hint
    ) {
      fields.symbol.parse_status = "ambiguous";
      fields.symbol.value = null;
    }
    if (fields.bid.parse_status === "missing" && book.bids && book.bids.length) {
      const best = book.bids.reduce((acc, row) => (row[0] > acc[0] ? row : acc));
      fields.bid = { name: "bid", raw_text: String(best[0]), value: best[0], selector_id: "orderbook_max_bid", parse_status: "ok", match_count: 1, age_ms: 0, unit: null };
      if (fields.bid_size.parse_status === "missing" && best[1] > 0) {
        fields.bid_size = { name: "bid_size", raw_text: String(best[1]), value: best[1], selector_id: "orderbook_max_bid", parse_status: "ok", match_count: 1, age_ms: 0, unit: null };
      }
    }
    if (fields.ask.parse_status === "missing" && book.asks && book.asks.length) {
      const best = book.asks.reduce((acc, row) => (row[0] < acc[0] ? row : acc));
      fields.ask = { name: "ask", raw_text: String(best[0]), value: best[0], selector_id: "orderbook_min_ask", parse_status: "ok", match_count: 1, age_ms: 0, unit: null };
      if (fields.ask_size.parse_status === "missing" && best[1] > 0) {
        fields.ask_size = { name: "ask_size", raw_text: String(best[1]), value: best[1], selector_id: "orderbook_min_ask", parse_status: "ok", match_count: 1, age_ms: 0, unit: null };
      }
    }
    const invalid = [...book.problems];
    for (const [name, spec] of Object.entries(catalog.fields)) {
      const rec = fields[name];
      if (rec.parse_status === "ambiguous") invalid.push(`ambiguous:${name}`);
      if (spec.required_for_valid && rec.parse_status !== "ok" && rec.parse_status !== "ok_redundant") {
        invalid.push(`missing_required:${name}`);
      }
    }
    if (typeof fields.bid.value === "number" && typeof fields.ask.value === "number" && fields.bid.value >= fields.ask.value) {
      invalid.push("crossed_book");
    }
    const changed = [];
    const now = Date.now();
    for (const [name, rec] of Object.entries(fields)) {
      const prev = previous[name];
      if (rec.parse_status === "missing") continue;
      if (!prev || prev.value !== rec.value) changed.push(name);
      rec.age_ms = prev && prev.value === rec.value ? (prev.age_ms || 0) + intervalMs : 0;
    }
    previous = fields;
    sequence += 1;
    const received = new Date(now).toISOString();
    return {
      schema: "mexc_ui_raw_snapshot",
      schema_version: 1,
      sequence,
      received_at_local: received,
      observed_at_local: received,
      monotonic_ms: performance.now(),
      exchange_display_at: typeof fields.exchange_display_at.value === "string" ? fields.exchange_display_at.value : null,
      trigger,
      selector_catalog_version: catalog.catalog_version,
      page_host: location.host,
      page_path: location.pathname,
      symbol_hint: hint,
      sample_interval_ms: intervalMs,
      observation_valid: invalid.length === 0,
      invalid_reasons: invalid,
      changed_fields: changed,
      fields,
      depth_bids: book.bids,
      depth_asks: book.asks,
      depth_selector_id: book.selector,
    };
  }

  function emit(trigger) {
    const snapshot = extract(trigger);
    if (!snapshot) return;
    const key = JSON.stringify({
      bid: snapshot.fields.bid && snapshot.fields.bid.value,
      ask: snapshot.fields.ask && snapshot.fields.ask.value,
      mark: snapshot.fields.mark && snapshot.fields.mark.value,
      index: snapshot.fields.index && snapshot.fields.index.value,
      last: snapshot.fields.last && snapshot.fields.last.value,
      valid: snapshot.observation_valid,
    });
    if (trigger === "interval" && key === lastEmitKey) return;
    lastEmitKey = key;
    chrome.runtime.sendMessage({ type: "CAPTURE_SNAPSHOT", snapshot });
  }

  function startObserver() {
    if (observer) observer.disconnect();
    observer = new MutationObserver(() => {
      if (capturing) emit("mutation");
    });
    observer.observe(document.body, { subtree: true, childList: true, characterData: true });
  }

  function applyState(state) {
    capturing = Boolean(state && state.capturing);
    intervalMs = Number((state && state.intervalMs) || 500);
    if (![250, 500, 1000].includes(intervalMs)) intervalMs = 500;
    if (intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
    if (capturing) {
      startObserver();
      emit("manual");
      intervalId = setInterval(() => emit("interval"), intervalMs);
    } else if (observer) {
      observer.disconnect();
    }
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (message && message.type === "CAPTURE_STATE") applyState(message.state);
  });

  fetch(chrome.runtime.getURL("selector_catalog_v1.json"))
    .then((response) => response.json())
    .then((payload) => {
      catalog = payload;
      chrome.storage.local.get(["capturing", "intervalMs"], applyState);
    });
})();
