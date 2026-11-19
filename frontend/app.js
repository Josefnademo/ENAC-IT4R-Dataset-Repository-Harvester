/* =========================================================================
   ENAC-IT4R Dataset & Repository Harvester - dashboard controller
   Vanilla ES2017, no build step, no external libraries.
   ========================================================================= */
"use strict";

var FALLBACK_FILTERS = [
  { key: "dataset", label: "Datasets Only", pill: "Datasets", classes: ["dataset"] },
  { key: "software", label: "Software Only", pill: "Software", classes: ["software"] },
  { key: "model_workflow", label: "Models & Workflows", pill: "Models & Workflows", classes: ["model", "workflow"] },
  { key: "other", label: "Other Research Products", pill: "Other Products", classes: ["other"] },
  { key: "all", label: "All Works", pill: "All Works", classes: ["dataset", "software", "model", "workflow", "other"] }
];

var SOURCE_LABELS = {
  merged: "Infoscience + Zenodo",
  infoscience: "Infoscience (DSpace 7)",
  zenodo: "Zenodo (EPFL Community)"
};

var state = {
  items: [],
  filters: FALLBACK_FILTERS.slice(),
  activeFilter: "dataset",
  search: "",
  dateFrom: "",
  dateTo: "",
  windowMode: "7d",
  toggles: { enac: false, code: false, funded: false, sync: false },
  lastRun: null,
  runs: [],
  logsCursor: 0,
  busy: false,
  generatedAt: null
};

var els = {};

/* In-browser preview ("surcouche"): one record, one active file at a time. */
var previewState = { item: null, entries: [], index: 0, token: 0, map: null };
var leafletPromise = null;

function $(id) {
  return document.getElementById(id);
}

/* ------------------------------------------------------------- utilities */
function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function isMergedRecord(item) {
  var keys = asArray(item && item.source_keys);
  return keys.indexOf("infoscience") !== -1 && keys.indexOf("zenodo") !== -1;
}

/** True for EPFL Infoscience handle DOIs, whose doi.org link lands on the record page. */
function isInfoscienceDoi(doi) {
  return /^10\.5075\/epfl\./i.test(String(doi || ""));
}

function isInfoscienceUrl(url) {
  return /^https?:\/\/([^/]+\.)?infoscience\.epfl\.ch(\/|$)/i.test(String(url || ""));
}

function looksLikeDoi(value) {
  return /^10\.\d{4,9}\//.test(String(value || ""));
}

function fallbackCopy(text, done) {
  try {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    var copied = document.execCommand("copy");
    document.body.removeChild(area);
    done(Boolean(copied));
  } catch (error) {
    done(false);
  }
}

function copyToClipboard(text, done) {
  if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      function () {
        done(true);
      },
      function () {
        fallbackCopy(text, done);
      }
    );
    return;
  }
  fallbackCopy(text, done);
}

function copyIdentifier(button) {
  var text = button.getAttribute("data-copy") || "";
  if (!text) {
    return;
  }
  copyToClipboard(text, function (copied) {
    button.textContent = copied ? (looksLikeDoi(text) ? "Copied DOI" : "Copied") : "Copy failed";
    button.classList.toggle("is-copied", copied);
    window.clearTimeout(button.copyTimer);
    button.copyTimer = window.setTimeout(function () {
      button.textContent = text;
      button.classList.remove("is-copied");
    }, 1400);
  });
}

/** Shared builder: every tag is a button whose tooltip and filter payload carry
    the full text, so a CSS-clamped header badge still reveals all of it. */
function tagButton(value, className) {
  var text = String(value === null || value === undefined ? "" : value);
  return '<button type="button" class="' + className + ' is-filterable" data-filter-tag="' +
    escapeHtml(text) + '" title="' + escapeHtml(text) + '">' + escapeHtml(text) + "</button>";
}

/** Every tag is rendered whole and stays clickable so it can drive the search. */
function filterableBadge(value, className, extraClass) {
  return tagButton(value, "badge " + className + (extraClass ? " " + extraClass : ""));
}

function applyTagFilter(text) {
  var value = String(text === null || text === undefined ? "" : text);
  state.search = value;
  els.search.value = value;
  render();
  els.search.classList.add("is-tag-flash");
  window.clearTimeout(applyTagFilter.timer);
  applyTagFilter.timer = window.setTimeout(function () {
    els.search.classList.remove("is-tag-flash");
  }, 900);
}

function safeUrl(value) {
  var text = String(value === null || value === undefined ? "" : value).trim();
  if (!text) {
    return "";
  }
  if (/^https?:\/\/[^\s]+$/i.test(text)) {
    return text;
  }
  return "";
}

function pad2(value) {
  return (value < 10 ? "0" : "") + value;
}

function toIsoDate(date) {
  return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate());
}

function addDays(date, days) {
  var copy = new Date(date.getTime());
  copy.setDate(copy.getDate() + days);
  return copy;
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function setStatus(text, kind) {
  els.statusText.textContent = text;
  els.statusDot.className = "status-dot" + (kind ? " is-" + kind : "");
}

function flashStatus(text, kind, timeout) {
  setStatus(text, kind || "ready");
  window.clearTimeout(flashStatus.timer);
  flashStatus.timer = window.setTimeout(function () {
    setStatus("Ready", "ready");
  }, timeout || 6000);
}

/* --------------------------------------------------------------- bootstrap */
function init() {
  els = {
    dateFrom: $("date-from"),
    dateTo: $("date-to"),
    search: $("search-input"),
    toggleEnac: $("toggle-enac"),
    toggleCode: $("toggle-code"),
    toggleFunded: $("toggle-funded"),
    toggleSync: $("toggle-sync"),
    runButton: $("run-harvest"),
    exportCsv: $("export-csv"),
    exportJson: $("export-json"),
    clearConsole: $("clear-console"),
    consoleLog: $("console-log"),
    presetGrid: $("preset-grid"),
    filterPills: $("filter-pills"),
    datasetList: $("dataset-list"),
    emptyState: $("empty-state"),
    emptyTitle: $("empty-title"),
    emptyCopy: $("empty-copy"),
    resultCount: $("result-count"),
    windowLabel: $("window-label"),
    resultsMeta: $("results-meta"),
    metricRecords: $("metric-records"),
    metricLabs: $("metric-labs"),
    metricNew: $("metric-new"),
    metricUpdated: $("metric-updated"),
    metricsHint: $("metrics-hint"),
    statusDot: $("status-dot"),
    statusText: $("status-text"),
    previewModal: $("preview-modal"),
    previewTitle: $("preview-title"),
    previewFileField: $("preview-file-field"),
    previewFile: $("preview-file"),
    previewSize: $("preview-size"),
    previewDownload: $("preview-download"),
    previewBody: $("preview-body"),
    previewClose: $("preview-close")
  };

  applyPreset("7d");
  renderFilterPills();
  bindEvents();
  appendConsoleEmpty();

  loadData();
  pollLogs();
  window.setInterval(pollLogs, 1500);
}

function bindEvents() {
  els.presetGrid.addEventListener("click", function (event) {
    var button = event.target.closest("[data-preset]");
    if (!button) {
      return;
    }
    applyPreset(button.getAttribute("data-preset"));
    render();
  });

  els.dateFrom.addEventListener("change", function () {
    state.dateFrom = els.dateFrom.value;
    state.windowMode = "custom";
    clearPresetHighlight();
    render();
  });

  els.dateTo.addEventListener("change", function () {
    state.dateTo = els.dateTo.value;
    state.windowMode = "custom";
    clearPresetHighlight();
    render();
  });

  els.search.addEventListener("input", function () {
    state.search = els.search.value.trim();
    render();
  });

  els.toggleEnac.addEventListener("change", function () {
    state.toggles.enac = els.toggleEnac.checked;
    render();
  });

  els.toggleCode.addEventListener("change", function () {
    state.toggles.code = els.toggleCode.checked;
    render();
  });

  els.toggleFunded.addEventListener("change", function () {
    state.toggles.funded = els.toggleFunded.checked;
    render();
  });

  els.toggleSync.addEventListener("change", function () {
    state.toggles.sync = els.toggleSync.checked;
    render();
  });

  els.runButton.addEventListener("click", runHarvest);
  els.exportCsv.addEventListener("click", exportCsv);
  els.exportJson.addEventListener("click", exportJsonFile);
  els.datasetList.addEventListener("click", function (event) {
    var previewTrigger = event.target.closest("[data-preview-id]");
    if (previewTrigger) {
      var previewItem = findItemByKey(previewTrigger.getAttribute("data-preview-id"));
      if (previewItem) {
        openPreview(previewItem);
      }
      return;
    }
    var copyTarget = event.target.closest("[data-copy]");
    if (copyTarget) {
      copyIdentifier(copyTarget);
      return;
    }
    var toggle = event.target.closest("[data-collapsible-toggle]");
    if (toggle) {
      toggleCollapsible(toggle);
      return;
    }
    var tag = event.target.closest("[data-filter-tag]");
    if (tag) {
      applyTagFilter(tag.getAttribute("data-filter-tag"));
    }
  });
  els.filterPills.addEventListener("click", function (event) {
    var pill = event.target.closest("[data-filter]");
    if (!pill) {
      return;
    }
    state.activeFilter = pill.getAttribute("data-filter");
    render();
  });
  if (els.previewModal) {
    els.previewModal.addEventListener("click", function (event) {
      if (event.target.closest("[data-preview-close]")) {
        closePreview();
      }
    });
    els.previewFile.addEventListener("change", function () {
      loadPreviewFile(Number(els.previewFile.value) || 0);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closePreview();
      }
    });
  }
  els.clearConsole.addEventListener("click", function () {
    els.consoleLog.innerHTML = "";
    appendConsoleEmpty();
  });
}

