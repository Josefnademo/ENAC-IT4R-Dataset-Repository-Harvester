/* ==========================================================================
   In-browser data preview ("surcouche").

   One record, one active file at a time. This module owns the overlay state,
   the on-demand file inventory, the renderers (table, chart, ZIP listing with
   clickable members, Markdown, PDF, GeoJSON map) and the in-session payload
   cache. It is loaded before app.js, so it only calls the global helpers that
   file defines (escapeHtml, asArray, safeUrl, state) at runtime, never at parse
   time. No build step, no framework, no external chart or Markdown library.
   ========================================================================== */
"use strict";

/* Payloads are cached per URL + inner path for the whole browser session, so
   closing the modal, switching files or leaving a ZIP member is instantaneous. */
var MAX_PREVIEW_CACHE = 40;
var PREVIEW_CHART_MAX_POINTS = 100;
/* Core formats used only until /api/data delivers the backend registry. */
var FALLBACK_PREVIEW_EXTENSIONS = [
  ".csv", ".tsv", ".json", ".geojson", ".txt", ".md", ".rst", ".yml", ".yaml",
  ".toml", ".py", ".sh", ".js", ".ts", ".pdf", ".zip",
  ".png", ".jpg", ".jpeg", ".webp", ".svg"
];
/* A PDF extracted from a ZIP is shown through a Blob URL that must be revoked. */
var previewObjectUrl = null;

var previewState = {
  item: null,
  entries: [],
  index: 0,
  view: "",
  payload: null,
  entry: null,
  chartX: null,
  chartY: null,
  token: 0,
  map: null
};
var previewEls = {};
var previewCache = new Map();
var leafletPromise = null;

/* --------------------------------------------------------------- bootstrap */
function initPreview() {
  previewEls = {
    modal: document.getElementById("preview-modal"),
    title: document.getElementById("preview-title"),
    fileField: document.getElementById("preview-file-field"),
    file: document.getElementById("preview-file"),
    size: document.getElementById("preview-size"),
    download: document.getElementById("preview-download"),
    body: document.getElementById("preview-body"),
    close: document.getElementById("preview-close")
  };
  if (!previewEls.modal || !previewEls.body) {
    return;
  }

  previewEls.modal.addEventListener("click", function (event) {
    if (event.target.closest("[data-preview-close]")) {
      closePreview();
    }
  });
  previewEls.file.addEventListener("change", function () {
    loadPreviewFile(Number(previewEls.file.value) || 0, "");
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !previewEls.modal.hidden) {
      closePreview();
    }
  });

  /* ZIP members, breadcrumb, Markdown toggle and table/chart toggle all
     re-render from the already fetched payload: no extra network call. */
  previewEls.body.addEventListener("click", function (event) {
    if (event.target.closest("[data-preview-back]")) {
      loadPreviewFile(previewState.index, "");
      return;
    }
    var inner = event.target.closest("[data-zip-inner]");
    if (inner) {
      loadPreviewFile(previewState.index, inner.getAttribute("data-zip-inner"));
      return;
    }
    var markdownView = event.target.closest("[data-md-view]");
    if (markdownView) {
      previewState.view = markdownView.getAttribute("data-md-view") === "raw" ? "md-raw" : "";
      renderPreviewPayload(previewState.payload, previewState.entry);
      return;
    }
    var tableView = event.target.closest("[data-table-view]");
    if (tableView) {
      previewState.view = tableView.getAttribute("data-table-view") === "chart" ? "chart" : "";
      renderPreviewPayload(previewState.payload, previewState.entry);
    }
  });
  previewEls.body.addEventListener("change", function (event) {
    var axis = event.target.closest("[data-chart-axis]");
    if (!axis) {
      return;
    }
    if (axis.getAttribute("data-chart-axis") === "x") {
      previewState.chartX = Number(axis.value);
    } else {
      previewState.chartY = Number(axis.value);
    }
    renderPreviewPayload(previewState.payload, previewState.entry);
  });
}

/* ------------------------------------------------- record and file inventory */
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

/** Card click delegate: resolve the record by its stable key, then open it. */
function openPreviewByKey(key) {
  for (var i = 0; i < state.items.length; i += 1) {
    if (previewKey(state.items[i]) === key) {
      openPreview(state.items[i]);
      return;
    }
  }
}

