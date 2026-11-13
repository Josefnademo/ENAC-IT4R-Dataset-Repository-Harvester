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

/** Shared builder: every tag is a button carrying its raw text for the search. */
function tagButton(value, className) {
  var text = String(value === null || value === undefined ? "" : value);
  return '<button type="button" class="' + className + ' is-filterable" data-filter-tag="' +
    escapeHtml(text) + '" title="Click to filter by this tag">' + escapeHtml(text) + "</button>";
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
    statusText: $("status-text")
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
    var copyTarget = event.target.closest("[data-copy]");
    if (copyTarget) {
      copyIdentifier(copyTarget);
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

function metaRow(label, valueHtml, extraClass) {
  return '<div class="meta-row' + (extraClass ? " " + extraClass : "") + '">' +
    '<span class="meta-label">' + escapeHtml(label) + "</span>" +
    valueHtml +
    "</div>";
}

function mutedValue(text) {
  return '<span class="meta-value is-muted">' + escapeHtml(text) + "</span>";
}

function valueList(values, emptyText) {
  var list = asArray(values);
  if (!list.length) {
    return mutedValue(emptyText);
  }
  var text = list
    .map(function (value) {
      return escapeHtml(value);
    })
    .join(", ");
  return '<span class="meta-value">' + text + "</span>";
}

function renderBadges(values, className) {
  var list = asArray(values);
  if (!list.length) {
    return "";
  }
  var html = "";
  for (var i = 0; i < list.length; i += 1) {
    html += filterableBadge(list[i], className);
  }
  return html;
}

function renderFunding(item) {
  var list = asArray(item.funding);
  if (!list.length) {
    return mutedValue("No funding metadata");
  }
  var html = "";
  for (var i = 0; i < list.length; i += 1) {
    var label = String(list[i]);
    var isOrd = /open research data|ord\b|flagship|programme/i.test(label);
    html += filterableBadge(label, "badge-grant", isOrd ? "is-ord" : "");
  }
  return '<span class="badge-row">' + html + "</span>";
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


function renderAuthors(item) {
  var authors = asArray(item.authors);
  if (!authors.length) {
    return mutedValue("Not listed");
  }
  var names = authors
    .map(function (name) {
      return escapeHtml(name);
    })
    .join(", ");
  return '<span class="meta-value">' + names + "</span>";
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

  var keywords = asArray(item.keywords);
  var keywordHtml = "";
  for (var i = 0; i < keywords.length; i += 1) {
    keywordHtml += tagButton(keywords[i], "keyword-chip");
  }

  var abstract = item.abstract ? escapeHtml(item.abstract) : "";

  return '<article class="dataset-card' +
    (item.sync_status === "new" ? " is-new" : "") +
    (item.sync_status === "updated" ? " is-updated" : "") + '">' +
    '<div class="card-badges">' +
      '<span class="badge badge-type">' + escapeHtml(type) + "</span>" +
      '<span class="badge badge-source">' + escapeHtml(sourceLabel(item)) + "</span>" +
      renderBadges(item.affiliations, "badge-lab") +
      renderBadges(item.parent_orgs, "badge-org") +
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
      metaRow("PI / Lab head", valueList(item.unit_managers, "Not published")) +
      metaRow("EPFL labs", valueList(item.affiliations, "Not mapped to an EPFL unit")) +
      metaRow("ENAC units", valueList(item.parent_orgs, "Not reported")) +
      metaRow("Funding", renderFunding(item)) +
    "</div>" +
    (abstract ? '<p class="card-abstract">' + abstract + "</p>" : "") +
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

/* ------------------------------------------------------------------ startup */
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}