function applyPreset(key) {
  var button = document.querySelector('[data-preset="' + key + '"]');
  var days = button ? Number(button.getAttribute("data-days")) : 7;
  var today = new Date();

  if (days > 0) {
    state.dateFrom = toIsoDate(addDays(today, -(days - 1)));
    state.dateTo = toIsoDate(today);
  } else {
    state.dateFrom = "";
    state.dateTo = toIsoDate(today);
  }
  state.windowMode = key;

  els.dateFrom.value = state.dateFrom;
  els.dateTo.value = state.dateTo;

  var presets = els.presetGrid.querySelectorAll("[data-preset]");
  for (var i = 0; i < presets.length; i += 1) {
    presets[i].classList.toggle(
      "is-active",
      presets[i].getAttribute("data-preset") === key
    );
  }
}

function clearPresetHighlight() {
  var presets = els.presetGrid.querySelectorAll("[data-preset]");
  for (var i = 0; i < presets.length; i += 1) {
    presets[i].classList.remove("is-active");
  }
}

function windowLabel() {
  var presetLabels = {
    "7d": "Last 7 days",
    "14d": "Last 14 days",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "365d": "Last 12 months"
  };
  if (state.windowMode === "all") {
    return "All stored time";
  }
  var range = "";
  if (state.dateFrom && state.dateTo) {
    range = state.dateFrom + " \u2192 " + state.dateTo;
  } else if (state.dateTo) {
    range = "up to " + state.dateTo;
  } else if (state.dateFrom) {
    range = "from " + state.dateFrom;
  }
  var preset = presetLabels[state.windowMode];
  if (preset && range) {
    return preset + " \u00b7 " + range;
  }
  return range || "All stored time";
}

/* ------------------------------------------------------------ data loading */
function findFilter(key) {
  for (var i = 0; i < state.filters.length; i += 1) {
    if (state.filters[i].key === key) {
      return state.filters[i];
    }
  }
  return null;
}

function buildHaystack(item) {
  var parts = [
    item.title,
    item.abstract,
    item.resource_type,
    item.resource_class,
    item.source,
    item.code_repo,
    item.doi,
    item.license,
    item.access_rights,
    item.publication_date,
    item.created_date
  ];
  parts = parts.concat(
    asArray(item.authors),
    asArray(item.affiliations),
    asArray(item.parent_orgs),
    asArray(item.unit_managers),
    asArray(item.author_orcids),
    asArray(item.funding),
    asArray(item.keywords)
  );
  var files = item.file_summary || {};
  if (files.total_human) {
    parts.push(files.total_human);
  }
  parts = parts.concat(asArray(files.extensions));
  return parts.join(" ").toLowerCase();
}

function prepareItem(item) {
  var copy = Object.assign({}, item);
  copy._search = buildHaystack(copy);
  return copy;
}

function applyPayload(payload) {
  if (Array.isArray(payload.items)) {
    state.items = payload.items.map(prepareItem);
  }
  if (Array.isArray(payload.filters) && payload.filters.length) {
    state.filters = payload.filters;
  }
  if (payload.last_run) {
    state.lastRun = payload.last_run;
  } else if (payload.run) {
    state.lastRun = payload.run;
  }
  if (Array.isArray(payload.runs) && payload.runs.length) {
    state.runs = payload.runs;
  }
  state.generatedAt = payload.generated_at || new Date().toISOString();

  if (!findFilter(state.activeFilter)) {
    state.activeFilter = payload.default_filter && findFilter(payload.default_filter)
      ? payload.default_filter
      : "dataset";
  }

  render();
}

async function loadData() {
  try {
    var response = await fetch("/api/data", { cache: "no-store" });
    if (!response.ok) {
      throw new Error("HTTP " + response.status);
    }
    var payload = await response.json();
    applyPayload(payload);
    setStatus("Ready", "ready");
  } catch (error) {
    setStatus("Storage unavailable", "error");
    appendLogLine("ERROR", "Cannot read /api/data: " + error.message);
  }
}

/* ---------------------------------------------------------------- harvest */
function setBusy(busy) {
  state.busy = busy;
  els.runButton.disabled = busy;
  els.runButton.textContent = busy ? "Harvesting..." : "Run Scraper (Live API)";
}

async function runHarvest() {
  if (state.busy) {
    return;
  }
  setBusy(true);
  setStatus("Harvesting live APIs...", "busy");

  var params = new URLSearchParams();
  params.set("type", state.activeFilter);
  /* "All Time" means: as much history as the per-run page caps allow. */
  if (state.windowMode === "all" && !state.dateFrom) {
    params.set("from", "1900-01-01");
  } else if (state.dateFrom) {
    params.set("from", state.dateFrom);
  }
  params.set("to", state.dateTo || toIsoDate(new Date()));

  try {
    var response = await fetch("/api/harvest?" + params.toString(), { cache: "no-store" });
    var payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || payload.error || "HTTP " + response.status);
    }

    applyPayload(payload);

    var counts = payload.counts || {};
    var message = "Harvest complete: " + (counts.new || 0) + " new, " +
      (counts.updated || 0) + " updated, " + (counts.stored || 0) + " stored";
    flashStatus(message, "ready", 9000);
    appendLogLine("INFO", message);
  } catch (error) {
    setStatus("Harvest failed", "error");
    appendLogLine("ERROR", "Harvest failed: " + error.message);
  } finally {
    setBusy(false);
  }
}

