/* Read-only MEXC UI capture. Observes rendered text only. Never trades. */

(() => {
  const ATTR = "data-mexc-capture";
  const IGNORE = "data-mexc-capture-ignore";
  let catalog = null;
  let capturing = false;
  let captureId = null;
  let intervalMs = 500;
  let intervalId = null;
  let observer = null;
  let lastEmitKey = "";
  let lastChangeMono = Object.create(null);
  let lastValue = Object.create(null);
  let agePageKey = "";
  let ageCaptureId = "";
  let emitChain = Promise.resolve();

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

  function looksNumeric(text) {
    return parseNumber(text).value !== null;
  }

  function followingText(labelNode, allowUncle) {
    let sibling = labelNode.nextElementSibling;
    while (sibling) {
      const text = collapse(sibling.textContent);
      if (looksNumeric(text)) return text;
      sibling = sibling.nextElementSibling;
    }
    const parent = labelNode.parentElement;
    if (!parent) return "";
    const all = collapse(parent.textContent);
    const label = collapse(labelNode.textContent);
    if (all.toLowerCase().startsWith(label.toLowerCase())) {
      const remainder = collapse(all.slice(label.length));
      if (looksNumeric(remainder)) return remainder;
    }
    if (!allowUncle) return "";
    const grand = parent.parentElement;
    if (grand) {
      let uncle = parent.nextElementSibling;
      while (uncle) {
        const text = collapse(uncle.textContent);
        if (looksNumeric(text)) return text;
        uncle = uncle.nextElementSibling;
      }
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
          changed_at_monotonic_ms: null,
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
        changed_at_monotonic_ms: null,
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
      changed_at_monotonic_ms: null,
      unit,
    };
  }

  function classNodes(tokens, exclude) {
    if (!tokens || !tokens.length) return [];
    const banned = exclude || [];
    return [...document.querySelectorAll("[class]")].filter((node) => {
      if (ignored(node)) return false;
      const classes = String(node.className || "");
      if (!tokens.some((token) => classes.includes(token))) return false;
      return !banned.some((item) => classes.includes(item));
    });
  }

  function extractField(name, spec) {
    const attrNodes = fieldNodes(spec.data_attr_value);
    if (attrNodes.length) {
      const raws = attrNodes.map((node) => collapse(node.textContent) || node.getAttribute("data-value") || "");
      return decode(name, spec, attrNodes, `data_attr:${name}`, raws);
    }
    const nodes = labelNodes(spec.labels || []);
    if (nodes.length) {
      const raws = nodes.map((node) => followingText(node, name === "funding"));
      if (!raws.every((raw) => missingText(raw))) {
        return decode(name, spec, nodes, `label:${name}`, raws);
      }
    }
    const classHits = classNodes(spec.class_contains || [], spec.class_exclude || []);
    if (classHits.length) {
      const raws = classHits.map((node) => collapse(node.textContent));
      return decode(name, spec, classHits, `class:${(spec.class_contains || [name])[0]}`, raws);
    }
    if (nodes.length) {
      return {
        name,
        raw_text: null,
        value: null,
        selector_id: `label:${name}`,
        parse_status: "missing",
        match_count: nodes.length,
        age_ms: null,
        changed_at_monotonic_ms: null,
        unit: null,
      };
    }
    return {
      name,
      raw_text: null,
      value: null,
      selector_id: null,
      parse_status: "missing",
      match_count: 0,
      age_ms: null,
      changed_at_monotonic_ms: null,
      unit: null,
    };
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

  function coalesceRoots(roots) {
    const unique = [];
    for (const node of roots) {
      let absorbed = false;
      for (let index = 0; index < unique.length; index += 1) {
        const existing = unique[index];
        if (existing === node || existing.contains(node)) {
          absorbed = true;
          break;
        }
        if (node.contains(existing)) {
          unique[index] = node;
          absorbed = true;
          break;
        }
      }
      if (!absorbed) unique.push(node);
    }
    if (!unique.length) return { root: null, problems: [] };
    if (unique.length > 1) return { root: null, problems: ["ambiguous_orderbook_heading"] };
    return { root: unique[0], problems: [] };
  }

  function collectOwnPrices(root) {
    const prices = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node = walker.currentNode;
    while (node) {
      if (!ignored(node)) {
        const own = collapse(
          [...node.childNodes]
            .filter((child) => child.nodeType === Node.TEXT_NODE)
            .map((child) => child.textContent)
            .join(" ")
        );
        const price = parsePrice(own);
        if (price) prices.push(price);
      }
      node = walker.nextNode();
    }
    return prices;
  }

  function wrapperPrices(wrapToken, priceToken, problemCode) {
    const hits = [...document.querySelectorAll("[class]")].filter(
      (node) => String(node.className || "").includes(wrapToken) && !ignored(node)
    );
    const coalesced = coalesceRoots(hits);
    if (coalesced.problems.length) return { prices: [], problems: [problemCode] };
    if (!coalesced.root) return { prices: [], problems: [] };
    const prices = [];
    const walker = document.createTreeWalker(coalesced.root, NodeFilter.SHOW_ELEMENT);
    let node = walker.currentNode;
    while (node) {
      if (!ignored(node) && String(node.className || "").includes(priceToken)) {
        const own = collapse(
          [...node.childNodes]
            .filter((child) => child.nodeType === Node.TEXT_NODE)
            .map((child) => child.textContent)
            .join(" ")
        );
        const price = parsePrice(own) || parsePrice(collapse(node.textContent));
        if (price) prices.push(price);
      }
      node = walker.nextNode();
    }
    return { prices, problems: [] };
  }

  function liveOrderBook(lastValue) {
    const spec = catalog.live_orderbook || {};
    const headings = labelNodes(spec.heading_labels || ["Order Book"]);
    if (!headings.length) return { bid: null, ask: null, problems: [] };
    const headerLabels = ["Fair Price", "Mark Price", "Index Price", "Funding Rate / Countdown", "Funding Rate"];
    const band = Number(spec.price_band_frac || 0.1);
    const minSide = Number(spec.min_side_levels || 1);
    const coalesced = coalesceRoots(headings.map((heading) => heading.parentElement || heading));
    if (coalesced.problems.length || !coalesced.root) {
      return { bid: null, ask: null, problems: coalesced.problems };
    }
    if (!(typeof lastValue === "number") || lastValue <= 0) {
      return { bid: null, ask: null, problems: [] };
    }
    let node = coalesced.root;
    let chosen = null;
    while (node && node !== document.body && node !== document.documentElement) {
      const headerHits = labelNodes(headerLabels).filter((item) => node.contains(item));
      if (node !== coalesced.root && headerHits.length) break;
      const near = collectOwnPrices(node).filter(
        (price) => Math.abs(price - lastValue) / lastValue <= band
      );
      const asks = near.filter((price) => price > lastValue);
      const bids = near.filter((price) => price < lastValue);
      if (asks.length >= minSide && bids.length >= minSide) {
        chosen = node;
        break;
      }
      node = node.parentElement;
    }
    if (!chosen) return { bid: null, ask: null, problems: [] };
    const near = collectOwnPrices(chosen).filter(
      (price) => Math.abs(price - lastValue) / lastValue <= band
    );
    const asks = near.filter((price) => price > lastValue);
    const bids = near.filter((price) => price < lastValue);
    const bestAsk = Math.min(...asks);
    const bestBid = Math.max(...bids);
    if (bestBid >= bestAsk) return { bid: null, ask: null, problems: [] };
    return { bid: bestBid, ask: bestAsk, problems: [] };
  }

  function symbolHint() {
    const parts = location.pathname.split("/").filter(Boolean);
    if (parts[0] === "futures") return parseSymbol(parts[1] || "");
    return null;
  }

  function resetAgeClock() {
    lastChangeMono = Object.create(null);
    lastValue = Object.create(null);
    agePageKey = "";
    ageCaptureId = "";
  }

  function applyAges(fields, nowMono, pageKey) {
    if (pageKey !== agePageKey || captureId !== ageCaptureId) {
      resetAgeClock();
      agePageKey = pageKey;
      ageCaptureId = captureId;
    }
    const changed = [];
    for (const [name, rec] of Object.entries(fields)) {
      const stable = rec.parse_status === "ok" || rec.parse_status === "ok_redundant";
      if (!stable || rec.value === null || rec.value === undefined) {
        rec.age_ms = null;
        rec.changed_at_monotonic_ms = null;
        delete lastChangeMono[name];
        delete lastValue[name];
        continue;
      }
      const prevMissing = !Object.prototype.hasOwnProperty.call(lastValue, name);
      if (prevMissing || lastValue[name] !== rec.value) {
        changed.push(name);
        lastChangeMono[name] = nowMono;
        lastValue[name] = rec.value;
        rec.age_ms = 0;
        rec.changed_at_monotonic_ms = nowMono;
      } else {
        rec.changed_at_monotonic_ms = lastChangeMono[name];
        rec.age_ms = Math.max(0, Math.round(nowMono - lastChangeMono[name]));
      }
    }
    return changed;
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
        changed_at_monotonic_ms: null,
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
      fields.bid = { name: "bid", raw_text: String(best[0]), value: best[0], selector_id: "orderbook_max_bid", parse_status: "ok", match_count: 1, age_ms: 0, changed_at_monotonic_ms: null, unit: null };
      if (fields.bid_size.parse_status === "missing" && best[1] > 0) {
        fields.bid_size = { name: "bid_size", raw_text: String(best[1]), value: best[1], selector_id: "orderbook_max_bid", parse_status: "ok", match_count: 1, age_ms: 0, changed_at_monotonic_ms: null, unit: null };
      }
    }
    if (fields.ask.parse_status === "missing" && book.asks && book.asks.length) {
      const best = book.asks.reduce((acc, row) => (row[0] < acc[0] ? row : acc));
      fields.ask = { name: "ask", raw_text: String(best[0]), value: best[0], selector_id: "orderbook_min_ask", parse_status: "ok", match_count: 1, age_ms: 0, changed_at_monotonic_ms: null, unit: null };
      if (fields.ask_size.parse_status === "missing" && best[1] > 0) {
        fields.ask_size = { name: "ask_size", raw_text: String(best[1]), value: best[1], selector_id: "orderbook_min_ask", parse_status: "ok", match_count: 1, age_ms: 0, changed_at_monotonic_ms: null, unit: null };
      }
    }
    const lastValue = typeof fields.last.value === "number" ? fields.last.value : null;
    if (fields.bid.parse_status === "missing" || fields.ask.parse_status === "missing") {
      const liveSpec = catalog.live_orderbook || {};
      const asks = wrapperPrices(
        liveSpec.asks_class_contains || "asksWrapper",
        liveSpec.ask_price_class_contains || "sell",
        "ambiguous_asks_wrapper"
      );
      const bids = wrapperPrices(
        liveSpec.bids_class_contains || "bidsWrapper",
        liveSpec.bid_price_class_contains || "buy",
        "ambiguous_bids_wrapper"
      );
      book.problems.push(...asks.problems, ...bids.problems);
      if (asks.prices.length && bids.prices.length) {
        const bestAsk = Math.min(...asks.prices);
        const bestBid = Math.max(...bids.prices);
        if (bestBid < bestAsk) {
          if (fields.bid.parse_status === "missing") {
            fields.bid = {
              name: "bid",
              raw_text: String(bestBid),
              value: bestBid,
              selector_id: "live_asks_bids_wrapper",
              parse_status: "ok",
              match_count: 1,
              age_ms: 0,
              changed_at_monotonic_ms: null,
              unit: null,
            };
          }
          if (fields.ask.parse_status === "missing") {
            fields.ask = {
              name: "ask",
              raw_text: String(bestAsk),
              value: bestAsk,
              selector_id: "live_asks_bids_wrapper",
              parse_status: "ok",
              match_count: 1,
              age_ms: 0,
              changed_at_monotonic_ms: null,
              unit: null,
            };
          }
        }
      }
    }
    if (fields.bid.parse_status === "missing" || fields.ask.parse_status === "missing") {
      const live = liveOrderBook(lastValue);
      book.problems.push(...live.problems);
      if (live.bid !== null && fields.bid.parse_status === "missing") {
        fields.bid = {
          name: "bid",
          raw_text: String(live.bid),
          value: live.bid,
          selector_id: "live_orderbook_split_by_last",
          parse_status: "ok",
          match_count: 1,
          age_ms: 0,
          changed_at_monotonic_ms: null,
          unit: null,
        };
      }
      if (live.ask !== null && fields.ask.parse_status === "missing") {
        fields.ask = {
          name: "ask",
          raw_text: String(live.ask),
          value: live.ask,
          selector_id: "live_orderbook_split_by_last",
          parse_status: "ok",
          match_count: 1,
          age_ms: 0,
          changed_at_monotonic_ms: null,
          unit: null,
        };
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
    const nowMono = performance.now();
    const pageKey = `${location.host}|${location.pathname}|${fields.symbol && fields.symbol.value ? fields.symbol.value : ""}`;
    const changed = applyAges(fields, nowMono, pageKey);
    const received = new Date().toISOString();
    return {
      schema: "mexc_ui_raw_snapshot",
      schema_version: 1,
      capture_id: captureId,
      sequence: 0,
      received_at_local: received,
      observed_at_local: received,
      monotonic_ms: nowMono,
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

  function stopLocal() {
    capturing = false;
    if (intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
    if (observer) observer.disconnect();
  }

  function emit(trigger) {
    if (!capturing) return;
    emitChain = emitChain.then(async () => {
      if (!capturing) return;
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
      const resp = await chrome.runtime.sendMessage({ type: "CAPTURE_SNAPSHOT", snapshot });
      if (!resp || resp.ok !== true) {
        stopLocal();
      }
    }).catch(() => {
      stopLocal();
    });
  }

  function startObserver() {
    if (observer) observer.disconnect();
    observer = new MutationObserver(() => {
      if (capturing) emit("mutation");
    });
    observer.observe(document.body, { subtree: true, childList: true, characterData: true });
  }

  async function applyState(state) {
    intervalMs = Number((state && state.intervalMs) || 500);
    if (![250, 500, 1000].includes(intervalMs)) intervalMs = 500;
    if (intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
    const want = Boolean(state && state.capturing);
    if (!want) {
      if (capturing) {
        capturing = false;
        if (observer) observer.disconnect();
        await chrome.runtime.sendMessage({ type: "STOP_SESSION" });
      }
      return;
    }
    const session = await chrome.runtime.sendMessage({
      type: "START_SESSION",
      intervalMs,
      page_host: location.host,
      page_path: location.pathname,
    });
    if (!session || session.ok !== true) {
      stopLocal();
      return;
    }
    captureId = session.session_id;
    resetAgeClock();
    lastEmitKey = "";
    capturing = true;
    startObserver();
    emit("manual");
    intervalId = setInterval(() => emit("interval"), intervalMs);
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
