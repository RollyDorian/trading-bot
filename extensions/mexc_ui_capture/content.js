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
  let lastHeaderProbeSignature = "";
  let lastChangeMono = Object.create(null);
  let lastValue = Object.create(null);
  let agePageKey = "";
  let ageCaptureId = "";
  let emitChain = Promise.resolve();

  let currentLocale = "unknown";
  const LOCALE_PREFIX = /^[a-z]{2}-[A-Z]{2}$/;
  const KNOWN_LOCALES = { "ru-RU": true, "en-US": true };

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

  function pathParts(pathname) {
    return String(pathname || "")
      .split("/")
      .filter(Boolean)
      .map((item) => item.split("?")[0]);
  }

  function localeFromPathname(pathname) {
    const parts = pathParts(pathname);
    if (!parts.length) return "unknown";
    if (LOCALE_PREFIX.test(parts[0]) && parts[1] === "futures") {
      return KNOWN_LOCALES[parts[0]] ? parts[0] : "unknown";
    }
    return "unknown";
  }

  function stripBidi(text) {
    return String(text || "").replace(/[\u200e\u200f\u202a-\u202e]/g, "");
  }

  function compactGroupingSpaces(text) {
    const translated = String(text || "")
      .replace(/[\u00a0\u202f\u2007\u2008\u2009\u200a]/g, " ");
    return translated.replace(/(\d)[ ]+(?=\d)/g, "$1");
  }

  function splitGrouped(body, groupChar) {
    const parts = body.split(groupChar);
    if (!parts.length || parts.some((part) => !/^\d+$/.test(part))) return null;
    if (!parts[0] || parts[0].length > 3) return null;
    for (let i = 1; i < parts.length; i += 1) {
      if (parts[i].length !== 3) return null;
    }
    return parts;
  }

  function interpretEnUs(body) {
    if (body.includes(",")) {
      if ((body.match(/\./g) || []).length > 1) return null;
      if (body.includes(".")) {
        const idx = body.lastIndexOf(".");
        const left = body.slice(0, idx);
        const right = body.slice(idx + 1);
        const grouped = splitGrouped(left, ",");
        if (!grouped || !/^\d+$/.test(right) || !right) return null;
        const value = Number(`${grouped.join("")}.${right}`);
        return Number.isFinite(value) ? value : null;
      }
      const grouped = splitGrouped(body, ",");
      if (!grouped) return null;
      const value = Number(grouped.join(""));
      return Number.isFinite(value) ? value : null;
    }
    if ((body.match(/\./g) || []).length > 1) return null;
    if (body.includes(".")) {
      const parts = body.split(".");
      if (parts.length !== 2 || !/^\d+$/.test(parts[0]) || !/^\d+$/.test(parts[1]) || !parts[1]) {
        return null;
      }
      const value = Number(`${parts[0]}.${parts[1]}`);
      return Number.isFinite(value) ? value : null;
    }
    if (!/^\d+$/.test(body)) return null;
    const value = Number(body);
    return Number.isFinite(value) ? value : null;
  }

  function interpretRuRu(body) {
    if ((body.match(/,/g) || []).length > 1) return null;
    if (body.includes(",")) {
      const parts = body.split(",");
      if (parts.length !== 2 || !/^\d+$/.test(parts[1]) || !parts[1]) return null;
      if (parts[0].includes(".")) {
        const grouped = splitGrouped(parts[0], ".");
        if (!grouped) return null;
        const value = Number(`${grouped.join("")}.${parts[1]}`);
        return Number.isFinite(value) ? value : null;
      }
      if (!/^\d+$/.test(parts[0])) return null;
      const value = Number(`${parts[0]}.${parts[1]}`);
      return Number.isFinite(value) ? value : null;
    }
    if (body.includes(".")) {
      const grouped = splitGrouped(body, ".");
      if (!grouped) return null;
      const value = Number(grouped.join(""));
      return Number.isFinite(value) ? value : null;
    }
    if (!/^\d+$/.test(body)) return null;
    const value = Number(body);
    return Number.isFinite(value) ? value : null;
  }

  function interpretUnknown(body) {
    if (body.includes(",")) return null;
    if ((body.match(/\./g) || []).length > 1) return null;
    if (body.includes(".")) {
      const parts = body.split(".");
      if (parts.length !== 2 || !/^\d+$/.test(parts[0]) || !/^\d+$/.test(parts[1]) || !parts[1]) {
        return null;
      }
      if (parts[1].length === 3 && parts[0].length >= 1 && parts[0].length <= 3) return null;
      const value = Number(`${parts[0]}.${parts[1]}`);
      return Number.isFinite(value) ? value : null;
    }
    if (!/^\d+$/.test(body)) return null;
    const value = Number(body);
    return Number.isFinite(value) ? value : null;
  }

  function interpretBody(body, locale) {
    if (locale === "ru-RU") return interpretRuRu(body);
    if (locale === "en-US") return interpretEnUs(body);
    return interpretUnknown(body);
  }

  function parseNumber(text, locale) {
    if (missingText(text)) return { value: null, unit: null };
    const mode = locale || currentLocale || "unknown";
    const compact = collapse(stripBidi(text));
    const unit = compact.includes("%") ? "percent" : null;
    const match = compact.match(/[-+]?(?:\d[\d\s.,]*)(?:[eE][-+]?\d+)?/);
    if (!match) return { value: null, unit: null };
    let token = match[0].trim();
    let sign = 1;
    if (token[0] === "+" || token[0] === "-") {
      sign = token[0] === "-" ? -1 : 1;
      token = token.slice(1);
    }
    let exponent = 0;
    const expMatch = token.match(/[eE]([+-]?\d+)$/);
    if (expMatch) {
      exponent = Number(expMatch[1]);
      token = token.slice(0, expMatch.index);
    }
    const body = compactGroupingSpaces(token).replace(/ /g, "");
    if (!body || !/\d/.test(body)) return { value: null, unit: null };
    const number = interpretBody(body, mode);
    if (number === null) return { value: null, unit: null };
    let value = sign * number;
    if (exponent) value *= 10 ** exponent;
    if (!Number.isFinite(value)) return { value: null, unit: null };
    return { value, unit };
  }

  function parsePrice(text, locale) {
    const parsed = parseNumber(text, locale);
    if (parsed.value === null || parsed.unit === "percent" || parsed.value <= 0) return null;
    return parsed.value;
  }

  function parseSymbol(text) {
    if (missingText(text)) return null;
    const compact = collapse(text).toUpperCase().replace(/[-_/ ]/g, "");
    if (!/^[A-Z0-9]{6,}$/.test(compact)) return null;
    return compact;
  }

  function symbolFromFuturesPath(pathname) {
    const parts = pathParts(pathname);
    if (!parts.length) return null;
    if (parts[0] === "futures" && parts[1]) return parseSymbol(parts[1]);
    if (LOCALE_PREFIX.test(parts[0]) && parts[1] === "futures" && parts[2]) {
      return parseSymbol(parts[2]);
    }
    return null;
  }

  function joinPriceTokens(tokens) {
    const pieces = [];
    for (const raw of tokens || []) {
      const piece = stripBidi(raw).trim();
      if (piece) pieces.push(piece);
    }
    if (!pieces.length) return null;
    const digitish = /^[+-]?\d+$/;
    for (let i = 0; i < pieces.length - 1; i += 1) {
      if (digitish.test(pieces[i]) && digitish.test(pieces[i + 1])) return null;
    }
    return pieces.join("");
  }

  function boundedTextTokens(node, maxTokens) {
    const limit = maxTokens || 12;
    const tokens = [];
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let current = walker.nextNode();
    while (current) {
      const piece = current.textContent || "";
      if (piece.trim()) tokens.push(piece);
      if (tokens.length >= limit) break;
      current = walker.nextNode();
    }
    return tokens;
  }

  function nodePriceHit(node) {
    const tokens = boundedTextTokens(node);
    const joined = joinPriceTokens(tokens);
    const combined = collapse(node.textContent || "");
    if (joined === null && tokens.length > 1) return null;
    const text = joined || combined;
    const price = parsePrice(text);
    if (!price) return null;
    const rawText = combined || joined;
    if (!rawText) return null;
    return { value: price, raw_text: rawText, tokens };
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
          parser_locale: currentLocale,
          raw_tokens: null,
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
        parser_locale: currentLocale,
        raw_tokens: null,
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
      parser_locale: currentLocale,
      raw_tokens: null,
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
    let nodes = labelNodes(spec.labels || []);
    if (["mark", "index", "funding"].includes(name)) {
      nodes = nodes.filter((node) => !marketHeaderItemAncestor(node));
    }
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
        parser_locale: currentLocale,
        raw_tokens: null,
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
      parser_locale: currentLocale,
      raw_tokens: null,
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

  function uniqueNested(roots) {
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
    return unique;
  }

  function coalesceRoots(roots, problemCode) {
    const unique = uniqueNested(roots);
    const code = problemCode || "ambiguous_orderbook_heading";
    if (!unique.length) return { root: null, problems: [] };
    if (unique.length > 1) return { root: null, problems: [code] };
    return { root: unique[0], problems: [] };
  }

  function classNameOf(node) {
    if (!node) return "";
    return node.getAttribute && node.getAttribute("class")
      ? node.getAttribute("class")
      : String(node.className || "");
  }

  function hasRenderableRect(node) {
    if (!node || !node.getBoundingClientRect) return false;
    const self = node.getBoundingClientRect();
    if (self.width > 0 && self.height > 0) return true;
    // asksWrapper/bidsWrapper can collapse their own box while price rows paint.
    const descendants = node.querySelectorAll ? node.querySelectorAll("*") : [];
    for (const child of descendants) {
      const rect = child.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) return true;
    }
    return false;
  }

  function isVisible(node) {
    if (!node || node.nodeType !== 1) return false;
    if (node.isConnected === false) return false;
    let current = node;
    while (current && current.nodeType === 1) {
      if (current.hasAttribute("hidden")) return false;
      let style = null;
      try {
        style = window.getComputedStyle(current);
      } catch (_err) {
        return false;
      }
      if (!style || style.display === "none" || style.visibility === "hidden") return false;
      current = current.parentElement;
    }
    return hasRenderableRect(node);
  }

  function classTokenHits(token) {
    return [...document.querySelectorAll("[class]")].filter(
      (node) => classNameOf(node).includes(token) && !ignored(node)
    );
  }

  function collectOwnPrices(root) {
    const hits = [];
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
        if (own) {
          const hit = nodePriceHit(node);
          if (hit) hits.push(hit);
        }
      }
      node = walker.nextNode();
    }
    return hits;
  }

  function wrapperPricesIn(wrap, priceToken) {
    const hits = [];
    const walker = document.createTreeWalker(wrap, NodeFilter.SHOW_ELEMENT);
    let node = walker.currentNode;
    while (node) {
      if (!ignored(node) && classNameOf(node).includes(priceToken)) {
        const hit = nodePriceHit(node);
        if (hit) hits.push(hit);
      }
      node = walker.nextNode();
    }
    return hits;
  }

  function depthOf(node) {
    let n = 0;
    let current = node;
    while (current) {
      n += 1;
      current = current.parentElement;
    }
    return n;
  }

  function lca(left, right) {
    const seen = new Set();
    let current = left;
    while (current) {
      seen.add(current);
      current = current.parentElement;
    }
    current = right;
    while (current) {
      if (seen.has(current)) return current;
      current = current.parentElement;
    }
    return null;
  }

  function isDocumentish(node) {
    return !node || node === document.body || node === document.documentElement;
  }

  function treeDistance(left, right) {
    const ancestor = lca(left, right);
    if (!ancestor) return 1e9;
    return depthOf(left) + depthOf(right) - 2 * depthOf(ancestor);
  }

  function pairAskBidWrappers(asks, bids) {
    const asksU = uniqueNested(asks);
    const bidsU = uniqueNested(bids);
    if (!asksU.length || !bidsU.length) return [];
    if (asksU.length === 1 && bidsU.length === 1) return [[asksU[0], bidsU[0]]];
    const scored = [];
    for (const askNode of asksU) {
      for (const bidNode of bidsU) {
        const ancestor = lca(askNode, bidNode);
        scored.push({
          dist: treeDistance(askNode, bidNode),
          lcaDepth: ancestor ? depthOf(ancestor) : 0,
          ask: askNode,
          bid: bidNode,
        });
      }
    }
    scored.sort((a, b) => a.dist - b.dist || b.lcaDepth - a.lcaDepth);
    const usedAsks = new Set();
    const usedBids = new Set();
    const pairs = [];
    for (const row of scored) {
      if (usedAsks.has(row.ask) || usedBids.has(row.bid)) continue;
      pairs.push([row.ask, row.bid]);
      usedAsks.add(row.ask);
      usedBids.add(row.bid);
    }
    return pairs;
  }

  function pairNestedIn(inner, outer) {
    const container = lca(outer[0], outer[1]);
    if (isDocumentish(container)) return false;
    return container.contains(inner[0]) && container.contains(inner[1]);
  }

  function bboFromWrapperPair(askWrap, bidWrap, askToken, bidToken) {
    const asks = wrapperPricesIn(askWrap, askToken);
    const bids = wrapperPricesIn(bidWrap, bidToken);
    if (!asks.length || !bids.length) return { bid: null, ask: null, error: "missing_wrapper_bbo" };
    const bestAsk = asks.reduce((acc, hit) => (hit.value < acc.value ? hit : acc));
    const bestBid = bids.reduce((acc, hit) => (hit.value > acc.value ? hit : acc));
    if (bestBid.value >= bestAsk.value) return { bid: null, ask: null, error: "crossed_wrapper_bbo" };
    return { bid: bestBid, ask: bestAsk, error: null };
  }

  function emptyOrderbookDiagnostics() {
    return {
      orderbook_heading_count: 0,
      visible_orderbook_heading_count: 0,
      asks_wrapper_count: 0,
      visible_asks_wrapper_count: 0,
      bids_wrapper_count: 0,
      visible_bids_wrapper_count: 0,
      chosen_bbo_source: "none",
      ambiguity_reason: null,
    };
  }

  function countOrderbookPresence() {
    const spec = (catalog && catalog.live_orderbook) || {};
    const headings = labelNodes(spec.heading_labels || ["Order Book"]);
    const asks = classTokenHits(spec.asks_class_contains || "asksWrapper");
    const bids = classTokenHits(spec.bids_class_contains || "bidsWrapper");
    const diag = emptyOrderbookDiagnostics();
    diag.orderbook_heading_count = headings.length;
    diag.visible_orderbook_heading_count = headings.filter(isVisible).length;
    diag.asks_wrapper_count = asks.length;
    diag.visible_asks_wrapper_count = asks.filter(isVisible).length;
    diag.bids_wrapper_count = bids.length;
    diag.visible_bids_wrapper_count = bids.filter(isVisible).length;
    return diag;
  }

  function resolveWrapperBbo() {
    // Canonical live BBO. Sides from MEXC ask/bid wrappers; never split by last.
    const spec = catalog.live_orderbook || {};
    const askWrapToken = spec.asks_class_contains || "asksWrapper";
    const bidWrapToken = spec.bids_class_contains || "bidsWrapper";
    const askToken = spec.ask_price_class_contains || "sell";
    const bidToken = spec.bid_price_class_contains || "buy";
    const visibleAsks = classTokenHits(askWrapToken).filter(isVisible);
    const visibleBids = classTokenHits(bidWrapToken).filter(isVisible);
    if (!visibleAsks.length || !visibleBids.length) return { bid: null, ask: null, problems: [] };
    const asksU = uniqueNested(visibleAsks);
    const bidsU = uniqueNested(visibleBids);
    const pairs = pairAskBidWrappers(visibleAsks, visibleBids);
    const used = new Set();
    for (const pair of pairs) {
      used.add(pair[0]);
      used.add(pair[1]);
    }
    const leftoverPriced =
      asksU.some((node) => !used.has(node) && wrapperPricesIn(node, askToken).length) ||
      bidsU.some((node) => !used.has(node) && wrapperPricesIn(node, bidToken).length);
    if (leftoverPriced || !pairs.length) {
      return { bid: null, ask: null, problems: ["ambiguous_live_orderbook"] };
    }
    const resolved = [];
    for (const pair of pairs) {
      const bbo = bboFromWrapperPair(pair[0], pair[1], askToken, bidToken);
      if (bbo.error) return { bid: null, ask: null, problems: [bbo.error] };
      resolved.push({ pair, bid: bbo.bid, ask: bbo.ask });
    }
    const uniqueKeys = [...new Set(resolved.map((item) => `${item.bid.value}|${item.ask.value}`))];
    if (uniqueKeys.length > 1) {
      return { bid: null, ask: null, problems: ["ambiguous_live_orderbook"] };
    }
    if (resolved.length === 1) {
      return { bid: resolved[0].bid, ask: resolved[0].ask, problems: [] };
    }
    const outers = resolved.filter((candidate) => {
      return !resolved.some(
        (other) => other !== candidate && pairNestedIn(candidate.pair, other.pair)
      );
    });
    if (outers.length === 1) {
      return { bid: outers[0].bid, ask: outers[0].ask, problems: [] };
    }
    return { bid: null, ask: null, problems: ["ambiguous_live_orderbook"] };
  }

  function liveOrderBook(lastValue) {
    const spec = catalog.live_orderbook || {};
    const headings = labelNodes(spec.heading_labels || ["Order Book"]).filter(isVisible);
    if (!headings.length) return { bid: null, ask: null, problems: [] };
    const headerLabels = [
      "Fair Price",
      "Mark Price",
      "Index Price",
      "Funding Rate / Countdown",
      "Funding Rate",
      "Справедливая цена",
      "Индексная цена",
      "Ставка финансирования/Обратный отсчет",
      "Ставка финансирования",
    ];
    const band = Number(spec.price_band_frac || 0.1);
    const minSide = Number(spec.min_side_levels || 1);
    const coalesced = coalesceRoots(
      headings.map((heading) => heading.parentElement || heading),
      "ambiguous_orderbook_heading"
    );
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
        (hit) => Math.abs(hit.value - lastValue) / lastValue <= band
      );
      const asks = near.filter((hit) => hit.value > lastValue);
      const bids = near.filter((hit) => hit.value < lastValue);
      if (asks.length >= minSide && bids.length >= minSide) {
        chosen = node;
        break;
      }
      node = node.parentElement;
    }
    if (!chosen) return { bid: null, ask: null, problems: [] };
    const near = collectOwnPrices(chosen).filter(
      (hit) => Math.abs(hit.value - lastValue) / lastValue <= band
    );
    const asks = near.filter((hit) => hit.value > lastValue);
    const bids = near.filter((hit) => hit.value < lastValue);
    const bestAsk = asks.reduce((acc, hit) => (hit.value < acc.value ? hit : acc));
    const bestBid = bids.reduce((acc, hit) => (hit.value > acc.value ? hit : acc));
    if (bestBid.value >= bestAsk.value) return { bid: null, ask: null, problems: [] };
    return { bid: bestBid, ask: bestAsk, problems: [] };
  }

  function chosenBboSource(fields) {
    const bid = fields.bid;
    const ask = fields.ask;
    if (!bid || !ask) return "none";
    const ok = bid.parse_status === "ok" || bid.parse_status === "ok_redundant";
    const askOk = ask.parse_status === "ok" || ask.parse_status === "ok_redundant";
    if (!ok || !askOk) return "none";
    const bidSel = bid.selector_id || "";
    const askSel = ask.selector_id || "";
    if (bidSel === "live_asks_bids_wrapper" || askSel === "live_asks_bids_wrapper") {
      return "live_asks_bids_wrapper";
    }
    if (bidSel === "live_orderbook_split_by_last" || askSel === "live_orderbook_split_by_last") {
      return "live_orderbook_heading_fallback";
    }
    if (bidSel.startsWith("data_attr") || askSel.startsWith("data_attr")) return "data_attr";
    if (
      bidSel === "orderbook_max_bid" ||
      bidSel === "orderbook_min_ask" ||
      askSel === "orderbook_max_bid" ||
      askSel === "orderbook_min_ask"
    ) {
      return "data_attr:orderbook";
    }
    return bidSel || askSel || "none";
  }

  function ambiguityReason(problems) {
    const codes = [
      "ambiguous_live_orderbook",
      "crossed_wrapper_bbo",
      "missing_wrapper_bbo",
      "ambiguous_orderbook_heading",
    ];
    for (const code of codes) {
      if (problems.includes(code)) return code;
    }
    return null;
  }

  function symbolHint() {
    return symbolFromFuturesPath(location.pathname);
  }

  function normalizeHeaderTitle(text) {
    return collapse(text).toLowerCase().replace(/\//g, " / ").replace(/\s+/g, " ").trim();
  }

  // Keep in sync with catalog.MARKET_HEADER_FIELD_TITLE_ALIASES.
  // Used when selector_catalog JSON has no market_header aliases (stale v1).
  const DEFAULT_MARKET_HEADER_ALIASES = {
    mark: ["Fair Price", "Mark Price", "Справедливая цена"],
    index: ["Index Price", "Индексная цена"],
    funding: [
      "Funding Rate / Countdown",
      "Funding Rate/Countdown",
      "Funding Rate",
      "Ставка финансирования / Обратный отсчет",
      "Ставка финансирования/Обратный отсчет",
      "Ставка финансирования",
    ],
  };

  function headerAliasLookup() {
    const aliases = ((catalog.market_header || {}).field_title_aliases) || {};
    const source = Object.keys(aliases).length ? aliases : DEFAULT_MARKET_HEADER_ALIASES;
    const lookup = Object.create(null);
    for (const [fieldName, titles] of Object.entries(source)) {
      for (const title of titles || []) {
        lookup[normalizeHeaderTitle(title)] = fieldName;
      }
    }
    return lookup;
  }

  function fieldFromHit(name, hit, selectorId) {
    return {
      name,
      raw_text: hit.raw_text,
      value: hit.value,
      selector_id: selectorId,
      parse_status: "ok",
      match_count: 1,
      age_ms: 0,
      changed_at_monotonic_ms: null,
      unit: null,
      parser_locale: currentLocale,
      raw_tokens: hit.tokens || null,
    };
  }

  function emptyHeaderDiagnostics() {
    return {
      ui_locale: currentLocale,
      parser_mode: currentLocale,
      header_item_count: 0,
      header_title_hits_mark: 0,
      header_title_hits_index: 0,
      header_title_hits_funding: 0,
      header_alias_count: 0,
      symbol_status: "missing",
      last_status: "missing",
      mark_status: "missing",
      index_status: "missing",
      funding_status: "missing",
      symbol_selector_id: null,
      last_selector_id: null,
      mark_selector_id: null,
      index_selector_id: null,
      funding_selector_id: null,
      ambiguity_reason: null,
      market_header_probe: null,
    };
  }

  const HEADER_PROBE_LIMITS = {
    items: 12, children: 8, classTokens: 16, relevantTokens: 24,
    textTokens: 16, attributes: 8, nodes: 256,
  };
  const HEADER_PROBE_ATTRS = [
    "title", "aria-label", "aria-labelledby", "data-title",
    "data-tooltip", "data-original-title", "role",
  ];
  const HEADER_PROBE_RELEVANT = /title|content|value|label|price|rate|fair|index|fund|item/i;
  const HEADER_PROBE_PRIVATE = /account|balance|wallet|position|\borders?\b|order(?:form|panel|entry|history)|margin|asset|equity|available|api.?key|secret|credential|email|\buid\b/i;

  function capProbe(value, limit) {
    return collapse(String(value || "")).slice(0, limit);
  }

  function headerItemConfig() {
    const spec = catalog.market_header || {};
    return {
      itemToken: spec.item_class_contains || "commonItem",
      rootToken: spec.root_class_contains || "contractDetail",
      excluded: spec.item_class_exclude || ["lastPriceWrapper", "rateItem"],
    };
  }

  function isMarketHeaderItem(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return false;
    const { itemToken, rootToken, excluded } = headerItemConfig();
    const classes = classNameOf(node);
    return classes.includes(itemToken)
      && classes.includes(rootToken)
      && !excluded.some((token) => classes.includes(token));
  }

  function marketHeaderItemAncestor(node) {
    let current = node;
    while (current && current !== document) {
      if (isMarketHeaderItem(current)) return current;
      current = current.parentElement;
    }
    return null;
  }

  function probeAttributes(node) {
    const out = {};
    for (const key of HEADER_PROBE_ATTRS) {
      const value = node.getAttribute && node.getAttribute(key);
      if (value) out[key] = capProbe(value, 120);
    }
    return out;
  }

  function privateProbeNode(node) {
    const values = [classNameOf(node), ...Object.values(probeAttributes(node))];
    for (const child of node.childNodes || []) {
      if (child.nodeType === Node.TEXT_NODE) values.push(child.textContent || "");
    }
    return HEADER_PROBE_PRIVATE.test(values.join(" "));
  }

  function boundedProbeNodes(root) {
    const out = [];
    const stack = [root];
    while (stack.length && out.length < HEADER_PROBE_LIMITS.nodes) {
      const node = stack.pop();
      if (!node || node.nodeType !== Node.ELEMENT_NODE || ignored(node) || !isVisible(node)) continue;
      out.push(node);
      if (node !== root && privateProbeNode(node)) continue;
      const children = [...node.children];
      for (let index = children.length - 1; index >= 0; index -= 1) stack.push(children[index]);
    }
    return out;
  }

  function probeTextTokens(root) {
    const tokens = [];
    for (const node of boundedProbeNodes(root)) {
      if (privateProbeNode(node)) continue;
      for (const child of node.childNodes || []) {
        if (child.nodeType !== Node.TEXT_NODE) continue;
        const token = capProbe(child.textContent, 80);
        if (token) tokens.push(token);
        if (tokens.length >= HEADER_PROBE_LIMITS.textTokens) return tokens;
      }
    }
    return tokens;
  }

  function probeClassTokens(node) {
    return classNameOf(node).split(/\s+/).filter(Boolean)
      .slice(0, HEADER_PROBE_LIMITS.classTokens)
      .map((token) => capProbe(token, 80));
  }

  function probeChild(node, titleToken, valueToken) {
    if (privateProbeNode(node)) {
      return {
        tag: capProbe(node.tagName && node.tagName.toLowerCase(), 24),
        class_string: "", class_tokens: [], visible_text: "", visible_text_tokens: [],
        attributes: {}, current_title_token_matched: false,
        current_value_token_matched: false, redacted: true,
      };
    }
    const classes = classNameOf(node);
    const textTokens = probeTextTokens(node);
    return {
      tag: capProbe(node.tagName && node.tagName.toLowerCase(), 24),
      class_string: capProbe(classes, 240),
      class_tokens: probeClassTokens(node),
      visible_text: capProbe(textTokens.join(" "), 240),
      visible_text_tokens: textTokens,
      attributes: probeAttributes(node),
      current_title_token_matched: classes.includes(titleToken),
      current_value_token_matched: classes.includes(valueToken),
      redacted: false,
    };
  }

  function probeItem(item, itemIndex, titleToken, valueToken) {
    if (privateProbeNode(item)) {
      return {
        item_index: itemIndex,
        tag: capProbe(item.tagName && item.tagName.toLowerCase(), 24),
        class_string: "", class_tokens: [], direct_children: [],
        descendant_relevant_class_tokens: [], descendant_attributes: [],
        visible_text: "", visible_text_tokens: [], attributes: {},
        current_title_token_matched: false, current_value_token_matched: false,
        redacted: true,
      };
    }
    const nodes = boundedProbeNodes(item).slice(1).filter((node) => !privateProbeNode(node));
    const relevant = [];
    const descendantAttributes = [];
    for (const node of nodes) {
      for (const token of classNameOf(node).split(/\s+/).filter(Boolean)) {
        if (HEADER_PROBE_RELEVANT.test(token) && !relevant.includes(token)) {
          relevant.push(capProbe(token, 80));
          if (relevant.length >= HEADER_PROBE_LIMITS.relevantTokens) break;
        }
      }
      const attributes = probeAttributes(node);
      if (Object.keys(attributes).length && descendantAttributes.length < HEADER_PROBE_LIMITS.attributes) {
        descendantAttributes.push({
          tag: capProbe(node.tagName && node.tagName.toLowerCase(), 24), attributes,
        });
      }
    }
    const textTokens = probeTextTokens(item);
    const classes = classNameOf(item);
    return {
      item_index: itemIndex,
      tag: capProbe(item.tagName && item.tagName.toLowerCase(), 24),
      class_string: capProbe(classes, 240),
      class_tokens: probeClassTokens(item),
      direct_children: [...item.children].filter((node) => isVisible(node) && !ignored(node))
        .slice(0, HEADER_PROBE_LIMITS.children)
        .map((node) => probeChild(node, titleToken, valueToken)),
      descendant_relevant_class_tokens: relevant,
      descendant_attributes: descendantAttributes,
      visible_text: capProbe(textTokens.join(" "), 240),
      visible_text_tokens: textTokens,
      attributes: probeAttributes(item),
      current_title_token_matched: nodes.some((node) => classNameOf(node).includes(titleToken)),
      current_value_token_matched: nodes.some((node) => classNameOf(node).includes(valueToken)),
      redacted: false,
    };
  }

  function fnv1a32(text) {
    let hash = 0x811c9dc5;
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 0x01000193);
    }
    return (hash >>> 0).toString(16).padStart(8, "0");
  }

  function marketHeaderProbe(items, titleToken, valueToken) {
    const summaries = items.slice(0, HEADER_PROBE_LIMITS.items)
      .map((item, index) => probeItem(item, index, titleToken, valueToken));
    const shape = JSON.stringify(summaries).toLowerCase()
      .replace(/[-+]?\d[\d\s.,:%/:-]*/g, "<number>");
    return {
      probe_version: 1,
      structural_signature: `fnv1a32:${fnv1a32(shape)}`,
      matched_item_count: items.length,
      items_truncated: items.length > HEADER_PROBE_LIMITS.items,
      items: summaries,
    };
  }

  function extractMarketHeader() {
    const spec = catalog.market_header || {};
    const itemToken = spec.item_class_contains || "commonItem";
    const rootToken = spec.root_class_contains || "contractDetail";
    const excluded = spec.item_class_exclude || ["lastPriceWrapper", "rateItem"];
    const titleToken = spec.title_class_contains || "itemTitle";
    const valueToken = spec.value_class_contains || "itemContent";
    const lookup = headerAliasLookup();
    const grouped = { mark: [], index: [], funding: [] };
    const diag = emptyHeaderDiagnostics();
    diag.header_alias_count = Object.keys(lookup).length;
    const items = [...document.querySelectorAll("[class]")].filter((node) => {
      if (ignored(node) || !isVisible(node)) return false;
      const classes = classNameOf(node);
      if (!classes.includes(itemToken) || !classes.includes(rootToken)) return false;
      return !excluded.some((token) => classes.includes(token));
    });
    diag.header_item_count = items.length;
    diag.market_header_probe = marketHeaderProbe(items, titleToken, valueToken);
    for (const item of items) {
      const titleNodes = [...item.querySelectorAll("[class]")].filter((node) =>
        classNameOf(node).includes(titleToken)
      );
      const valueNodes = [...item.querySelectorAll("[class]")].filter((node) =>
        classNameOf(node).includes(valueToken)
      );
      const titles = titleNodes.map((node) => collapse(node.textContent)).filter(Boolean);
      const values = valueNodes.map((node) => collapse(node.textContent)).filter(Boolean);
      const title = titles.sort((a, b) => b.length - a.length)[0] || "";
      const value = values.sort((a, b) => b.length - a.length)[0] || "";
      const fieldName = lookup[normalizeHeaderTitle(title)];
      if (!fieldName || !grouped[fieldName]) continue;
      grouped[fieldName].push({ node: item, raw: value });
    }
    diag.header_title_hits_mark = grouped.mark.length;
    diag.header_title_hits_index = grouped.index.length;
    diag.header_title_hits_funding = grouped.funding.length;
    const fields = {};
    for (const [name, rows] of Object.entries(grouped)) {
      if (!rows.length) continue;
      const specField = catalog.fields[name];
      fields[name] = decode(
        name,
        specField,
        rows.map((row) => row.node),
        `header_struct:${name}`,
        rows.map((row) => row.raw)
      );
    }
    return { fields, diag };
  }

  function overlayHeaderFields(fields, headerFields) {
    for (const [name, rec] of Object.entries(headerFields)) {
      const current = fields[name];
      if (current && String(current.selector_id || "").startsWith("data_attr")) continue;
      if (!current || current.parse_status === "missing" || rec.parse_status !== "missing") {
        fields[name] = rec;
      }
    }
  }

  function finishHeaderDiagnostics(diag, fields) {
    diag.ui_locale = currentLocale;
    diag.parser_mode = currentLocale;
    for (const name of ["symbol", "last", "mark", "index", "funding"]) {
      const rec = fields[name];
      diag[`${name}_status`] = rec && rec.parse_status ? rec.parse_status : "missing";
      diag[`${name}_selector_id`] = rec && rec.selector_id ? rec.selector_id : null;
    }
    const reasons = [];
    for (const name of ["mark", "index", "funding"]) {
      if (fields[name] && fields[name].parse_status === "ambiguous") reasons.push(`ambiguous:${name}`);
    }
    diag.ambiguity_reason = reasons[0] || null;
    return diag;
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
    currentLocale = localeFromPathname(location.pathname);
    const fields = {};
    for (const [name, spec] of Object.entries(catalog.fields)) {
      fields[name] = extractField(name, spec);
    }
    const header = extractMarketHeader();
    overlayHeaderFields(fields, header.fields);
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
        parser_locale: currentLocale,
        raw_tokens: null,
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
    const diagnostics = countOrderbookPresence();
    const wrapperAvailable =
      diagnostics.visible_asks_wrapper_count > 0 && diagnostics.visible_bids_wrapper_count > 0;
    if (
      (fields.bid.parse_status === "missing" || fields.ask.parse_status === "missing") &&
      wrapperAvailable
    ) {
      const wrap = resolveWrapperBbo();
      book.problems.push(...wrap.problems);
      if (wrap.bid !== null && fields.bid.parse_status === "missing") {
        fields.bid = fieldFromHit("bid", wrap.bid, "live_asks_bids_wrapper");
      }
      if (wrap.ask !== null && fields.ask.parse_status === "missing") {
        fields.ask = fieldFromHit("ask", wrap.ask, "live_asks_bids_wrapper");
      }
    }
    if (
      (fields.bid.parse_status === "missing" || fields.ask.parse_status === "missing") &&
      !wrapperAvailable
    ) {
      const live = liveOrderBook(lastValue);
      book.problems.push(...live.problems);
      if (live.bid !== null && fields.bid.parse_status === "missing") {
        fields.bid = fieldFromHit("bid", live.bid, "live_orderbook_split_by_last");
      }
      if (live.ask !== null && fields.ask.parse_status === "missing") {
        fields.ask = fieldFromHit("ask", live.ask, "live_orderbook_split_by_last");
      }
    }
    diagnostics.chosen_bbo_source = chosenBboSource(fields);
    diagnostics.ambiguity_reason = ambiguityReason(book.problems);
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
      orderbook_diagnostics: diagnostics,
      ui_locale: currentLocale,
      parser_mode: currentLocale,
      header_diagnostics: finishHeaderDiagnostics(header.diag, fields),
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
      const probe = snapshot.header_diagnostics && snapshot.header_diagnostics.market_header_probe;
      const probeSignature = probe && probe.structural_signature ? probe.structural_signature : "";
      const probeChanged = Boolean(probeSignature && probeSignature !== lastHeaderProbeSignature);
      if (!probeChanged && snapshot.header_diagnostics) {
        snapshot.header_diagnostics.market_header_probe = null;
      }
      const key = JSON.stringify({
        bid: snapshot.fields.bid && snapshot.fields.bid.value,
        ask: snapshot.fields.ask && snapshot.fields.ask.value,
        mark: snapshot.fields.mark && snapshot.fields.mark.value,
        index: snapshot.fields.index && snapshot.fields.index.value,
        last: snapshot.fields.last && snapshot.fields.last.value,
        valid: snapshot.observation_valid,
      });
      if (trigger === "interval" && key === lastEmitKey && !probeChanged) return;
      lastEmitKey = key;
      const resp = await chrome.runtime.sendMessage({ type: "CAPTURE_SNAPSHOT", snapshot });
      if (!resp || resp.ok !== true) {
        stopLocal();
      } else if (probeChanged) {
        lastHeaderProbeSignature = probeSignature;
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
    lastHeaderProbeSignature = "";
    capturing = true;
    startObserver();
    emit("manual");
    intervalId = setInterval(() => emit("interval"), intervalMs);
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message && message.type === "CAPTURE_STATE") {
      // Ack synchronously so the popup can tell "script is present" from
      // "Receiving end does not exist" without waiting on applyState.
      sendResponse({ ok: true });
      applyState(message.state);
    }
  });

  fetch(chrome.runtime.getURL("selector_catalog_v1.json"))
    .then((response) => response.json())
    .then((payload) => {
      catalog = payload;
      chrome.storage.local.get(["capturing", "intervalMs"], applyState);
    });
})();