/** Shared by the cached and the on-demand flow: fill the file ``<select>``. */
function populatePreviewSelect(entries) {
  previewEls.file.innerHTML = "";
  for (var i = 0; i < entries.length; i += 1) {
    var option = document.createElement("option");
    option.value = String(i);
    option.textContent = entries[i].key +
      (entries[i].size_human ? " \u00b7 " + entries[i].size_human : "");
    previewEls.file.appendChild(option);
  }
  previewEls.file.disabled = entries.length < 2;
  previewEls.fileField.hidden = entries.length === 0;
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
  if (!item || !previewEls.modal) {
    return;
  }
  /* Never inherit the view toggle or payload of a previously opened record. */
  previewState.view = "";
  previewState.payload = null;
  previewState.entry = null;
  var entries = previewEntries(item);
  previewState.item = item;
  previewState.entries = entries;
  previewState.index = 0;
  var token = (previewState.token += 1);
  destroyPreviewMap();

  previewEls.title.textContent = item.title || "Untitled record";
  previewEls.modal.hidden = false;
  document.body.classList.add("preview-open");
  previewEls.size.textContent = "";
  previewEls.download.hidden = true;
  populatePreviewSelect(entries);
  previewEls.close.focus();

  if (entries.length) {
    loadPreviewFile(0, "");
    return;
  }

  /* Rows harvested before the preview feature only know the file count: ask
     the backend to resolve the inventory and cache it in SQLite. */
  previewEls.body.innerHTML =
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
      previewEls.body.innerHTML =
        '<div class="preview-notice">No downloadable files are attached to this record.</div>';
      return;
    }
    loadPreviewFile(0, "");
  } catch (error) {
    if (token !== previewState.token) {
      return;
    }
    previewEls.body.innerHTML =
      '<div class="preview-notice is-error">Cannot load the file list: ' +
      escapeHtml(error.message) + "</div>";
  }
}

function closePreview() {
  if (!previewEls.modal || previewEls.modal.hidden) {
    return;
  }
  previewState.token += 1;
  previewState.item = null;
  previewState.entries = [];
  destroyPreviewMap();
  releasePreviewPdf();
  previewEls.modal.hidden = true;
  previewEls.body.innerHTML = "";
  document.body.classList.remove("preview-open");
}

/* --------------------------------------------------------------- file cache */
function previewCacheKey(url, inner) {
  return String(url || "") + "::" + String(inner || "");
}

function previewCacheGet(url, inner) {
  return previewCache.get(previewCacheKey(url, inner));
}

function previewCacheSet(url, inner, payload) {
  var key = previewCacheKey(url, inner);
  if (previewCache.has(key)) {
    previewCache.delete(key); /* refresh recency */
  }
  previewCache.set(key, payload);
  while (previewCache.size > MAX_PREVIEW_CACHE) {
    previewCache.delete(previewCache.keys().next().value);
  }
}