/* ------------------------------------------------------------ live console */
function appendConsoleEmpty() {
  var empty = document.createElement("div");
  empty.className = "console-empty";
  empty.textContent = "Waiting for the first harvest...";
  els.consoleLog.appendChild(empty);
}

function removeConsoleEmpty() {
  var empty = els.consoleLog.querySelector(".console-empty");
  if (empty) {
    empty.parentNode.removeChild(empty);
  }
}

function appendLogLine(level, message) {
  appendLogLines([{ level: level, message: message, timestamp: "" }]);
}

function appendLogLines(entries) {
  var log = els.consoleLog;
  var nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  removeConsoleEmpty();

  var fragment = document.createDocumentFragment();
  for (var i = 0; i < entries.length; i += 1) {
    var entry = entries[i] || {};
    var line = document.createElement("div");
    line.className = "console-line level-" + String(entry.level || "INFO").toUpperCase();

    var time = document.createElement("span");
    time.className = "console-time";
    time.textContent = entry.timestamp || "";

    var message = document.createElement("span");
    message.className = "console-msg";
    message.textContent = entry.message || "";

    line.appendChild(time);
    line.appendChild(message);
    fragment.appendChild(line);
  }
  log.appendChild(fragment);

  while (log.children.length > 400) {
    log.removeChild(log.firstChild);
  }
  if (nearBottom) {
    log.scrollTop = log.scrollHeight;
  }
}

async function pollLogs() {
  try {
    var response = await fetch("/api/logs?since=" + state.logsCursor, { cache: "no-store" });
    if (!response.ok) {
      return;
    }
    var payload = await response.json();
    if (typeof payload.cursor === "number") {
      state.logsCursor = payload.cursor;
    }
    var entries = asArray(payload.entries);
    if (entries.length) {
      appendLogLines(entries);
    }
  } catch (error) {
    /* the console simply keeps polling */
  }
}

/* -------------------------------------------------------------- filtering */
function inWindow(dateIso) {
  if (state.windowMode === "all" && !state.dateFrom) {
    return true;
  }
  if (!state.dateFrom && !state.dateTo) {
    return true;
  }
  if (!dateIso) {
    return true;
  }
  if (state.dateFrom && dateIso < state.dateFrom) {
    return false;
  }
  if (state.dateTo && dateIso > state.dateTo) {
    return false;
  }
  return true;
}

function hasEnacUnit(item) {
  var orgs = asArray(item.parent_orgs);
  for (var i = 0; i < orgs.length; i += 1) {
    if (String(orgs[i]).toUpperCase().indexOf("ENAC") !== -1) {
      return true;
    }
  }
  return false;
}

function filterItems(options) {
  var ignoreClass = Boolean(options && options.ignoreClass);
  var spec = findFilter(state.activeFilter);
  var classes = spec ? asArray(spec.classes) : [];
  var query = state.search.toLowerCase();

  return state.items.filter(function (item) {
    if (!ignoreClass && classes.length && classes.indexOf(item.resource_class) === -1) {
      return false;
    }
    if (!inWindow(item.window_date)) {
      return false;
    }
    if (state.toggles.enac && !hasEnacUnit(item)) {
      return false;
    }
    if (state.toggles.code && !item.code_repo) {
      return false;
    }
    if (state.toggles.funded && asArray(item.funding).length === 0) {
      return false;
    }
    if (
      state.toggles.sync &&
      item.sync_status !== "new" &&
      item.sync_status !== "updated"
    ) {
      return false;
    }
    if (query && String(item._search || "").indexOf(query) === -1) {
      return false;
    }
    return true;
  });
}

function sourceLabel(item) {
  var keys = asArray(item.source_keys);
  var hasInfoscience = keys.indexOf("infoscience") !== -1;
  var hasZenodo = keys.indexOf("zenodo") !== -1;
  if (hasInfoscience && hasZenodo) {
    return SOURCE_LABELS.merged;
  }
  if (hasInfoscience) {
    return SOURCE_LABELS.infoscience;
  }
  if (hasZenodo) {
    return SOURCE_LABELS.zenodo;
  }
  return item.source || "Unknown repository";
}

/* --------------------------------------------------------------- rendering */
function render() {
  var items = filterItems();
  renderMetrics(items);
  renderResultsBar(items);
  renderFilterPills();
  renderCards(items);
}

function renderMetrics(items) {
  var labs = {};
  var labCount = 0;
  for (var i = 0; i < items.length; i += 1) {
    var affiliations = asArray(items[i].affiliations);
    for (var j = 0; j < affiliations.length; j += 1) {
      var key = String(affiliations[j]).toUpperCase();
      if (!labs[key]) {
        labs[key] = true;
        labCount += 1;
      }
    }
  }

  var run = state.lastRun || {};
  els.metricRecords.textContent = String(items.length);
  els.metricLabs.textContent = String(labCount);
  els.metricNew.textContent = String(run.new_count || 0);
  els.metricUpdated.textContent = String(run.updated_count || 0);

  if (run.run_at) {
    els.metricsHint.textContent =
      "Last run " + run.run_at.replace("T", " ").replace("+00:00", " UTC") +
      " \u00b7 " + (run.date_from || "?") + " \u2192 " + (run.date_to || "?") +
      " \u00b7 " + (run.total_records || 0) + " records stored";
  } else {
    els.metricsHint.textContent = "No harvest yet in this session.";
  }
}

function renderResultsBar(items) {
  els.resultCount.textContent = String(items.length);
  els.windowLabel.textContent = windowLabel();

  var zenodoCount = 0;
  var infoscienceCount = 0;
  var mergedCount = 0;
  for (var i = 0; i < items.length; i += 1) {
    var keys = asArray(items[i].source_keys);
    var hasZenodo = keys.indexOf("zenodo") !== -1;
    var hasInfoscience = keys.indexOf("infoscience") !== -1;
    if (hasZenodo && hasInfoscience) {
      mergedCount += 1;
    }
    if (hasZenodo) {
      zenodoCount += 1;
    }
    if (hasInfoscience) {
      infoscienceCount += 1;
    }
  }

  var stored = state.items.length;
  var parts = [
    "Zenodo " + zenodoCount,
    "Infoscience " + infoscienceCount,
    "merged " + mergedCount,
    "stored " + stored
  ];
  if (state.generatedAt) {
    parts.push("snapshot " + String(state.generatedAt).slice(0, 16).replace("T", " "));
  }
  els.resultsMeta.textContent = parts.join(" \u00b7 ");
}