/* -------------------------------------------------------------- file loader */
/** Load one file of the current record; ``innerPath`` names a ZIP member. */
async function loadPreviewFile(index, innerPath) {
  var entries = previewState.entries;
  if (!entries.length) {
    return;
  }
  if (index < 0 || index >= entries.length) {
    index = 0;
  }
  var entry = entries[index];
  var url = safeUrl(entry.url);
  var inner = innerPath ? String(innerPath) : "";
  var token = (previewState.token += 1);
  previewState.index = index;
  previewState.view = "";
  previewState.payload = null;
  previewState.entry = entry;
  previewState.chartX = null;
  previewState.chartY = null;
  destroyPreviewMap();
  releasePreviewPdf();

  previewEls.file.value = String(index);
  previewEls.size.textContent = inner ? "" : (entry.size_human || "");
  previewEls.download.href = url || "#";
  previewEls.download.hidden = !url;

  var cached = previewCacheGet(url, inner);
  if (cached) {
    renderPreviewPayload(cached, entry);
    return;
  }

  previewEls.body.innerHTML = '<div class="preview-loading">Fetching ' +
    escapeHtml(inner || entry.key) + "\u2026</div>";

  var params = new URLSearchParams();
  params.set("url", entry.url);
  params.set("name", entry.key || "");
  if (inner) {
    params.set("inner", inner);
  }
  try {
    var response = await fetch("/api/preview?" + params.toString(), { cache: "no-store" });
    var payload = await response.json();
    if (token !== previewState.token) {
      return;
    }
    if (!response.ok) {
      throw new Error(payload.detail || payload.error || "HTTP " + response.status);
    }
    previewCacheSet(url, inner, payload);
    renderPreviewPayload(payload, entry);
  } catch (error) {
    if (token !== previewState.token) {
      return;
    }
    previewEls.body.innerHTML = '<div class="preview-notice is-error">Preview unavailable: ' +
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

/* --------------------------------------------------------------- renderers */
function renderPreviewPayload(payload, entry) {
  previewState.payload = payload;
  previewState.entry = entry;

  var kind = String((payload && payload.kind) || "unsupported");
  var html = "";
  if (kind === "table") {
    html = renderPreviewTable(payload, entry);
  } else if (kind === "geojson") {
    html = renderPreviewGeojsonShell(payload);
  } else if (kind === "zip") {
    html = renderPreviewZip(payload);
  } else if (kind === "json" || kind === "text") {
    html = renderPreviewCode(payload, entry);
  } else if (kind === "pdf") {
    html = renderPreviewPdf(payload, entry);
  } else if (kind === "image") {
    html = renderPreviewImage(payload, entry);
  } else {
    html = renderPreviewUnsupported(payload, entry);
  }

  var innerPath = String((payload && payload.inner_path) || "");
  previewEls.body.innerHTML =
    (innerPath ? renderPreviewInnerBar(entry, innerPath) : "") + html;

  if (kind === "table") {
    bindPreviewTableFilter();
  } else if (kind === "geojson") {
    mountPreviewMap(payload.geojson);
  }
}

/** Breadcrumb shown above a file extracted from a ZIP: one click back. */
function renderPreviewInnerBar(entry, innerPath) {
  var archive = String((entry && entry.key) || "archive");
  return '<div class="preview-inner-bar">' +
    '<button type="button" class="preview-back" data-preview-back>' +
      "\u2190 Back to " + escapeHtml(archive) + " contents</button>" +
    '<span class="preview-inner-path" title="' + escapeHtml(innerPath) + '">' +
      escapeHtml(innerPath) + "</span>" +
  "</div>";
}

/** CSV/TSV: sticky header, row numbers, row filter, 100-row cap, quick chart. */
function renderPreviewTable(payload, entry) {
  var columns = asArray(payload.columns);
  var rows = asArray(payload.rows);
  if (!columns.length) {
    return '<div class="preview-banner">The file is empty or has no readable header row.</div>' +
      renderPreviewDownloadHint(entry);
  }

  var numeric = previewNumericColumns(columns, rows);
  var hasChart = numeric.length > 0 && rows.length >= 2;
  var chart = hasChart && previewState.view === "chart";

  var banner = payload.truncated
    ? "Preview limited to the first 256 KB and at most 100 rows. Download the original file for the complete dataset."
    : "Showing all " + rows.length + " row(s).";

  var tools = '<div class="preview-tools">';
  if (hasChart) {
    tools += '<div class="preview-view-toggle" role="group" aria-label="Table or chart">' +
      '<button type="button" data-table-view="table" class="' +
        (chart ? "" : "is-active") + '">Table</button>' +
      '<button type="button" data-table-view="chart" class="' +
        (chart ? "is-active" : "") + '">Quick Chart</button>' +
    "</div>";
  }
  if (chart) {
    tools += renderPreviewChartAxes(columns, numeric);
  } else {
    tools += '<input type="search" id="preview-row-filter" class="preview-filter-input" ' +
      'placeholder="Filter rows\u2026" aria-label="Filter table rows">' +
      '<span class="preview-filter-count" id="preview-row-count"></span>';
  }
  tools += "</div>";

  return '<div class="preview-banner">' + escapeHtml(banner) + "</div>" + tools +
    (chart ? renderPreviewChart(columns, rows) : renderPreviewTableBody(columns, rows));
}

function renderPreviewTableBody(columns, rows) {
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

  return '<div class="preview-table-wrap"><table class="preview-table">' +
    "<thead><tr>" + head + "</tr></thead><tbody>" +
    (body || '<tr><td class="preview-empty" colspan="' + (columns.length + 1) +
      '">No data rows</td></tr>') +
    "</tbody></table></div>";
}

function bindPreviewTableFilter() {
  var input = document.getElementById("preview-row-filter");
  var count = document.getElementById("preview-row-count");
  var rows = previewEls.body.querySelectorAll("[data-preview-row]");
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


/* ------------------------------------------------------------- quick chart */
function previewNumber(value) {
  if (value === null || value === undefined) {
    return null;
  }
  var text = String(value).trim();
  if (!text) {
    return null;
  }
  var number = Number(text.replace(/,/g, ""));
  return isFinite(number) ? number : null;
}

/** Columns where at least 60% of the non-empty cells parse as finite numbers. */
function previewNumericColumns(columns, rows) {
  var numeric = [];
  for (var c = 0; c < columns.length; c += 1) {
    var seen = 0;
    var parsed = 0;
    for (var r = 0; r < rows.length; r += 1) {
      var value = rows[r][c];
      if (value === null || value === undefined || String(value).trim() === "") {
        continue;
      }
      seen += 1;
      if (previewNumber(value) !== null) {
        parsed += 1;
      }
    }
    if (seen && parsed / seen >= 0.6) {
      numeric.push(c);
    }
  }
  return numeric;
}

/** Resolve the X/Y selects. Y defaults to the last numeric column (usually the
    measurement) and X to the first other column, falling back to the row index. */
function previewChartAxes(columns, numeric) {
  var y = previewState.chartY;
  if (numeric.indexOf(y) === -1) {
    y = numeric[numeric.length - 1];
  }
  var x = previewState.chartX;
  if (x === null || x === undefined) {
    x = -1;
    for (var c = 0; c < columns.length; c += 1) {
      if (c !== y) {
        x = c;
        break;
      }
    }
  }
  if (x >= columns.length) {
    x = -1;
  }
  previewState.chartX = x;
  previewState.chartY = y;
  return { x: x, y: y };
}

/** Y-axis chooser (numeric columns only) and X-axis chooser (any column). */
function renderPreviewChartAxes(columns, numeric) {
  var axes = previewChartAxes(columns, numeric);
  var html = '<span class="preview-axis"><label for="preview-chart-x">X</label>' +
    '<select id="preview-chart-x" data-chart-axis="x">' +
      '<option value="-1"' + (axes.x < 0 ? " selected" : "") + ">Row #</option>";
  for (var c = 0; c < columns.length; c += 1) {
    html += '<option value="' + c + '"' + (c === axes.x ? " selected" : "") + ">" +
      escapeHtml(columns[c]) + "</option>";
  }
  html += '</select></span><span class="preview-axis">' +
    '<label for="preview-chart-y">Y</label>' +
    '<select id="preview-chart-y" data-chart-axis="y">';
  for (var n = 0; n < numeric.length; n += 1) {
    html += '<option value="' + numeric[n] + '"' +
      (numeric[n] === axes.y ? " selected" : "") + ">" +
      escapeHtml(columns[numeric[n]]) + "</option>";
  }
  return html + "</select></span>";
}

function previewChartNumber(value) {
  return String(Math.round(value * 100) / 100);
}

function previewChartLabel(text) {
  var value = String(text === null || text === undefined ? "" : text);
  return value.length > 18 ? value.slice(0, 17) + "\u2026" : value;
}

/** Zero-dependency inline SVG line chart over the first 100 previewed rows. */
function renderPreviewChart(columns, rows) {
  var numeric = previewNumericColumns(columns, rows);
  var axes = previewChartAxes(columns, numeric);
  var points = [];
  for (var r = 0; r < rows.length && points.length < PREVIEW_CHART_MAX_POINTS; r += 1) {
    var value = previewNumber(rows[r][axes.y]);
    if (value === null) {
      continue;
    }
    var raw = axes.x >= 0 ? rows[r][axes.x] : "Row " + (r + 1);
    points.push({
      label: String(raw === null || raw === undefined ? "" : raw),
      value: value
    });
  }
  if (points.length < 2) {
    return '<div class="preview-notice">Not enough numeric values to draw a chart.</div>';
  }

  var min = points[0].value;
  var max = points[0].value;
  for (var i = 1; i < points.length; i += 1) {
    min = Math.min(min, points[i].value);
    max = Math.max(max, points[i].value);
  }
  var width = 720;
  var height = 300;
  var left = 64;
  var right = 18;
  var top = 18;
  var bottom = 34;
  var innerWidth = width - left - right;
  var innerHeight = height - top - bottom;
  var span = max - min || 1;
  var step = innerWidth / (points.length - 1);

  var polyline = "";
  var dots = "";
  for (var p = 0; p < points.length; p += 1) {
    var px = left + step * p;
    var py = top + innerHeight - ((points[p].value - min) / span) * innerHeight;
    polyline += (p ? " " : "") + px.toFixed(1) + "," + py.toFixed(1);
    dots += '<circle class="preview-chart-dot" cx="' + px.toFixed(1) +
      '" cy="' + py.toFixed(1) + '" r="3.4"><title>' +
      escapeHtml(points[p].label) + ": " +
      escapeHtml(previewChartNumber(points[p].value)) + "</title></circle>";
  }

  var grid = "";
  for (var g = 0; g <= 2; g += 1) {
    var gy = top + (innerHeight / 2) * g;
    grid += '<line class="preview-chart-grid" x1="' + left + '" y1="' + gy.toFixed(1) +
      '" x2="' + (width - right) + '" y2="' + gy.toFixed(1) + '"></line>' +
      '<text class="preview-chart-label" x="' + (left - 8) + '" y="' +
      (gy + 4).toFixed(1) + '" text-anchor="end">' +
      escapeHtml(previewChartNumber(max - (span / 2) * g)) + "</text>";
  }

  var xLabels = '<text class="preview-chart-label" x="' + left + '" y="' +
      (height - 12) + '">' + escapeHtml(previewChartLabel(points[0].label)) + "</text>" +
    '<text class="preview-chart-label" x="' + (width - right) + '" y="' + (height - 12) +
      '" text-anchor="end">' +
      escapeHtml(previewChartLabel(points[points.length - 1].label)) + "</text>";

  var yName = String(columns[axes.y] || "");
  var xName = axes.x >= 0 ? String(columns[axes.x] || "") : "row index";
  return '<div class="preview-chart">' +
    '<svg class="preview-chart-svg" viewBox="0 0 ' + width + " " + height +
      '" role="img" aria-label="' + escapeHtml(yName + " against " + xName) + '">' +
      grid +
      '<polyline class="preview-chart-line" points="' + polyline + '"></polyline>' +
      dots + xLabels +
    "</svg>" +
    '<p class="preview-status">' + points.length + " point(s) \u00b7 Y: " +
      escapeHtml(yName) + " \u00b7 X: " + escapeHtml(xName) + "</p>" +
  "</div>";
}


/* --------------------------------------------------------------------- ZIP */
/** Lower-case extension of a path, ignoring dots inside directory names. */
function previewExtension(name) {
  var text = String(name || "");
  var slash = text.lastIndexOf("/");
  var dot = text.lastIndexOf(".");
  if (dot <= slash || dot === text.length - 1) {
    return "";
  }
  var ext = text.slice(dot).toLowerCase();
  return ext.length <= 16 ? ext : "";
}

/** Previewable tokens come from /api/data (`preview_extensions`), so the backend
    registry stays the single source of truth; the small fallback list only
    covers the first paint, before that payload has arrived. */
function isPreviewableInner(name) {
  var text = String(name || "");
  if (!text || text.slice(-1) === "/") {
    return false;
  }
  var tokens = asArray(state.previewExtensions);
  if (!tokens.length) {
    tokens = FALLBACK_PREVIEW_EXTENSIONS;
  }
  var basename = text.split("/").pop().toLowerCase();
  if (tokens.indexOf(basename) !== -1) {
    return true; /* Makefile, LICENSE, Dockerfile, .gitignore, ... */
  }
  var ext = previewExtension(text);
  return !!ext && tokens.indexOf(ext) !== -1;
}

/** Remote ZIP: central-directory listing whose members can be opened in place. */
function renderPreviewZip(payload) {
  var entries = asArray(payload.entries);
  var total = Number(payload.entry_count || entries.length);
  var rows = "";
  for (var i = 0; i < entries.length; i += 1) {
    var entry = entries[i] || {};
    var name = String(entry.name || "");
    var clickable = isPreviewableInner(name);
    rows += "<tr" + (clickable
      ? ' class="preview-zip-row" data-zip-inner="' + escapeHtml(name) + '"' : "") + ">" +
      '<td class="preview-row-num">' + (i + 1) + "</td>" +
      '<td title="' + escapeHtml(name) + '">' + escapeHtml(name) + "</td>" +
      "<td>" + (entry.ext
        ? '<span class="preview-ext">' + escapeHtml(entry.ext) + "</span>"
        : "") + "</td>" +
      "<td>" + escapeHtml(entry.size_human || "") + "</td>" +
      '<td class="preview-zip-action">' + (clickable
        ? '<button type="button" class="preview-pill" data-zip-inner="' +
          escapeHtml(name) + '" title="Preview ' + escapeHtml(name) + '">Preview</button>'
        : "") + "</td>" +
      "</tr>";
  }
  var banner = "Archive inspected remotely: only the ZIP central directory was read, " +
    "never the whole archive. Click Preview on a CSV, JSON, text, code, image or PDF " +
    "entry to read just that file.";
  if (total > entries.length) {
    banner += " Showing the first " + entries.length + " of " + total + " entries.";
  }
  return '<div class="preview-banner">' + escapeHtml(banner) + "</div>" +
    '<div class="preview-table-wrap"><table class="preview-table preview-zip-table">' +
      '<thead><tr><th class="preview-row-num">#</th><th>File path</th>' +
      "<th>Extension</th><th>Uncompressed size</th><th>Preview</th></tr></thead><tbody>" +
      (rows || '<tr><td class="preview-empty" colspan="5">The archive is empty</td></tr>') +
    "</tbody></table></div>";
}


/* ------------------------------------------------------ JSON / text / MD */
/** JSON / text: escaped monospace viewer; `.md` renders formatted by default. */
function renderPreviewCode(payload, entry) {
  var content = String(payload.content === null || payload.content === undefined
    ? ""
    : payload.content);
  var name = String(payload.inner_path || (entry && entry.key) || "");
  var banner = payload.truncated
    ? '<div class="preview-banner">Preview limited to the first 256 KB of the file.</div>'
    : "";

  if (!/\.md$/i.test(name)) {
    return banner + '<pre class="preview-code">' + escapeHtml(content) + "</pre>";
  }

  var raw = previewState.view === "md-raw";
  var toggle = '<div class="preview-tools">' +
    '<div class="preview-view-toggle" role="group" aria-label="Rendered or raw Markdown">' +
      '<button type="button" data-md-view="rendered" class="' +
        (raw ? "" : "is-active") + '">Rendered</button>' +
      '<button type="button" data-md-view="raw" class="' +
        (raw ? "is-active" : "") + '">Raw</button>' +
    "</div></div>";
  return banner + toggle + (raw
    ? '<pre class="preview-code">' + escapeHtml(content) + "</pre>"
    : '<div class="preview-markdown">' + renderMarkdown(content) + "</div>");
}

/** Inline Markdown on already escaped text: code, bold and http(s) links. */
function renderMarkdownInline(text) {
  return String(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(https?:\/\/[^\s<]+)/g, function (url) {
      var trailing = "";
      while (url.length && ".,;:!?)]".indexOf(url.charAt(url.length - 1)) !== -1) {
        trailing = url.charAt(url.length - 1) + trailing; /* punctuation stays outside */
        url = url.slice(0, -1);
      }
      return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' +
        url + "</a>" + trailing;
    });
}

/** Minimal Markdown (headings, bold, code, fences, lists, links), XSS-safe:
    the whole document is escaped before any markup is added. */
function renderMarkdown(text) {
  var source = escapeHtml(String(text === null || text === undefined ? "" : text))
    .replace(/\r\n?/g, "\n");

  var fences = [];
  source = source.replace(/```([\s\S]*?)```/g, function (match, code) {
    fences.push('<pre class="preview-md-code">' +
      code.replace(/^\n+|\n+$/g, "") + "</pre>");
    return "\u0000" + (fences.length - 1) + "\u0000";
  });

  var html = [];
  var paragraph = [];
  var list = [];

  function flushParagraph() {
    if (paragraph.length) {
      html.push("<p>" + renderMarkdownInline(paragraph.join(" ")) + "</p>");
      paragraph = [];
    }
  }
  function flushList() {
    if (list.length) {
      html.push("<ul>" + list.join("") + "</ul>");
      list = [];
    }
  }
  function flushAll() {
    flushParagraph();
    flushList();
  }

  var lines = source.split("\n");
  for (var i = 0; i < lines.length; i += 1) {
    var trimmed = lines[i].trim();
    var fence = /^\u0000(\d+)\u0000$/.exec(trimmed);
    if (fence) {
      flushAll();
      html.push(fences[Number(fence[1])]);
      continue;
    }
    if (!trimmed) {
      flushAll();
      continue;
    }
    var heading = /^(#{1,3})\s+(.*)$/.exec(trimmed);
    if (heading) {
      flushAll();
      var level = heading[1].length + 2; /* h3-h5: never shadow the page h1/h2 */
      html.push("<h" + level + ">" + renderMarkdownInline(heading[2]) +
        "</h" + level + ">");
      continue;
    }
    var bullet = /^[-*+]\s+(.*)$/.exec(trimmed);
    if (bullet) {
      flushParagraph();
      list.push("<li>" + renderMarkdownInline(bullet[1]) + "</li>");
      continue;
    }
    flushList();
    paragraph.push(trimmed);
  }
  flushAll();
  return html.join("");
}


/* -------------------------------------------------------------- PDF viewer */
/** Free the Blob URL of the ZIP-extracted PDF currently displayed. */
function releasePreviewPdf() {
  if (previewObjectUrl) {
    window.URL.revokeObjectURL(previewObjectUrl);
    previewObjectUrl = null;
  }
}

/** Decode the base64 payload of a ZIP member into raw bytes. */
function previewBase64Bytes(encoded) {
  var binary = window.atob(String(encoded));
  var bytes = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

/** PDF: a direct iframe for remote URLs, a Blob URL for ZIP members. */
function renderPreviewPdf(payload, entry) {
  var source = "";
  var note = "";
  if (payload && payload.pdf_base64) {
    try {
      releasePreviewPdf();
      var blob = new Blob(
        [previewBase64Bytes(payload.pdf_base64)],
        { type: "application/pdf" }
      );
      previewObjectUrl = window.URL.createObjectURL(blob);
      source = previewObjectUrl;
      note = "PDF decoded from the ZIP archive in memory; only that member was read.";
    } catch (error) {
      return renderPreviewUnsupported(
        { reason: "The PDF inside the archive could not be decoded." },
        entry
      );
    }
  } else {
    source = safeUrl(payload && payload.pdf_url);
    note = "PDF served directly by the repository.";
  }
  if (!source) {
    return renderPreviewUnsupported(
      { reason: "The PDF URL could not be validated." },
      entry
    );
  }
  var name = String(payload.inner_path || (entry && entry.key) || "PDF preview");
  return '<iframe class="preview-pdf" src="' + escapeHtml(source) +
    '" title="' + escapeHtml(name) + '"></iframe>' +
    '<p class="preview-status">' + escapeHtml(note) + "</p>";
}

/* --------------------------------------------------- image and unsupported */
/** Remote image URLs are hot-linked; ZIP-extracted ones are safe data URIs. */
function previewImageUrl(value) {
  var text = String(value === null || value === undefined ? "" : value).trim();
  if (/^data:image\/(?:png|jpeg|webp|svg\+xml);base64,[A-Za-z0-9+/=]+$/.test(text)) {
    return text;
  }
  return safeUrl(text);
}

function renderPreviewImage(payload, entry) {
  var url = previewImageUrl(payload.image_url);
  if (!url) {
    return renderPreviewUnsupported(
      { reason: "The image URL could not be validated." },
      entry
    );
  }
  var name = String(payload.inner_path || (entry && entry.key) || "Image preview");
  var note = payload.from_zip
    ? "Image decoded from the ZIP archive in memory; only that member was read."
    : "Full-resolution image served directly by the repository.";
  return '<div class="preview-image-wrap"><img class="preview-image" src="' +
    escapeHtml(url) + '" alt="' + escapeHtml(name) + '" loading="lazy"></div>' +
    '<p class="preview-status">' + escapeHtml(note) + "</p>";
}

function renderPreviewUnsupported(payload, entry) {
  var file = entry || {};
  var bits = [];
  if (payload && payload.inner_path) {
    bits.push(payload.inner_path);
  } else {
    if (file.key) {
      bits.push(file.key);
    }
    if (file.size_human) {
      bits.push(file.size_human);
    }
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


/* ------------------------------------------------------------ Leaflet map */
/** Leaflet is injected from unpkg only the first time a GeoJSON is opened. */
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
  var mapEl = document.getElementById("preview-map");
  var statusEl = document.getElementById("preview-map-status");
  var fallbackEl = document.getElementById("preview-geojson-fallback");
  if (!mapEl) {
    return;
  }
  loadLeaflet().then(function (L) {
    if (!previewEls.body.contains(mapEl)) {
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