function renderFilterPills() {
  var base = filterItems({ ignoreClass: true });
  var html = "";

  for (var i = 0; i < state.filters.length; i += 1) {
    var spec = state.filters[i];
    var classes = asArray(spec.classes);
    var count = 0;
    for (var j = 0; j < base.length; j += 1) {
      if (classes.indexOf(base[j].resource_class) !== -1) {
        count += 1;
      }
    }
    var active = spec.key === state.activeFilter;
    html += '<button type="button" class="filter-pill' + (active ? " is-active" : "") +
      '" data-filter="' + escapeHtml(spec.key) + '" role="tab" aria-selected="' +
      (active ? "true" : "false") + '" title="' + escapeHtml(spec.description || spec.label) +
      '">' + escapeHtml(spec.pill || spec.label) +
      ' <span class="pill-count">' + count + "</span></button>";
  }
  els.filterPills.innerHTML = html;
}

function metaRow(label, valueHtml) {
  return '<div class="meta-row">' +
    '<span class="meta-label">' + escapeHtml(label) + "</span>" +
    valueHtml +
    "</div>";
}

function mutedValue(text) {
  return '<span class="meta-value is-muted">' + escapeHtml(text) + "</span>";
}

/* ------------------------------------------------------ collapsible content */
/* One contract for every overflow: a `.collapsible` wrapper whose body is
   swapped in place (prose) or whose hidden tail is revealed (lists), driven by
   the single `[data-collapsible-toggle]` pill. A field never mixes controls:
   prose gets "Show more", lists get "+ N more". */
var ABSTRACT_CHAR_LIMIT = 280;     /* prose preview on the card */
var SINGLE_VALUE_CHAR_LIMIT = 160; /* one huge affiliation becomes prose */

function collapsiblePill(moreLabel, lessLabel, extraAttributes) {
  return '<button type="button" class="inline-expand-btn" data-collapsible-toggle' +
    ' aria-expanded="false"' +
    ' data-more-label="' + escapeHtml(moreLabel) + '"' +
    ' data-less-label="' + escapeHtml(lessLabel) + '"' +
    (extraAttributes || "") + ">" + escapeHtml(moreLabel) + "</button>";
}

/** Clip prose at `charLimit` behind a Show more / Show less pill. */
function renderCollapsibleText(text, charLimit) {
  var value = String(text === null || text === undefined ? "" : text);
  if (value.length <= charLimit) {
    return escapeHtml(value);
  }
  var shortText = value.slice(0, charLimit).replace(/\s+$/, "") + "\u2026";
  return '<span class="collapsible" data-collapsible>' +
    '<span data-collapsible-body>' + escapeHtml(shortText) + "</span> " +
    collapsiblePill(
      "Show more",
      "Show less",
      ' data-short="' + escapeHtml(shortText) + '"' +
        ' data-full="' + escapeHtml(value) + '"'
    ) +
  "</span>";
}

/** Join items, hiding the tail once `maxItems` or the row budget is exceeded. */
function renderCollapsibleItems(items, options) {
  var settings = options || {};
  var textOf =
    settings.textOf ||
    function (value) {
      return String(value === null || value === undefined ? "" : value);
    };
  var list = asArray(items).filter(function (value) {
    return textOf(value).trim() !== "";
  });
  if (!list.length) {
    return "";
  }
  var maxItems = settings.maxItems || list.length;
  var charLimit = settings.charLimit || Infinity;
  var separator = settings.separator === undefined ? " \u00b7 " : settings.separator;
  /* Every rendered tag also spends its own padding and the flex gap, so a badge
     or chip row is budgeted in row characters instead of item count alone. */
  var itemOverhead = settings.itemOverhead || 0;
  var renderItem = settings.renderItem || escapeHtml;
  var shown = [];
  var hidden = [];
  var used = 0;

  for (var i = 0; i < list.length; i += 1) {
    var cost = textOf(list[i]).length + separator.length + itemOverhead;
    /* The first item always shows, so a lone value never hides behind its own
       "+ 1 more"; the overflow pill only ever counts genuinely hidden items. */
    var fits = shown.length === 0 || (shown.length < maxItems && used + cost <= charLimit);
    if (!hidden.length && fits) {
      shown.push(renderItem(list[i]));
      used += cost;
    } else {
      hidden.push(renderItem(list[i]));
    }
  }
  if (!hidden.length) {
    return shown.join(separator);
  }
  return '<span class="collapsible" data-collapsible>' +
    shown.join(separator) +
    '<span class="collapsible-extra" data-collapsible-hidden hidden>' +
      (shown.length ? separator : "") + hidden.join(separator) +
    "</span>" + separator +
    collapsiblePill("+ " + hidden.length + " more", "Show fewer") +
  "</span>";
}

/** Single handler: swap the text body or reveal the hidden tail, then relabel. */
function toggleCollapsible(button) {
  var wrapper = button.closest("[data-collapsible]");
  if (!wrapper) {
    return;
  }
  var expanded = button.getAttribute("aria-expanded") === "true";
  if (button.hasAttribute("data-full")) {
    var body = wrapper.querySelector("[data-collapsible-body]");
    if (body) {
      body.textContent = button.getAttribute(expanded ? "data-short" : "data-full") || "";
    }
  }
  var hidden = wrapper.querySelectorAll("[data-collapsible-hidden]");
  for (var i = 0; i < hidden.length; i += 1) {
    hidden[i].hidden = expanded;
  }
  button.setAttribute("aria-expanded", expanded ? "false" : "true");
  button.textContent =
    button.getAttribute(expanded ? "data-more-label" : "data-less-label") || "";
}

/* ------------------------------------------------- lists, tags and badges */
/** Meta row list: three entries, one single "+ N more" pill. A row that holds
    one over-long value (a 400-character Zenodo affiliation) is prose instead,
    so it shows "Show more" — never both controls at once. */
function metaList(values, emptyText) {
  var list = asArray(values);
  if (!list.length) {
    return mutedValue(emptyText);
  }
  var single = String(list[0] === null || list[0] === undefined ? "" : list[0]);
  if (list.length === 1 && single.length > SINGLE_VALUE_CHAR_LIMIT) {
    return '<span class="meta-value">' +
      renderCollapsibleText(single, SINGLE_VALUE_CHAR_LIMIT) + "</span>";
  }
  return '<span class="meta-value">' +
    renderCollapsibleItems(list, { maxItems: 3, charLimit: 180 }) +
  "</span>";
}

/** Header row: affiliations then ENAC units, budgeted by row width. A full row —
    an institution name plus its acronyms — never collapses, and each badge shows
    its whole text (wrapping naturally) with the same text in the tooltip and in
    the click-to-filter payload. */
function renderHeaderBadges(item) {
  var badges = [];
  var affiliations = asArray(item.affiliations);
  var orgs = asArray(item.parent_orgs);
  var i;
  for (i = 0; i < affiliations.length; i += 1) {
    badges.push({ text: affiliations[i], className: "badge-lab" });
  }
  for (i = 0; i < orgs.length; i += 1) {
    badges.push({ text: orgs[i], className: "badge-org" });
  }
  if (!badges.length) {
    return "";
  }
  return renderCollapsibleItems(badges, {
    maxItems: 8,
    charLimit: 115,
    itemOverhead: 4,
    separator: "",
    textOf: function (badge) {
      return String(badge.text);
    },
    renderItem: function (badge) {
      return filterableBadge(badge.text, badge.className);
    }
  });
}

function renderFunding(item) {
  var list = asArray(item.funding);
  if (!list.length) {
    return mutedValue("No funding metadata");
  }
  return '<span class="badge-row">' +
    renderCollapsibleItems(list, {
      maxItems: 4,
      charLimit: 200,
      separator: "",
      renderItem: function (label) {
        var isOrd = /open research data|ord\b|flagship|programme/i.test(String(label));
        return filterableBadge(label, "badge-grant", isOrd ? "is-ord" : "");
      }
    }) +
  "</span>";
}

/** Bottom keyword row: chips fill the row before one "+ N more" pill appears. */
function renderKeywords(item) {
  return renderCollapsibleItems(item.keywords, {
    maxItems: 12,
    charLimit: 125,
    itemOverhead: 4,
    separator: "",
    renderItem: function (value) {
      return tagButton(value, "keyword-chip");
    }
  });
}

function renderFiles(item) {
  var files = item.file_summary || {};
  var count = Number(files.count || 0);
  if (!count) {
    return mutedValue("File metadata not exposed");
  }
  var bits = [count + (count === 1 ? " file" : " files")];
  if (files.total_human) {
    bits.push(files.total_human);
  }
  var extensions = asArray(files.extensions);
  if (extensions.length) {
    bits.push("." + extensions.join(", ."));
  }
  return '<span class="meta-value">' + escapeHtml(bits.join(" \u00b7 ")) + "</span>";
}

function renderCodeRepo(item) {
  var url = safeUrl(item.code_repo);
  if (!url) {
    return mutedValue("No linked repository");
  }
  return '<span class="meta-value"><a href="' + escapeHtml(url) +
    '" target="_blank" rel="noopener noreferrer">' +
    escapeHtml(String(item.code_repo).replace(/^https?:\/\//, "")) +
    "</a></span>";
}

function renderAccess(item) {
  var bits = [];
  if (item.access_rights) {
    bits.push(String(item.access_rights));
  }
  var licenseUrl = safeUrl(item.license);
  if (licenseUrl) {
    bits.push(licenseUrl.replace(/^https?:\/\/(www\.)?/, ""));
  } else if (item.license) {
    bits.push(String(item.license));
  }
  if (!bits.length) {
    return mutedValue("Not published");
  }
  return '<span class="meta-value">' + escapeHtml(bits.join(" \u00b7 ")) + "</span>";
}

function renderDates(item) {
  var bits = [];
  if (item.publication_date) {
    bits.push("Published " + item.publication_date);
  }
  if (item.created_date && item.created_date !== item.publication_date) {
    bits.push("Indexed " + item.created_date);
  }
  if (!bits.length) {
    return mutedValue("Date not published");
  }
  return '<span class="meta-value">' + escapeHtml(bits.join(" \u00b7 ")) + "</span>";
}


/* Consortium papers list 100+ authors: six inline, then "+ N more". The middle
   dot separator keeps each "Lastname, Firstname" pair readable. */
function renderAuthors(item) {
  var authors = asArray(item.authors);
  if (!authors.length) {
    return mutedValue("Not listed");
  }
  return '<span class="meta-value">' +
    renderCollapsibleItems(authors, { maxItems: 6, separator: " \u00b7 " }) +
  "</span>";
}

/** Card footer trigger: only exists when the record reports files to inspect. */
function renderPreviewTrigger(item) {
  if (!canPreview(item)) {
    return "";
  }
  return '<button type="button" class="preview-link" data-preview-id="' +
    escapeHtml(previewKey(item)) +
    '" title="Inspect the record files in the browser without downloading them">' +
    "Preview Data</button>";
}

function renderCard(item) {
  var type = item.resource_type || item.resource_class || "Other";
  var syncBadge = "";
  if (item.sync_status === "new") {
    syncBadge = '<span class="badge badge-new">NEW</span>';
  } else if (item.sync_status === "updated") {
    syncBadge = '<span class="badge badge-updated">UPDATED</span>';
  }

  var recordUrl = safeUrl(item.url);
  var doiUrl = item.doi ? safeUrl("https://doi.org/" + item.doi) : "";
  var codeUrl = safeUrl(item.code_repo);

  var merged = isMergedRecord(item);
  var identifier = String(item.doi || item.canonical_id || item.source_id || "");
  /* A 10.5075/epfl.* handle DOI resolves to the same page the Record link opens. */
  var doiIsDuplicate = !merged && isInfoscienceDoi(item.doi) && isInfoscienceUrl(recordUrl);
  var zenodoUrl = merged ? doiUrl : "";

  var keywordHtml = renderKeywords(item);

  var abstract = item.abstract
    ? '<p class="card-abstract">' +
      renderCollapsibleText(item.abstract, ABSTRACT_CHAR_LIMIT) + "</p>"
    : "";

  return '<article class="dataset-card' +
    (item.sync_status === "new" ? " is-new" : "") +
    (item.sync_status === "updated" ? " is-updated" : "") + '">' +
    '<div class="card-badges">' +
      '<span class="badge badge-type">' + escapeHtml(type) + "</span>" +
      '<span class="badge badge-source">' + escapeHtml(sourceLabel(item)) + "</span>" +
      renderHeaderBadges(item) +
      syncBadge +
    "</div>" +
    '<h3 class="card-title">' +
      (recordUrl
        ? '<a href="' + escapeHtml(recordUrl) + '" target="_blank" rel="noopener noreferrer">' +
          escapeHtml(item.title || "Untitled record") + "</a>"
        : escapeHtml(item.title || "Untitled record")) +
    "</h3>" +
    '<div class="card-meta">' +
      metaRow("Authors", renderAuthors(item)) +
      metaRow("PI / Lab head", metaList(item.unit_managers, "Not published")) +
      metaRow("EPFL labs", metaList(item.affiliations, "Not mapped to an EPFL unit")) +
      metaRow("ENAC units", metaList(item.parent_orgs, "Not reported")) +
      metaRow("Funding", renderFunding(item)) +
    "</div>" +
    abstract +
    '<div class="card-details">' +
      '<div class="detail"><span class="detail-label">Date</span>' +
        renderDates(item) + "</div>" +
      '<div class="detail"><span class="detail-label">Access / licence</span>' +
        renderAccess(item) + "</div>" +
      '<div class="detail"><span class="detail-label">Files</span>' +
        renderFiles(item) + "</div>" +
      '<div class="detail"><span class="detail-label">Code</span>' +
        renderCodeRepo(item) + "</div>" +
    "</div>" +
    (keywordHtml ? '<div class="card-keywords">' + keywordHtml + "</div>" : "") +
    '<div class="card-footer">' +
      (identifier
        ? '<button type="button" class="card-id" data-copy="' + escapeHtml(identifier) +
          '" title="' +
          (looksLikeDoi(identifier) ? "Click to copy the DOI" : "Click to copy the identifier") +
          '">' + escapeHtml(identifier) + "</button>"
        : "") +
      '<span class="card-links">' +
        renderPreviewTrigger(item) +
        (recordUrl
          ? '<a href="' + escapeHtml(recordUrl) +
            '" target="_blank" rel="noopener noreferrer" title="' +
            (merged ? "Infoscience record" : "Record page") + '">Record</a>'
          : "") +
        (doiUrl && !doiIsDuplicate && !merged
          ? '<a href="' + escapeHtml(doiUrl) +
            '" target="_blank" rel="noopener noreferrer" title="Resolve the DOI">DOI</a>'
          : "") +
        (zenodoUrl
          ? '<a href="' + escapeHtml(zenodoUrl) +
            '" target="_blank" rel="noopener noreferrer" title="Zenodo record (DOI resolver)">Zenodo</a>'
          : "") +
        (codeUrl
          ? '<a href="' + escapeHtml(codeUrl) +
            '" target="_blank" rel="noopener noreferrer">Repository</a>'
          : "") +
      "</span>" +
    "</div>" +
    "</article>";
}


function renderCards(items) {
  if (!items.length) {
    els.datasetList.innerHTML = "";
    els.emptyState.hidden = false;
    if (!state.items.length) {
      els.emptyTitle.textContent = "No harvested records yet";
      els.emptyCopy.textContent =
        "Pick a window on the left and run the scraper to pull live ENAC research " +
        "outputs from Infoscience and the Zenodo EPFL community.";
    } else {
      els.emptyTitle.textContent = "No records match the current filters";
      els.emptyCopy.textContent =
        "Widen the date window, clear the search box, or switch off one of the " +
        "refinement toggles to see more records.";
    }
    return;
  }

  els.emptyState.hidden = true;
  var html = "";
  for (var i = 0; i < items.length; i += 1) {
    html += renderCard(items[i]);
  }
  els.datasetList.innerHTML = html;
}

/* ----------------------------------------------------------------- exports */
var CSV_COLUMNS = [
  ["canonical_id", function (item) { return item.canonical_id; }],
  ["title", function (item) { return item.title; }],
  ["resource_type", function (item) { return item.resource_type; }],
  ["resource_class", function (item) { return item.resource_class; }],
  ["source", function (item) { return item.source; }],
  ["sync_status", function (item) { return item.sync_status; }],
  ["doi", function (item) { return item.doi; }],
  ["publication_date", function (item) { return item.publication_date; }],
  ["created_date", function (item) { return item.created_date; }],
  ["window_date", function (item) { return item.window_date; }],
  ["authors", function (item) { return asArray(item.authors); }],
  ["unit_managers", function (item) { return asArray(item.unit_managers); }],
  ["affiliations", function (item) { return asArray(item.affiliations); }],
  ["parent_orgs", function (item) { return asArray(item.parent_orgs); }],
  ["funding", function (item) { return asArray(item.funding); }],
  ["keywords", function (item) { return asArray(item.keywords); }],
  ["license", function (item) { return item.license; }],
  ["access_rights", function (item) { return item.access_rights; }],
  ["code_repo", function (item) { return item.code_repo; }],
  ["url", function (item) { return item.url; }],
  ["file_count", function (item) {
    return (item.file_summary || {}).count || 0;
  }],
  ["file_size", function (item) {
    return (item.file_summary || {}).total_human || "";
  }],
  ["file_extensions", function (item) {
    return asArray((item.file_summary || {}).extensions);
  }]
];

function csvEscape(value) {
  if (value === null || value === undefined) {
    return "";
  }
  var text = Array.isArray(value) ? value.join("; ") : String(value);
  text = text.replace(/\r?\n/g, " ").trim();
  if (/[",]/.test(text)) {
    text = '"' + text.replace(/"/g, '""') + '"';
  }
  return text;
}

function exportStamp() {
  var now = new Date();
  return toIsoDate(now) + "-" + pad2(now.getHours()) + pad2(now.getMinutes());
}

function downloadFile(filename, content, mimeType) {
  var blob = new Blob([content], { type: mimeType + ";charset=utf-8" });
  var url = URL.createObjectURL(blob);
  var link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  window.setTimeout(function () {
    URL.revokeObjectURL(url);
  }, 1500);
}

function exportCsv() {
  var rows = filterItems();
  if (!rows.length) {
    flashStatus("Nothing to export", "warn", 5000);
    return;
  }

  var lines = [CSV_COLUMNS.map(function (column) { return csvEscape(column[0]); }).join(",")];
  for (var i = 0; i < rows.length; i += 1) {
    var cells = CSV_COLUMNS.map(function (column) {
      return csvEscape(column[1](rows[i]));
    });
    lines.push(cells.join(","));
  }

  downloadFile("enac-harvest-" + exportStamp() + ".csv", lines.join("\r\n"), "text/csv");
  appendLogLine("INFO", "CSV export: " + rows.length + " rows");
  flashStatus("CSV exported (" + rows.length + " rows)", "ready", 6000);
}

function exportJsonFile() {
  var rows = filterItems();
  if (!rows.length) {
    flashStatus("Nothing to export", "warn", 5000);
    return;
  }

  var items = rows.map(function (item) {
    var copy = Object.assign({}, item);
    delete copy._search;
    return copy;
  });

  var payload = {
    generated_at: new Date().toISOString(),
    filter: state.activeFilter,
    window: {
      mode: state.windowMode,
      date_from: state.dateFrom || null,
      date_to: state.dateTo || null
    },
    toggles: state.toggles,
    count: items.length,
    items: items
  };

  downloadFile(
    "enac-harvest-" + exportStamp() + ".json",
    JSON.stringify(payload, null, 2),
    "application/json"
  );
  appendLogLine("INFO", "JSON export: " + items.length + " records");
  flashStatus("JSON exported (" + items.length + " records)", "ready", 6000);
}

/* ------------------------------------------------------------- preview */
/** Stable record key used by the card trigger (canonical_id is always set). */
function previewKey(item) {
  return String((item && (item.canonical_id || item.source_id || item.url)) || "");
}

/** Files that carry a direct download URL and can therefore be previewed. */
function previewEntries(item) {
  var files = (item && item.file_summary) || {};
  var entries = asArray(files.entries);
  var usable = [];
  for (var i = 0; i < entries.length; i += 1) {
    if (entries[i] && safeUrl(entries[i].url)) {
      usable.push(entries[i]);
    }
  }
  return usable;
}

/** Older cached rows only know the file count; they still open the overlay. */
function canPreview(item) {
  var files = (item && item.file_summary) || {};
  return previewEntries(item).length > 0 || Number(files.count || 0) > 0;
}

function findItemByKey(key) {
  for (var i = 0; i < state.items.length; i += 1) {
    if (previewKey(state.items[i]) === key) {
      return state.items[i];
    }
  }
  return null;
}

/** Shared by the cached and the on-demand flow: fill the file ``<select>``. */
function populatePreviewSelect(entries) {
  els.previewFile.innerHTML = "";
  for (var i = 0; i < entries.length; i += 1) {
    var option = document.createElement("option");
    option.value = String(i);
    option.textContent = entries[i].key +
      (entries[i].size_human ? " \u00b7 " + entries[i].size_human : "");
    els.previewFile.appendChild(option);
  }
  els.previewFile.disabled = entries.length < 2;
  els.previewFileField.hidden = entries.length === 0;
}

/** Cache a resolved inventory in memory so reopening the modal is instant. */
function cachePreviewInventory(item, fileSummary) {
  item.file_summary = fileSummary;
  var key = previewKey(item);
  for (var i = 0; i < state.items.length; i += 1) {
    if (state.items[i] !== item && previewKey(state.items[i]) === key) {
      state.items[i].file_summary = fileSummary;
    }
  }
}

async function openPreview(item) {
  if (!item || !els.previewModal) {
    return;
  }
  var entries = previewEntries(item);
  previewState.item = item;
  previewState.entries = entries;
  previewState.index = 0;
  var token = (previewState.token += 1);
  destroyPreviewMap();

  els.previewTitle.textContent = item.title || "Untitled record";
  els.previewModal.hidden = false;
  document.body.classList.add("preview-open");
  els.previewSize.textContent = "";
  els.previewDownload.hidden = true;
  populatePreviewSelect(entries);
  els.previewClose.focus();

  if (entries.length) {
    loadPreviewFile(0);
    return;
  }

  /* Rows harvested before the preview feature only know the file count: ask
     the backend to resolve the inventory and cache it in SQLite. */
  els.previewBody.innerHTML =
    '<div class="preview-loading">Loading file list from repository\u2026</div>';
  try {
    var response = await fetch(
      "/api/record-files?id=" + encodeURIComponent(previewKey(item)),
      { cache: "no-store" }
    );
    var payload = await response.json();
    if (token !== previewState.token) {
      return;
    }
    if (!response.ok) {
      throw new Error(payload.detail || payload.error || "HTTP " + response.status);
    }
    cachePreviewInventory(item, payload.file_summary || {});
    entries = previewEntries(item);
    previewState.entries = entries;
    populatePreviewSelect(entries);
    if (!entries.length) {
      els.previewBody.innerHTML =
        '<div class="preview-notice">No downloadable files are attached to this record.</div>';
      return;
    }
    loadPreviewFile(0);
  } catch (error) {
    if (token !== previewState.token) {
      return;
    }
    els.previewBody.innerHTML =
      '<div class="preview-notice is-error">Cannot load the file list: ' +
      escapeHtml(error.message) + "</div>";
  }
}

function closePreview() {
  if (!els.previewModal || els.previewModal.hidden) {
    return;
  }
  previewState.token += 1;
  previewState.item = null;
  previewState.entries = [];
  destroyPreviewMap();
  els.previewModal.hidden = true;
  els.previewBody.innerHTML = "";
  document.body.classList.remove("preview-open");
}

async function loadPreviewFile(index) {
  var entries = previewState.entries;
  if (!entries.length) {
    return;
  }
  if (index < 0 || index >= entries.length) {
    index = 0;
  }
  var entry = entries[index];
  var url = safeUrl(entry.url);
  var token = (previewState.token += 1);
  previewState.index = index;
  destroyPreviewMap();

  els.previewFile.value = String(index);
  els.previewSize.textContent = entry.size_human || "";
  els.previewDownload.href = url || "#";
  els.previewDownload.hidden = !url;
  els.previewBody.innerHTML = '<div class="preview-loading">Fetching ' +
    escapeHtml(entry.key) + "\u2026</div>";

  var params = new URLSearchParams();
  params.set("url", entry.url);
  params.set("name", entry.key || "");
  try {
    var response = await fetch("/api/preview?" + params.toString(), { cache: "no-store" });
    var payload = await response.json();
    if (token !== previewState.token) {
      return;
    }
    if (!response.ok) {
      throw new Error(payload.detail || payload.error || "HTTP " + response.status);
    }
    renderPreviewPayload(payload, entry);
  } catch (error) {
    if (token !== previewState.token) {
      return;
    }
    els.previewBody.innerHTML = '<div class="preview-notice is-error">Preview unavailable: ' +
      escapeHtml(error.message) + "</div>" + renderPreviewDownloadHint(entry);
  }
}

function renderPreviewDownloadHint(entry) {
  var url = safeUrl(entry && entry.url);
  if (!url) {
    return "";
  }
  return '<p class="preview-status"><a href="' + escapeHtml(url) +
    '" target="_blank" rel="noopener noreferrer">Download the original file</a> ' +
    "to inspect it locally.</p>";
}

function prettyJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (error) {
    return String(value);
  }
}

function renderPreviewPayload(payload, entry) {
  var kind = String((payload && payload.kind) || "unsupported");
  if (kind === "table") {
    els.previewBody.innerHTML = renderPreviewTable(payload, entry);
    bindPreviewTableFilter();
  } else if (kind === "geojson") {
    els.previewBody.innerHTML = renderPreviewGeojsonShell(payload);
    mountPreviewMap(payload.geojson);
  } else if (kind === "zip") {
    els.previewBody.innerHTML = renderPreviewZip(payload);
  } else if (kind === "json" || kind === "text") {
    els.previewBody.innerHTML = renderPreviewCode(payload);
  } else if (kind === "image") {
    els.previewBody.innerHTML = renderPreviewImage(payload, entry);
  } else {
    els.previewBody.innerHTML = renderPreviewUnsupported(payload, entry);
  }
}

/** CSV/TSV: sticky header, row numbers, client-side row filter, 100-row cap. */
function renderPreviewTable(payload, entry) {
  var columns = asArray(payload.columns);
  var rows = asArray(payload.rows);
  if (!columns.length) {
    return '<div class="preview-banner">The file is empty or has no readable header row.</div>' +
      renderPreviewDownloadHint(entry);
  }

  var head = '<th class="preview-row-num">#</th>';
  for (var c = 0; c < columns.length; c += 1) {
    head += "<th>" + escapeHtml(columns[c]) + "</th>";
  }

  var body = "";
  for (var r = 0; r < rows.length; r += 1) {
    var cells = "";
    var haystack = [];
    for (var k = 0; k < columns.length; k += 1) {
      var value = rows[r][k] === undefined || rows[r][k] === null ? "" : rows[r][k];
      haystack.push(String(value));
      cells += '<td title="' + escapeHtml(value) + '">' + escapeHtml(value) + "</td>";
    }
    body += '<tr data-preview-row data-search="' +
      escapeHtml(haystack.join(" ").toLowerCase()) + '">' +
      '<td class="preview-row-num">' + (r + 1) + "</td>" + cells + "</tr>";
  }

  var banner = payload.truncated
    ? "Preview limited to the first 256 KB and at most 100 rows. Download the original file for the complete dataset."
    : "Showing all " + rows.length + " row(s).";

  return '<div class="preview-banner">' + escapeHtml(banner) + "</div>" +
    '<div class="preview-tools">' +
      '<input type="search" id="preview-row-filter" class="preview-filter-input" ' +
        'placeholder="Filter rows\u2026" aria-label="Filter table rows">' +
      '<span class="preview-filter-count" id="preview-row-count"></span>' +
    "</div>" +
    '<div class="preview-table-wrap"><table class="preview-table">' +
      "<thead><tr>" + head + "</tr></thead><tbody>" +
      (body || '<tr><td class="preview-empty" colspan="' + (columns.length + 1) +
        '">No data rows</td></tr>') +
      "</tbody></table></div>";
}

function bindPreviewTableFilter() {
  var input = $("preview-row-filter");
  var count = $("preview-row-count");
  var rows = els.previewBody.querySelectorAll("[data-preview-row]");
  if (!rows.length) {
    return;
  }
  var apply = function () {
    var query = input ? input.value.trim().toLowerCase() : "";
    var shown = 0;
    for (var i = 0; i < rows.length; i += 1) {
      var match = !query ||
        (rows[i].getAttribute("data-search") || "").indexOf(query) !== -1;
      rows[i].hidden = !match;
      if (match) {
        shown += 1;
      }
    }
    if (count) {
      count.textContent = shown + " / " + rows.length + " rows";
    }
  };
  if (input) {
    input.addEventListener("input", apply);
  }
  apply();
}

/** Remote ZIP: the internal structure, read from the central directory only. */
function renderPreviewZip(payload) {
  var entries = asArray(payload.entries);
  var total = Number(payload.entry_count || entries.length);
  var rows = "";
  for (var i = 0; i < entries.length; i += 1) {
    var entry = entries[i] || {};
    rows += "<tr>" +
      '<td class="preview-row-num">' + (i + 1) + "</td>" +
      '<td title="' + escapeHtml(entry.name) + '">' + escapeHtml(entry.name) + "</td>" +
      "<td>" + (entry.ext ? '<span class="preview-ext">' + escapeHtml(entry.ext) + "</span>" : "") + "</td>" +
      "<td>" + escapeHtml(entry.size_human || "") + "</td>" +
    "</tr>";
  }
  var banner = "Archive inspected remotely: only the ZIP central directory was read, " +
    "never the archive contents.";
  if (total > entries.length) {
    banner += " Showing the first " + entries.length + " of " + total + " entries.";
  }
  return '<div class="preview-banner">' + escapeHtml(banner) + "</div>" +
    '<div class="preview-table-wrap"><table class="preview-table">' +
      "<thead><tr><th class=\"preview-row-num\">#</th><th>File path</th>" +
      "<th>Extension</th><th>Uncompressed size</th></tr></thead><tbody>" +
      (rows || '<tr><td class="preview-empty" colspan="4">The archive is empty</td></tr>') +
    "</tbody></table></div>";
}

/** JSON / text: escaped monospace viewer with the truncation notice. */
function renderPreviewCode(payload) {
  var content = String(payload.content === null || payload.content === undefined
    ? ""
    : payload.content);
  var banner = "";
  if (payload.truncated) {
    banner = '<div class="preview-banner">Preview limited to the first 256 KB of the file.</div>';
  }
  return banner + '<pre class="preview-code">' + escapeHtml(content) + "</pre>";
}

function renderPreviewImage(payload, entry) {
  var url = safeUrl(payload.image_url);
  if (!url) {
    return renderPreviewUnsupported(
      { reason: "The image URL could not be validated." },
      entry
    );
  }
  return '<div class="preview-image-wrap"><img class="preview-image" src="' +
    escapeHtml(url) + '" alt="' + escapeHtml(entry && entry.key ? entry.key : "Image preview") +
    '" loading="lazy"></div>' +
    '<p class="preview-status">Full-resolution image served directly by the repository.</p>';
}

function renderPreviewUnsupported(payload, entry) {
  var file = entry || {};
  var bits = [];
  if (file.key) {
    bits.push(file.key);
  }
  if (file.size_human) {
    bits.push(file.size_human);
  }
  var reason = String(payload && payload.reason ? payload.reason :
    "This format cannot be previewed in the browser.");
  return '<div class="preview-unsupported">' +
    "<p>" + escapeHtml(reason) + "</p>" +
    (bits.length
      ? '<p class="preview-size">' + escapeHtml(bits.join(" \u00b7 ")) + "</p>"
      : "") +
    (safeUrl(file.url)
      ? '<a class="preview-download" href="' + escapeHtml(safeUrl(file.url)) +
        '" target="_blank" rel="noopener noreferrer">Download file</a>'
      : "") +
  "</div>";
}

/** GeoJSON: map container, status line and a formatted-JSON offline fallback. */
function renderPreviewGeojsonShell(payload) {
  return '<div class="preview-map" id="preview-map"></div>' +
    '<p class="preview-status" id="preview-map-status">Loading the interactive map\u2026</p>' +
    '<pre class="preview-code" id="preview-geojson-fallback" hidden>' +
      escapeHtml(prettyJson(payload.geojson)) +
    "</pre>";
}

function loadLeaflet() {
  if (window.L) {
    return Promise.resolve(window.L);
  }
  if (leafletPromise) {
    return leafletPromise;
  }
  leafletPromise = new Promise(function (resolve, reject) {
    if (!document.querySelector("link[data-leaflet-css]")) {
      var css = document.createElement("link");
      css.rel = "stylesheet";
      css.href = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
      css.setAttribute("data-leaflet-css", "true");
      document.head.appendChild(css);
    }
    var script = document.createElement("script");
    script.src = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
    script.async = true;
    script.setAttribute("data-leaflet-js", "true");
    script.onload = function () {
      if (window.L) {
        resolve(window.L);
      } else {
        reject(new Error("Leaflet did not initialise"));
      }
    };
    script.onerror = function () {
      reject(new Error("Leaflet failed to load"));
    };
    document.head.appendChild(script);
  });
  leafletPromise.catch(function () {
    leafletPromise = null; /* allow a retry on the next GeoJSON preview */
  });
  return leafletPromise;
}

function mountPreviewMap(geojson) {
  var mapEl = $("preview-map");
  var statusEl = $("preview-map-status");
  var fallbackEl = $("preview-geojson-fallback");
  if (!mapEl) {
    return;
  }
  loadLeaflet().then(function (L) {
    if (!document.body.contains(mapEl)) {
      return; /* another file was selected in the meantime */
    }
    var map = L.map(mapEl, { scrollWheelZoom: true });
    previewState.map = map;
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors"
    }).addTo(map);
    var layer = L.geoJSON(geojson, { onEachFeature: bindPreviewFeaturePopup });
    layer.addTo(map);
    var bounds = layer.getBounds();
    if (bounds && bounds.isValid()) {
      map.fitBounds(bounds, { padding: [24, 24], maxZoom: 12 });
    } else {
      map.setView([46.52, 6.57], 3);
    }
    window.setTimeout(function () {
      map.invalidateSize();
    }, 120);
    if (statusEl) {
      statusEl.hidden = true;
    }
  }).catch(function () {
    if (mapEl) {
      mapEl.hidden = true;
    }
    if (fallbackEl) {
      fallbackEl.hidden = false;
    }
    if (statusEl) {
      statusEl.hidden = false;
      statusEl.classList.add("is-warn");
      statusEl.textContent =
        "The map library could not be loaded (offline?). Showing the formatted GeoJSON instead.";
    }
  });
}

function bindPreviewFeaturePopup(feature, layer) {
  var html = renderPreviewPopup(feature && feature.properties);
  if (html) {
    layer.bindPopup(html);
  }
}

function renderPreviewPopup(properties) {
  if (!properties || typeof properties !== "object") {
    return '<div class="preview-popup"><em>No properties</em></div>';
  }
  var keys = Object.keys(properties);
  if (!keys.length) {
    return '<div class="preview-popup"><em>No properties</em></div>';
  }
  var rows = "";
  var shown = Math.min(keys.length, 12);
  for (var i = 0; i < shown; i += 1) {
    var value = properties[keys[i]];
    if (value !== null && typeof value === "object") {
      value = prettyJson(value);
    }
    rows += "<dt>" + escapeHtml(keys[i]) + "</dt><dd>" +
      escapeHtml(value === null || value === undefined ? "" : value) + "</dd>";
  }
  if (keys.length > shown) {
    rows += "<dt>\u2026</dt><dd>" + (keys.length - shown) + " more field(s)</dd>";
  }
  return '<div class="preview-popup"><dl>' + rows + "</dl></div>";
}

function destroyPreviewMap() {
  if (previewState.map) {
    try {
      previewState.map.remove();
    } catch (error) {
      /* the container is already gone */
    }
    previewState.map = null;
  }
}

/* ------------------------------------------------------------------ startup */
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}

