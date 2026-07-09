/* Shared dashboard logic: data fetching, chart renderers, searchable data table,
 * and the chat widget. Every page loads this file and calls into `App`. */
const App = (() => {
  const CHART_COLORS = ["#2563eb", "#8b5cf6", "#06b6d4", "#16a34a", "#f59e0b", "#f97316", "#dc2626", "#94a3b8", "#0ea5e9", "#a855f7"];

  const PILL_COLORS = {
    // status-ish
    "completed": "#16a34a", "active": "#16a34a", "green": "#16a34a", "done": "#16a34a", "low": "#16a34a",
    "in progress": "#2563eb", "planning": "#8b5cf6", "not started": "#94a3b8", "backlog": "#94a3b8",
    "in review": "#0ea5e9",
    "at risk": "#f59e0b", "amber": "#f59e0b", "medium": "#f59e0b", "on leave": "#f59e0b", "on hold": "#f97316",
    "blocked": "#dc2626", "red": "#dc2626", "high": "#dc2626", "critical": "#dc2626", "contractor": "#8b5cf6",
  };

  function savedTheme() {
    try { return localStorage.getItem("dashboardTheme"); } catch (_) { return null; }
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  }

  function initialTheme() {
    try {
      const stored = savedTheme();
      if (stored) return stored;
      return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    } catch (_) {
      return "light";
    }
  }

  try {
    applyTheme(initialTheme());
  } catch (_) {
    document.documentElement.dataset.theme = "light";
  }

  function pillColor(value) {
    const key = String(value ?? "").trim().toLowerCase();
    if (PILL_COLORS[key]) return PILL_COLORS[key];
    // Stable fallback color for values we don't have an explicit mapping for.
    let hash = 0;
    for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
    return CHART_COLORS[hash % CHART_COLORS.length];
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function formatNumber(value) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value !== "number") return escapeHtml(value);
    return value.toLocaleString("en-US", { maximumFractionDigits: 1 });
  }

  function progressMarkup(value, max = 100, label = `${formatNumber(value)}%`) {
    const numeric = Number(value) || 0;
    const maximum = Number(max) > 0 ? Number(max) : 100;
    const percent = numeric / maximum * 100;
    const width = Math.min(100, Math.max(0, percent));
    return `<div class="progress-value${percent > 100 ? " over" : ""}" role="progressbar" aria-valuemin="0" aria-valuemax="${maximum}" aria-valuenow="${numeric}"><span>${label}</span><i><b style="width:${width.toFixed(1)}%"></b></i></div>`;
  }

  function debounce(fn, wait) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
  }

  function updateUrlState(values) {
    const url = new URL(location.href);
    Object.entries(values).forEach(([key, value]) => {
      if (value === null || value === undefined || value === "") url.searchParams.delete(key);
      else url.searchParams.set(key, String(value));
    });
    history.replaceState(null, "", url.href);
  }

  async function copyViewLink(button) {
    try {
      await navigator.clipboard.writeText(location.href);
    } catch (_) {
      const input = document.createElement("textarea");
      input.value = location.href;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      document.execCommand("copy");
      input.remove();
    }
    const original = button.textContent;
    button.textContent = "Link copied";
    setTimeout(() => { button.textContent = original; }, 1600);
  }

  function resetDashboardState() {
    try { sessionStorage.removeItem("dashboardFilters"); } catch (_) {}
    try {
      localStorage.removeItem("dashboardTheme");
      Object.keys(localStorage).filter(key => key.startsWith("dashboardKpis:")).forEach(key => localStorage.removeItem(key));
    } catch (_) {}
    const url = new URL(location.href);
    url.search = "";
    history.replaceState(null, "", url.href);
    location.reload();
  }

  const apiMetaTag = document.querySelector('meta[name="dashboard-api"]');
  const configuredApi = apiMetaTag ? apiMetaTag.content.trim().replace(/\/$/, "") : "";
  const configuredApiBase = configuredApi.replace(/\/ask$/, "");
  const isLocalHost = ["127.0.0.1", "localhost"].includes(location.hostname);
  const isLiveServer = location.protocol === "file:" || (isLocalHost && location.port && location.port !== "8000");
  const apiBase = configuredApiBase || (isLiveServer ? "http://127.0.0.1:8000" : "");

  function apiUrl(path) {
    return `${apiBase}${path}`;
  }

  async function fetchJSON(url) {
    let response;
    try {
      response = await fetch(url, { credentials: "include" });
    } catch (_) {
      throw new Error("Could not connect to the dashboard server. Check that it is running, then retry.");
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    if (!Object.keys(data).length) throw new Error("The dashboard server returned an invalid response. Please retry.");
    return data;
  }

  const fetchTables = () => fetchJSON(apiUrl("/api/tables"));
  const fetchData = (tableName) => fetchJSON(apiUrl(`/api/data/${encodeURIComponent(tableName)}`));

  const ROLE_PAGES = {
    "Administrator": ["index.html", "departments.html", "employees.html", "projects.html", "tasks.html", "meetings.html", "weekly-updates.html", "activity-log.html", "lists.html", "admin.html"],
    "HR Manager": ["index.html", "departments.html", "employees.html", "projects.html", "tasks.html", "meetings.html", "weekly-updates.html"],
    "Department Director": ["index.html", "departments.html", "employees.html", "projects.html", "tasks.html", "meetings.html", "weekly-updates.html"],
    "Project Manager": ["index.html", "projects.html", "tasks.html", "meetings.html", "weekly-updates.html"],
    "Employee": ["index.html", "employees.html", "projects.html", "tasks.html", "meetings.html", "weekly-updates.html"],
  };

  function applyRoleNavigation(user) {
    const role = user?.role || "Employee";
    const pages = new Set(ROLE_PAGES[role] || ROLE_PAGES.Employee);
    document.querySelectorAll("nav.tabs a").forEach(link => {
      const page = (link.getAttribute("href") || "").split("?")[0].split("/").pop() || "index.html";
      link.hidden = !pages.has(page);
    });
  }

  function looksLikeDate(value) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2})?/.test(value);
  }

  function sparklineSeries(rows, dateKey, valueKey = null, limit = 10) {
    const buckets = {};
    rows.forEach(row => {
      const date = String(row[dateKey] || "").slice(0, 7);
      if (!/^\d{4}-\d{2}$/.test(date)) return;
      const value = valueKey ? Number(row[valueKey]) : 1;
      if (!Number.isFinite(value)) return;
      (buckets[date] ||= []).push(value);
    });
    return Object.keys(buckets).sort().slice(-limit).map(date => {
      const values = buckets[date];
      return valueKey ? values.reduce((sum, value) => sum + value, 0) / values.length : values.length;
    });
  }

  function kpiKey(label) {
    return String(label).trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  }

  function kpiStorageKey(container) {
    return `dashboardKpis:${location.pathname}:${container.id || "kpis"}`;
  }

  function readKpiConfig(container) {
    try { return JSON.parse(localStorage.getItem(kpiStorageKey(container)) || "{}") || {}; } catch (_) { return {}; }
  }

  function ensureKpiToolbar(container) {
    if (container.previousElementSibling?.classList.contains("kpi-toolbar")) return;
    const toolbar = document.createElement("div");
    toolbar.className = "kpi-toolbar";
    toolbar.innerHTML = `<span>Summary cards</span><button type="button">Customize cards</button>`;
    toolbar.querySelector("button").addEventListener("click", () => openKpiCustomizer(container));
    container.insertAdjacentElement("beforebegin", toolbar);
  }

  function openKpiCustomizer(container) {
    const items = container.kpiItems || [];
    const config = readKpiConfig(container);
    const order = config.order || [];
    const ordered = items.slice().sort((a, b) => {
      const aIndex = order.indexOf(kpiKey(a.label));
      const bIndex = order.indexOf(kpiKey(b.label));
      return (aIndex < 0 ? Number.MAX_SAFE_INTEGER : aIndex) - (bIndex < 0 ? Number.MAX_SAFE_INTEGER : bIndex);
    });
    let shell = document.getElementById("kpi-customizer-shell");
    if (!shell) {
      shell = document.createElement("div");
      shell.id = "kpi-customizer-shell";
      shell.className = "detail-shell";
      shell.innerHTML = `<button class="detail-backdrop" type="button" aria-label="Close card customizer"></button><section class="kpi-customizer" role="dialog" aria-modal="true" aria-labelledby="kpi-customizer-title"><header><div><span>Dashboard layout</span><h2 id="kpi-customizer-title">Customize summary cards</h2></div><button class="detail-close" type="button" aria-label="Close card customizer">×</button></header><p class="customizer-help">Choose which cards appear, change their size, and arrange their order.</p><div class="customizer-list"></div><footer><button type="button" class="customizer-reset">Restore defaults</button><div><button type="button" class="customizer-cancel">Cancel</button><button type="button" class="customizer-save">Save layout</button></div></footer></section>`;
      document.body.appendChild(shell);
      shell.closePanel = () => { shell.classList.remove("open"); document.body.classList.remove("drawer-open"); shell.trigger?.focus(); };
      shell.querySelector(".detail-backdrop").addEventListener("click", shell.closePanel);
      shell.querySelector(".detail-close").addEventListener("click", shell.closePanel);
      shell.querySelector(".customizer-cancel").addEventListener("click", shell.closePanel);
      document.addEventListener("keydown", event => { if (event.key === "Escape" && shell.classList.contains("open")) shell.closePanel(); });
    }
    shell.container = container;
    shell.trigger = container.previousElementSibling?.querySelector("button");
    const list = shell.querySelector(".customizer-list");
    list.innerHTML = ordered.map(item => {
      const key = kpiKey(item.label);
      const visible = !(config.hidden || []).includes(key);
      const size = config.sizes?.[key] === "wide" ? "wide" : "standard";
      return `<div class="customizer-row" data-kpi-key="${key}"><label><input type="checkbox"${visible ? " checked" : ""}><span>${escapeHtml(item.label)}</span></label><select aria-label="Size for ${escapeHtml(item.label)}"><option value="standard"${size === "standard" ? " selected" : ""}>Standard</option><option value="wide"${size === "wide" ? " selected" : ""}>Wide</option></select><div><button type="button" data-move="up" aria-label="Move ${escapeHtml(item.label)} up">↑</button><button type="button" data-move="down" aria-label="Move ${escapeHtml(item.label)} down">↓</button></div></div>`;
    }).join("");
    list.onclick = event => {
      const button = event.target.closest("[data-move]");
      const row = button?.closest(".customizer-row");
      if (!row) return;
      if (button.dataset.move === "up" && row.previousElementSibling) list.insertBefore(row, row.previousElementSibling);
      if (button.dataset.move === "down" && row.nextElementSibling) list.insertBefore(row.nextElementSibling, row);
    };
    shell.querySelector(".customizer-save").onclick = () => {
      const rows = [...list.querySelectorAll(".customizer-row")];
      const next = {
        order: rows.map(row => row.dataset.kpiKey),
        hidden: rows.filter(row => !row.querySelector('input[type="checkbox"]').checked).map(row => row.dataset.kpiKey),
        sizes: Object.fromEntries(rows.map(row => [row.dataset.kpiKey, row.querySelector("select").value])),
      };
      try { localStorage.setItem(kpiStorageKey(container), JSON.stringify(next)); } catch (_) {}
      shell.closePanel();
      kpiGrid(container, container.kpiItems);
    };
    shell.querySelector(".customizer-reset").onclick = () => {
      try { localStorage.removeItem(kpiStorageKey(container)); } catch (_) {}
      shell.closePanel();
      kpiGrid(container, container.kpiItems);
    };
    shell.classList.add("open");
    document.body.classList.add("drawer-open");
    shell.querySelector(".detail-close").focus();
  }

  function kpiTone(item) {
    if (item.tone) return item.tone;
    const text = `${item.label} ${item.value} ${item.sub || ""}`.toLowerCase();
    if (/blocked|at risk|red health|overdue|critical/.test(text)) return "danger";
    if (/completed|green health|healthy|active employees/.test(text)) return "success";
    if (/planning|review|amber|on leave/.test(text)) return "warning";
    return "info";
  }

  function sparklineMarkup(values) {
    const points = (values || []).map(Number).filter(Number.isFinite);
    if (points.length < 2) return "";
    const width = 130, height = 34, pad = 3;
    const min = Math.min(...points), max = Math.max(...points), range = max - min || 1;
    const coordinates = points.map((value, index) => {
      const x = pad + index / (points.length - 1) * (width - pad * 2);
      const y = height - pad - (value - min) / range * (height - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<svg class="kpi-sparkline" viewBox="0 0 ${width} ${height}" aria-hidden="true" focusable="false"><polyline points="${coordinates}"></polyline></svg>`;
  }

  function animateKpiCounters(container) {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    container.querySelectorAll(".kpi strong[data-final]").forEach(node => {
      const finalValue = node.dataset.final;
      const match = finalValue.match(/^(.*?)(-?[\d,]+(?:\.\d+)?)([^\d]*)$/);
      if (!match) return;
      const target = Number(match[2].replaceAll(",", ""));
      if (!Number.isFinite(target)) return;
      const decimals = (match[2].split(".")[1] || "").length;
      const started = performance.now();
      node.setAttribute("aria-label", finalValue);
      function frame(now) {
        const progress = Math.min(1, (now - started) / 720);
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = target * eased;
        node.textContent = `${match[1]}${current.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}${match[3]}`;
        if (progress < 1 && node.isConnected) requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    });
  }

  // ---------------- KPI cards ----------------
  function kpiGrid(container, items) {
    const el = typeof container === "string" ? document.querySelector(container) : container;
    el.removeAttribute("aria-busy");
    el.kpiItems = items;
    ensureKpiToolbar(el);
    const config = readKpiConfig(el);
    const order = config.order || [];
    const visibleItems = items.filter(item => !(config.hidden || []).includes(kpiKey(item.label))).sort((a, b) => {
      const aIndex = order.indexOf(kpiKey(a.label));
      const bIndex = order.indexOf(kpiKey(b.label));
      return (aIndex < 0 ? Number.MAX_SAFE_INTEGER : aIndex) - (bIndex < 0 ? Number.MAX_SAFE_INTEGER : bIndex);
    });
    el.innerHTML = visibleItems.length ? visibleItems.map(item => {
      const displayValue = typeof item.value === "number" ? formatNumber(item.value) : escapeHtml(item.value);
      return `
      <article class="kpi tone-${kpiTone(item)}${config.sizes?.[kpiKey(item.label)] === "wide" ? " kpi-wide" : ""}" data-kpi-key="${kpiKey(item.label)}">
        <span>${escapeHtml(item.label)}</span>
        <strong data-final="${displayValue}">${displayValue}</strong>
        ${item.sub ? `<small>${escapeHtml(item.sub)}</small>` : ""}
        ${Number.isFinite(Number(item.progress)) ? progressMarkup(item.progress, 100, "") : ""}
        ${sparklineMarkup(item.sparkline)}
      </article>`;
    }).join("") : `<div class="kpi-empty"><strong>All summary cards are hidden.</strong><span>Use Customize cards to restore the cards you want to see.</span></div>`;
    animateKpiCounters(el);
  }

  // ---------------- Horizontal bar chart ----------------
  // items: [{label, value, color?}]
  function barChart(container, items, options = {}) {
    const el = typeof container === "string" ? document.querySelector(container) : container;
    el.removeAttribute("aria-busy");
    if (!items.length) { el.innerHTML = `<p class="loading-note">No data to chart.</p>`; return; }
    const max = Math.max(...items.map(i => i.value), 1);
    el.innerHTML = `<div class="bar-chart">${items.map((item, i) => `
      <div class="bar-row${options.filterKey ? " chart-selectable" : ""}" data-chart-index="${i}"${options.filterKey ? ` tabindex="0" role="button" aria-label="Filter by ${escapeHtml(options.filterKey.replaceAll("_", " "))}: ${escapeHtml(item.label)}, ${formatNumber(item.value)} records"` : ""}>
        <div class="bar-label"><span>${escapeHtml(item.label)}</span><b>${formatNumber(item.value)}</b></div>
        <div class="bar-track"><div class="bar-fill" style="width:${(item.value / max * 100).toFixed(2)}%;background:${item.color || CHART_COLORS[i % CHART_COLORS.length]}"></div></div>
      </div>`).join("")}</div>`;
    el.setAttribute("role", options.filterKey ? "group" : "img");
    el.setAttribute("aria-label", `Bar chart. ${items.map(item => `${item.label}: ${item.value}`).join("; ")}`);
    bindChartDrilldown(el, ".bar-row", items, options.filterKey);
  }

  // ---------------- Donut chart ----------------
  // items: [{label, value, color?}]
  function donutChart(container, items, centerLabel, options = {}) {
    const el = typeof container === "string" ? document.querySelector(container) : container;
    el.removeAttribute("aria-busy");
    const total = items.reduce((sum, i) => sum + i.value, 0);
    if (!total) { el.innerHTML = `<p class="loading-note">No data to chart.</p>`; return; }
    let cursor = 0;
    const stops = items.map((item, i) => {
      const color = item.color || CHART_COLORS[i % CHART_COLORS.length];
      const start = (cursor / total) * 100;
      cursor += item.value;
      const end = (cursor / total) * 100;
      return `${color} ${start.toFixed(2)}% ${end.toFixed(2)}%`;
    }).join(", ");
    el.innerHTML = `
      <div class="donut-layout">
        <div class="donut" style="background:conic-gradient(${stops})">
          <div class="donut-center"><strong>${formatNumber(total)}</strong><span>${escapeHtml(centerLabel || "total")}</span></div>
        </div>
        <div class="legend">${items.map((item, i) => `
          <div class="legend-row${options.filterKey ? " chart-selectable" : ""}" data-chart-index="${i}"${options.filterKey ? ` tabindex="0" role="button" aria-label="Filter by ${escapeHtml(options.filterKey.replaceAll("_", " "))}: ${escapeHtml(item.label)}, ${formatNumber(item.value)} records"` : ""}>
            <i style="background:${item.color || CHART_COLORS[i % CHART_COLORS.length]}"></i>
            <span>${escapeHtml(item.label)}</span>
            <b>${formatNumber(item.value)} · ${(item.value / total * 100).toFixed(0)}%</b>
          </div>`).join("")}</div>
      </div>`;
    el.setAttribute("role", options.filterKey ? "group" : "img");
    el.setAttribute("aria-label", `Donut chart. ${items.map(item => `${item.label}: ${item.value}`).join("; ")}`);
    bindChartDrilldown(el, ".legend-row", items, options.filterKey);
  }

  function bindChartDrilldown(container, selector, items, filterKey) {
    if (!filterKey) return;
    const activate = (target) => {
      const item = items[Number(target.dataset.chartIndex)];
      if (!item) return;
      document.dispatchEvent(new CustomEvent("dashboard:drilldown", {
        detail: { key: filterKey, value: item.filterValue ?? item.label },
      }));
    };
    container.querySelectorAll(selector).forEach(target => {
      target.addEventListener("click", () => activate(target));
      target.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        activate(target);
      });
    });
  }

  // ---------------- Trend / line chart ----------------
  // points: [{label, value}] assumed already in chronological order
  function trendChart(container, points) {
    const el = typeof container === "string" ? document.querySelector(container) : container;
    el.removeAttribute("aria-busy");
    if (!points.length) { el.innerHTML = `<p class="loading-note">No data to chart.</p>`; return; }
    const width = 680, height = 230, padLeft = 40, padBottom = 26, padTop = 14, padRight = 14;
    const plotW = width - padLeft - padRight, plotH = height - padTop - padBottom;
    const values = points.map(p => p.value);
    const maxV = Math.max(...values, 1), minV = Math.min(...values, 0);
    const range = maxV - minV || 1;
    const x = (i) => padLeft + (points.length === 1 ? plotW / 2 : (i / (points.length - 1)) * plotW);
    const y = (v) => padTop + plotH - ((v - minV) / range) * plotH;
    const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(" ");
    const areaPath = `${linePath} L${x(points.length - 1).toFixed(1)},${(padTop + plotH).toFixed(1)} L${x(0).toFixed(1)},${(padTop + plotH).toFixed(1)} Z`;
    const gridLines = [0, 0.5, 1].map(t => {
      const gy = padTop + plotH * t;
      const val = maxV - range * t;
      return `<line class="trend-grid" x1="${padLeft}" x2="${width - padRight}" y1="${gy.toFixed(1)}" y2="${gy.toFixed(1)}"/><text class="trend-yaxis" x="${padLeft - 8}" y="${(gy + 3).toFixed(1)}">${formatNumber(Math.round(val))}</text>`;
    }).join("");
    const labelStep = Math.max(1, Math.ceil(points.length / 8));
    const xLabels = points.map((p, i) => (i % labelStep === 0 || i === points.length - 1)
      ? `<text class="trend-label" x="${x(i).toFixed(1)}" y="${height - 6}">${escapeHtml(p.label)}</text>` : "").join("");
    const dots = points.map((p, i) => `<circle class="trend-point" cx="${x(i).toFixed(1)}" cy="${y(p.value).toFixed(1)}" r="3.5"><title>${escapeHtml(p.label)}: ${formatNumber(p.value)}</title></circle>`).join("");
    el.innerHTML = `<div class="trend-chart"><svg viewBox="0 0 ${width} ${height}">${gridLines}<path class="trend-area" d="${areaPath}"/><path class="trend-line" d="${linePath}"/>${dots}${xLabels}</svg></div>`;
    el.setAttribute("role", "img");
    el.setAttribute("aria-label", `Trend chart with ${points.length} points, from ${points[0].label} to ${points.at(-1).label}.`);
  }

  // ---------------- Persistent page filters ----------------
  function filterBar({ columns, rows, onChange }) {
    const columnKeys = new Set(columns.map(column => column.key));
    const definitions = [
      { key: "department", fallback: "department_name", label: "Department" },
      { key: "project", fallback: "project_name", label: "Project" },
      { key: "status", label: "Status" },
      { key: "priority", label: "Priority" },
      { key: "risk_level", label: "Risk" },
      { key: "health", label: "Health" },
      { key: "division", label: "Division" },
      { key: "employment_status", label: "Employment status" },
      { key: "meeting_type", label: "Meeting type" },
      { key: "impact", label: "Impact" },
      { key: "outcome", label: "Outcome" },
      { key: "location", label: "Location" },
    ].map(definition => ({
      ...definition,
      sourceKey: columnKeys.has(definition.key) ? definition.key : definition.fallback,
    })).filter(definition => definition.sourceKey && columnKeys.has(definition.sourceKey));
    const dateColumn = columns.find(column => /(^|_)(date|timestamp|week_starting|date_time)($|_)/.test(column.key));

    if (!definitions.length && !dateColumn) {
      return { set: () => {}, clear: () => {}, apply: () => onChange(rows, { activeCount: 0 }) };
    }

    const host = document.createElement("section");
    host.className = "filter-panel";
    host.setAttribute("aria-label", "Dashboard filters");
    const controls = definitions.map(definition => {
      const values = [...new Set(rows.map(row => row[definition.sourceKey]).filter(value => value !== null && value !== undefined && value !== ""))]
        .sort((a, b) => String(a).localeCompare(String(b)));
      return `<label><span>${escapeHtml(definition.label)}</span><select data-filter-key="${escapeHtml(definition.key)}"><option value="">All</option>${values.map(value => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("")}</select></label>`;
    }).join("");
    const dateControls = dateColumn ? `
      <label><span>From</span><input type="date" data-date-filter="from"></label>
      <label><span>To</span><input type="date" data-date-filter="to"></label>` : "";
    host.innerHTML = `
      <div class="filter-heading"><div><strong>Filters</strong><span class="filter-status" aria-live="polite">All records</span></div><div class="filter-actions"><button type="button" class="copy-view">Copy view link</button><button type="button" class="filter-clear">Clear filters</button><button type="button" class="reset-view">Reset dashboard</button></div></div>
      <div class="filter-controls">${controls}${dateControls}</div>`;
    document.querySelector("nav.tabs")?.insertAdjacentElement("afterend", host);

    const selects = [...host.querySelectorAll("select[data-filter-key]")];
    const fromInput = host.querySelector('[data-date-filter="from"]');
    const toInput = host.querySelector('[data-date-filter="to"]');
    const status = host.querySelector(".filter-status");
    const state = {};
    try { Object.assign(state, JSON.parse(sessionStorage.getItem("dashboardFilters") || "{}")); } catch (_) {}
    const urlState = new URLSearchParams(location.search);

    selects.forEach(select => {
      const requested = urlState.get(`f_${select.dataset.filterKey}`) ?? state[select.dataset.filterKey];
      if ([...select.options].some(option => option.value === requested)) {
        select.value = requested;
      }
    });
    if (fromInput) fromInput.value = urlState.get("from") || "";
    if (toInput) toInput.value = urlState.get("to") || "";

    function persist() {
      const saved = {};
      selects.forEach(select => { if (select.value) saved[select.dataset.filterKey] = select.value; });
      try { sessionStorage.setItem("dashboardFilters", JSON.stringify(saved)); } catch (_) {}
      const urlValues = { from: fromInput?.value, to: toInput?.value };
      selects.forEach(select => { urlValues[`f_${select.dataset.filterKey}`] = select.value; });
      updateUrlState(urlValues);
    }

    function apply() {
      const active = selects.filter(select => select.value);
      const from = fromInput?.value || "";
      const to = toInput?.value || "";
      const filtered = rows.filter(row => {
        const facetsMatch = active.every(select => {
          const definition = definitions.find(item => item.key === select.dataset.filterKey);
          return String(row[definition.sourceKey]) === select.value;
        });
        if (!facetsMatch || !dateColumn) return facetsMatch;
        const date = String(row[dateColumn.key] || "").slice(0, 10);
        return (!from || date >= from) && (!to || date <= to);
      });
      const count = active.length + Number(Boolean(from)) + Number(Boolean(to));
      status.textContent = count ? `${filtered.length.toLocaleString()} records · ${count} active` : "All records";
      persist();
      onChange(filtered, { activeCount: count });
    }

    function set(key, value) {
      const select = selects.find(item => item.dataset.filterKey === key);
      if (!select || ![...select.options].some(option => option.value === String(value))) return;
      select.value = String(value);
      apply();
      host.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    function clear() {
      selects.forEach(select => { select.value = ""; });
      if (fromInput) fromInput.value = "";
      if (toInput) toInput.value = "";
      apply();
    }

    host.addEventListener("change", apply);
    host.querySelector(".filter-clear").addEventListener("click", clear);
    host.querySelector(".copy-view").addEventListener("click", event => copyViewLink(event.currentTarget));
    host.querySelector(".reset-view").addEventListener("click", resetDashboardState);
    document.addEventListener("dashboard:drilldown", event => set(event.detail?.key, event.detail?.value));
    queueMicrotask(apply);
    return { set, clear, apply };
  }

  function alertPanel(items) {
    let host = document.getElementById("dashboard-alerts");
    if (!host) {
      host = document.createElement("section");
      host.id = "dashboard-alerts";
      host.className = "alert-panel";
      host.setAttribute("aria-label", "Dashboard alerts");
      const anchor = document.querySelector(".filter-panel") || document.querySelector("nav.tabs");
      anchor?.insertAdjacentElement("afterend", host);
    }
    const visible = items.filter(item => item && item.count !== 0);
    host.hidden = !visible.length;
    host.innerHTML = visible.map((item, index) => `
      <button type="button" class="dashboard-alert ${escapeHtml(item.tone || "info")}" data-alert-index="${index}">
        <span class="alert-icon" aria-hidden="true">${item.tone === "danger" ? "!" : item.tone === "warning" ? "▲" : "i"}</span>
        <span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.text || "")}</small></span>
      </button>`).join("");
    host.querySelectorAll("[data-alert-index]").forEach(button => {
      const item = visible[Number(button.dataset.alertIndex)];
      button.addEventListener("click", () => openAlertDetail(item, button));
    });
  }

  function openAlertDetail(item, trigger) {
    let shell = document.getElementById("alert-detail-shell");
    if (!shell) {
      shell = document.createElement("div");
      shell.id = "alert-detail-shell";
      shell.className = "detail-shell";
      shell.innerHTML = `<button class="detail-backdrop" type="button" aria-label="Close notification"></button><aside class="alert-detail" role="dialog" aria-modal="true" aria-labelledby="alert-detail-title"><button class="detail-close" type="button" aria-label="Close notification">×</button><div class="alert-detail-visual"><span class="alert-detail-icon" aria-hidden="true"></span></div><div class="alert-detail-body"><span class="alert-eyebrow">Dashboard alert</span><h2 id="alert-detail-title"></h2><p></p><footer><button class="alert-dismiss" type="button">Close</button><button class="alert-action" type="button"></button></footer></div></aside>`;
      document.body.appendChild(shell);
      shell.closePanel = () => {
        shell.classList.remove("open");
        document.body.classList.remove("drawer-open");
        shell.trigger?.focus();
      };
      shell.querySelector(".detail-backdrop").addEventListener("click", shell.closePanel);
      shell.querySelector(".detail-close").addEventListener("click", shell.closePanel);
      shell.querySelector(".alert-dismiss").addEventListener("click", shell.closePanel);
      document.addEventListener("keydown", event => { if (event.key === "Escape" && shell.classList.contains("open")) shell.closePanel(); });
    }
    shell.trigger = trigger;
    const modal = shell.querySelector(".alert-detail");
    const tone = ["danger", "warning", "info"].includes(item.tone) ? item.tone : "info";
    modal.className = `alert-detail ${tone}`;
    shell.querySelector(".alert-detail-icon").textContent = tone === "danger" ? "!" : tone === "warning" ? "▲" : "i";
    shell.querySelector("#alert-detail-title").textContent = item.title;
    shell.querySelector(".alert-detail-body p").textContent = item.text || "Review the affected records for more information.";
    const action = shell.querySelector(".alert-action");
    action.textContent = item.filterKey ? "Show matching records" : "Review affected records";
    action.onclick = () => {
      if (item.filterKey) {
        document.dispatchEvent(new CustomEvent("dashboard:drilldown", { detail: { key: item.filterKey, value: item.filterValue } }));
      } else if (item.href) {
        location.href = item.href;
        return;
      } else {
        document.getElementById("data-table")?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
      shell.closePanel();
    };
    shell.classList.add("open");
    document.body.classList.add("drawer-open");
    shell.querySelector(".detail-close").focus();
  }

  function openDetailDrawer(columns, row) {
    let shell = document.getElementById("detail-shell");
    if (!shell) {
      shell = document.createElement("div");
      shell.id = "detail-shell";
      shell.className = "detail-shell";
      shell.innerHTML = `<button class="detail-backdrop" type="button" aria-label="Close record details"></button><aside class="detail-drawer" role="dialog" aria-modal="true" aria-labelledby="detail-title"><header><div><span>Record details</span><h2 id="detail-title">Details</h2></div><button class="detail-close" type="button" aria-label="Close record details">×</button></header><dl></dl></aside>`;
      document.body.appendChild(shell);
      const close = () => { shell.classList.remove("open"); document.body.classList.remove("drawer-open"); };
      shell.querySelector(".detail-backdrop").addEventListener("click", close);
      shell.querySelector(".detail-close").addEventListener("click", close);
      document.addEventListener("keydown", event => { if (event.key === "Escape" && shell.classList.contains("open")) close(); });
    }
    shell.querySelector("dl").innerHTML = columns.map(column => `<div><dt>${escapeHtml(column.label)}</dt><dd>${formatNumber(row[column.key])}</dd></div>`).join("");
    shell.classList.add("open");
    document.body.classList.add("drawer-open");
    shell.querySelector(".detail-close").focus();
  }

  function exportFileName(extension) {
    const base = document.title.split("·")[0].trim().toLowerCase().replace(/[^a-z0-9]+/g, "-") || "dashboard";
    return `${base}-${new Date().toISOString().slice(0, 10)}.${extension}`;
  }

  function downloadBlob(content, type, filename) {
    const blob = content instanceof Blob ? content : new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function exportCsv(columns, rows) {
    const cell = value => `"${String(value ?? "").replace(/"/g, '""')}"`;
    const csv = [columns.map(column => cell(column.label)).join(","), ...rows.map(row => columns.map(column => cell(row[column.key])).join(","))].join("\r\n");
    downloadBlob(`\ufeff${csv}`, "text/csv;charset=utf-8", exportFileName("csv"));
  }

  function exportExcel(columns, rows) {
    const xml = value => String(value ?? "").replace(/[<>&"']/g, character => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" }[character]));
    const workbookRows = [columns.map(column => column.label), ...rows.map(row => columns.map(column => row[column.key]))];
    const body = workbookRows.map((row, rowIndex) => `<Row>${row.map(value => `<Cell${rowIndex === 0 ? ' ss:StyleID="Header"' : ""}><Data ss:Type="${typeof value === "number" ? "Number" : "String"}">${xml(value)}</Data></Cell>`).join("")}</Row>`).join("");
    const documentXml = `<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Styles><Style ss:ID="Header"><Font ss:Bold="1"/><Interior ss:Color="#DCE9F9" ss:Pattern="Solid"/></Style></Styles><Worksheet ss:Name="Dashboard"><Table>${body}</Table></Worksheet></Workbook>`;
    downloadBlob(documentXml, "application/vnd.ms-excel;charset=utf-8", exportFileName("xls"));
  }

  function exportPdf(columns, rows) {
    const popup = window.open("", "_blank");
    if (!popup) return;
    popup.opener = null;
    const heading = escapeHtml(document.title.split("·")[0].trim());
    const table = `<table><thead><tr>${columns.map(column => `<th>${escapeHtml(column.label)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${columns.map(column => `<td>${escapeHtml(row[column.key] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    popup.document.write(`<!doctype html><html><head><title>${heading}</title><style>@page{size:landscape;margin:12mm}body{font-family:Arial,sans-serif;color:#172033}h1{font-size:18px;margin:0 0 4px}p{font-size:10px;color:#64748b;margin:0 0 14px}table{width:100%;border-collapse:collapse;font-size:7px}th,td{padding:5px;border:1px solid #dbe2ea;text-align:left;vertical-align:top}th{background:#eaf2ff;font-weight:700}tr:nth-child(even){background:#f8fafc}</style></head><body><h1>${heading}</h1><p>Exported ${new Date().toLocaleString()} · ${rows.length.toLocaleString()} records</p>${table}<script>window.onload=()=>{window.print();window.onafterprint=()=>window.close()}<\/script></body></html>`);
    popup.document.close();
  }

  // ---------------- Searchable / sortable / paginated data table ----------------
  function dataTable(container, { columns, rows, pageSize = 12, searchPlaceholder = "Search this table…", pillColumns = [] }) {
    const el = typeof container === "string" ? document.querySelector(container) : container;
    el.removeAttribute("aria-busy");
    const urlState = new URLSearchParams(location.search);
    const requestedSort = urlState.get("sort");
    const state = {
      query: (urlState.get("q") || "").trim().toLowerCase(),
      sortKey: columns.some(column => column.key === requestedSort) ? requestedSort : null,
      sortDir: urlState.get("dir") === "desc" ? -1 : 1,
      page: Math.max(1, Number.parseInt(urlState.get("page") || "1", 10) || 1),
    };
    let tableRows = rows;
    let indexed = indexRows(tableRows);
    let renderedRows = [];
    let hasSetRows = false;

    function indexRows(nextRows) {
      return nextRows.map(row => ({ row, blob: columns.map(c => row[c.key]).join(" ").toLowerCase() }));
    }

    el.innerHTML = `
      <div class="table-toolbar">
        <div class="search-box"><input type="text" placeholder="${escapeHtml(searchPlaceholder)}" aria-label="Search table"></div>
        <div class="table-actions"><span class="result-count"></span><button class="copy-view" type="button">Copy view link</button><button class="export-pdf" type="button">Export PDF</button><button class="export-excel" type="button">Export Excel</button><button class="export-csv" type="button">Export CSV</button><button class="reset-view" type="button">Reset</button></div>
      </div>
      <div class="table-scroll"><table class="data-table"><caption class="sr-only">Dashboard records. Use the column headings to sort, or select a row to open its details.</caption><thead><tr>${columns.map(c =>
        `<th data-key="${escapeHtml(c.key)}" class="${c.type !== "TEXT" ? "num" : ""}" tabindex="0" aria-sort="none">${escapeHtml(c.label)}<span class="sort-arrow" aria-hidden="true">↕</span></th>`).join("")}
      </tr></thead><tbody></tbody></table></div>
      <div class="pagination"><button type="button" data-dir="-1">‹ Prev</button><span class="page-info"></span><button type="button" data-dir="1">Next ›</button></div>`;

    const searchInput = el.querySelector(".search-box input");
    const tbody = el.querySelector("tbody");
    const countEl = el.querySelector(".result-count");
    const pageInfo = el.querySelector(".page-info");
    const headers = [...el.querySelectorAll("thead th")];
    const [prevBtn, nextBtn] = el.querySelectorAll(".pagination button");
    searchInput.value = state.query;

    function currentRows() {
      let filtered = state.query
        ? indexed.filter(item => item.blob.includes(state.query)).map(item => item.row)
        : indexed.map(item => item.row);
      if (state.sortKey) {
        const key = state.sortKey, dir = state.sortDir;
        filtered = filtered.slice().sort((a, b) => {
          const va = a[key], vb = b[key];
          const aMissing = va === null || va === undefined || va === "";
          const bMissing = vb === null || vb === undefined || vb === "";
          if (aMissing && bMissing) return 0;
          // Keep missing values at the bottom in both sort directions.
          if (aMissing) return 1;
          if (bMissing) return -1;
          if (typeof va === "number" && typeof vb === "number") return (va - vb) * dir;
          return String(va).localeCompare(String(vb)) * dir;
        });
      }
      return filtered;
    }

    function render() {
      const filtered = currentRows();
      const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
      state.page = Math.min(state.page, totalPages);
      const start = (state.page - 1) * pageSize;
      const pageRows = filtered.slice(start, start + pageSize);

      countEl.textContent = state.query ? `${filtered.length.toLocaleString()} of ${tableRows.length.toLocaleString()} rows` : `${tableRows.length.toLocaleString()} rows`;

      renderedRows = pageRows;
      tbody.innerHTML = pageRows.length ? pageRows.map((row, index) => `<tr data-record-index="${index}" tabindex="0">${columns.map(c => {
        const value = row[c.key];
        if (pillColumns.includes(c.key) && value) {
          const color = pillColor(value);
          return `<td><span class="pill" style="color:${color};background:${color}1a">${escapeHtml(value)}</span></td>`;
        }
        if ((c.key === "progress" || c.key === "completion") && typeof value === "number") {
          return `<td class="num progress-cell">${progressMarkup(value)}</td>`;
        }
        if (c.key === "actual_spend_sar" && typeof value === "number" && typeof row.budget_sar === "number") {
          return `<td class="num progress-cell">${progressMarkup(value, row.budget_sar, formatNumber(value))}</td>`;
        }
        if (looksLikeDate(value)) return `<td>${escapeHtml(value)}</td>`;
        return `<td class="${c.type !== "TEXT" ? "num" : ""}">${formatNumber(value)}</td>`;
      }).join("")}</tr>`).join("") : `<tr class="empty-row"><td colspan="${columns.length}">No matching rows.</td></tr>`;

      pageInfo.textContent = `Page ${state.page} of ${totalPages}`;
      prevBtn.disabled = state.page <= 1;
      nextBtn.disabled = state.page >= totalPages;
      headers.forEach(th => {
        const isSorted = th.dataset.key === state.sortKey;
        th.classList.toggle("sorted", isSorted);
        th.setAttribute("aria-sort", isSorted ? (state.sortDir === 1 ? "ascending" : "descending") : "none");
        th.querySelector(".sort-arrow").textContent = isSorted ? (state.sortDir === 1 ? "↑" : "↓") : "↕";
      });
      updateUrlState({
        q: state.query,
        sort: state.sortKey,
        dir: state.sortKey && state.sortDir === -1 ? "desc" : null,
        page: state.page > 1 ? state.page : null,
      });
    }

    searchInput.addEventListener("input", debounce((e) => {
      state.query = e.target.value.trim().toLowerCase();
      state.page = 1;
      render();
    }, 150));

    function sortByHeader(th) {
      const key = th.dataset.key;
      state.sortDir = state.sortKey === key ? -state.sortDir : 1;
      state.sortKey = key;
      state.page = 1;
      render();
    }

    headers.forEach(th => {
      th.addEventListener("click", () => sortByHeader(th));
      th.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        sortByHeader(th);
      });
    });

    el.querySelector(".pagination").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      state.page += Number(btn.dataset.dir);
      render();
    });

    tbody.addEventListener("click", event => {
      const rowElement = event.target.closest("tr[data-record-index]");
      if (rowElement) openDetailDrawer(columns, renderedRows[Number(rowElement.dataset.recordIndex)]);
    });
    tbody.addEventListener("keydown", event => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const rowElement = event.target.closest("tr[data-record-index]");
      if (!rowElement) return;
      event.preventDefault();
      openDetailDrawer(columns, renderedRows[Number(rowElement.dataset.recordIndex)]);
    });

    el.querySelector(".export-pdf").addEventListener("click", () => exportPdf(columns, currentRows()));
    el.querySelector(".export-excel").addEventListener("click", () => exportExcel(columns, currentRows()));
    el.querySelector(".export-csv").addEventListener("click", () => exportCsv(columns, currentRows()));
    el.querySelector(".copy-view").addEventListener("click", event => copyViewLink(event.currentTarget));
    el.querySelector(".reset-view").addEventListener("click", resetDashboardState);

    render();
    return {
      setRows(nextRows) {
        tableRows = nextRows;
        indexed = indexRows(tableRows);
        if (hasSetRows) state.page = 1;
        hasSetRows = true;
        render();
      },
    };
  }

  // ---------------- Chat widget (identical assistant used on every page) ----------------
  function initChat() {
    const chatLauncher = document.getElementById("chat-launcher");
    const chatPanel = document.getElementById("chat-panel");
    if (!chatLauncher || !chatPanel) return;
    const chatClose = document.getElementById("chat-close");
    const chatMessages = document.getElementById("chat-messages");
    const chatForm = document.getElementById("chat-form");
    const chatInput = document.getElementById("chat-input");
    const chatSubmit = chatForm.querySelector("button");
    const quickButtons = [...document.querySelectorAll("[data-question]")];
    const chatApi = configuredApi.endsWith("/ask") ? configuredApi : apiUrl("/ask");
    const chatHistory = [];
    let requestPending = false;
    chatPanel.setAttribute("aria-hidden", "true");

    function toggleChat(open, restoreFocus = false) {
      chatPanel.classList.toggle("open", open);
      chatLauncher.setAttribute("aria-expanded", String(open));
      chatPanel.setAttribute("aria-hidden", String(!open));
      if (open) chatInput.focus();
      else if (restoreFocus) chatLauncher.focus();
    }

    function addChatMessage(text, type) {
      const message = document.createElement("div");
      message.className = `chat-message ${type}`;
      message.textContent = text;
      chatMessages.appendChild(message);
      chatMessages.scrollTop = chatMessages.scrollHeight;
      return message;
    }

    function renderBotReply(message, text) {
      const value = String(text || "").trim();
      message.textContent = "";
      message.classList.add("structured");

      const colonIndex = value.indexOf(":");
      const possibleIntro = colonIndex > 0 ? value.slice(0, colonIndex + 1).trim() : "";
      const possibleList = colonIndex > 0 ? value.slice(colonIndex + 1).trim() : value;
      const commaItems = possibleList.split(/,\s*/).map(item => item.trim()).filter(Boolean);
      if (commaItems.length >= 8 && commaItems.every(item => item.length <= 100)) {
        if (possibleIntro) {
          const intro = document.createElement("p");
          intro.className = "chat-intro";
          intro.textContent = possibleIntro;
          message.appendChild(intro);
        }
        const list = document.createElement("ol");
        list.className = "chat-list chat-list-grid";
        commaItems.forEach((item, index) => {
          const entry = document.createElement("li");
          entry.textContent = index === commaItems.length - 1 ? item.replace(/\s*\.\s*$/, "") : item;
          list.appendChild(entry);
        });
        message.appendChild(list);
        return;
      }

      const lines = value.split(/\n+/).map(line => line.trim()).filter(Boolean);
      let activeList = null;
      lines.forEach(line => {
        const match = line.match(/^(?:[-*•]|\d+[.)])\s+(.+)$/);
        if (match) {
          if (!activeList) {
            activeList = document.createElement("ul");
            activeList.className = "chat-list";
            message.appendChild(activeList);
          }
          const entry = document.createElement("li");
          entry.textContent = match[1].replace(/\*\*/g, "");
          activeList.appendChild(entry);
          return;
        }
        activeList = null;
        const paragraph = document.createElement("p");
        paragraph.textContent = line.replace(/\*\*/g, "");
        message.appendChild(paragraph);
      });
      if (!message.childElementCount) message.textContent = value;
    }

    async function askDashboard(question) {
      if (requestPending) return;
      requestPending = true;
      addChatMessage(question, "user");
      chatSubmit.disabled = true;
      quickButtons.forEach(button => { button.disabled = true; });
      const pending = addChatMessage("Checking the dashboard data…", "bot");
      try {
        const response = await fetch(chatApi, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ question, history: chatHistory.slice(-12) }),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.error || "Request failed");
        if (typeof result.reply !== "string" || !result.reply.trim()) throw new Error("The assistant returned an empty response");
        renderBotReply(pending, result.reply);
        chatHistory.push({ role: "user", text: question }, { role: "model", text: result.reply });
      } catch (error) {
        pending.classList.add("error");
        pending.textContent = error.message.includes("GEMINI_API_KEY")
          ? "Gemini is not configured. Set GEMINI_API_KEY before starting server.py."
          : `Dashboard assistant error: ${error.message}`;
      } finally {
        requestPending = false;
        chatSubmit.disabled = false;
        quickButtons.forEach(button => { button.disabled = false; });
        if (chatPanel.classList.contains("open")) chatInput.focus();
      }
    }

    chatLauncher.addEventListener("click", () => toggleChat(!chatPanel.classList.contains("open")));
    chatClose.addEventListener("click", () => toggleChat(false, true));
    chatForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const question = chatInput.value.trim();
      if (!question) return;
      chatInput.value = "";
      askDashboard(question);
    });
    quickButtons.forEach(button => {
      button.addEventListener("click", () => askDashboard(button.dataset.question));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && chatPanel.classList.contains("open")) toggleChat(false, true);
    });
  }

  function showError(container, message) {
    const el = typeof container === "string" ? document.querySelector(container) : container;
    const markup = `<div class="error-state" role="alert"><strong>Unable to load this section</strong><p>${escapeHtml(message)}</p><button type="button" class="retry-page">Retry</button></div>`;
    el.removeAttribute("aria-busy");
    el.innerHTML = markup;
    // Page scripts load all widgets together. If that request fails, replace the
    // remaining permanent loading placeholders instead of leaving a half-loaded UI.
    document.querySelectorAll(".loading-note").forEach(note => { note.outerHTML = markup; });
    document.querySelectorAll("[aria-busy='true']").forEach(node => {
      node.removeAttribute("aria-busy");
      node.innerHTML = markup;
    });
    document.querySelectorAll(".retry-page").forEach(button => button.addEventListener("click", () => location.reload()));
  }

  function initLoadingSkeletons() {
    document.querySelectorAll(".kpi-grid > .loading-note").forEach(note => {
      const grid = note.parentElement;
      grid.setAttribute("aria-busy", "true");
      grid.innerHTML = Array.from({ length: 4 }, () => `<article class="kpi skeleton-kpi" aria-hidden="true"><i></i><b></b><i></i></article>`).join("");
    });
    document.querySelectorAll("#chart-primary, #chart-secondary").forEach(chart => {
      if (!chart.querySelector(".loading-note")) return;
      chart.setAttribute("aria-busy", "true");
      chart.innerHTML = `<div class="skeleton-chart" aria-hidden="true">${Array.from({ length: 5 }, (_, index) => `<i style="width:${55 + index * 9}%"></i>`).join("")}</div>`;
    });
    const table = document.getElementById("data-table");
    if (table?.querySelector(".loading-note")) {
      table.setAttribute("aria-busy", "true");
      table.innerHTML = `<div class="skeleton-table" aria-hidden="true"><i></i>${Array.from({ length: 6 }, () => "<span></span>").join("")}</div>`;
    }
  }

  function initThemeToggle() {
    const topbar = document.querySelector(".topbar");
    if (!topbar || topbar.querySelector(".theme-toggle")) return;
    let actions = topbar.querySelector(".topbar-actions");
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "topbar-actions";
      topbar.appendChild(actions);
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "theme-toggle";
    actions.appendChild(button);

    function update() {
      const dark = document.documentElement.dataset.theme === "dark";
      button.innerHTML = `<span aria-hidden="true">${dark ? "☀" : "☾"}</span><b>${dark ? "Light mode" : "Dark mode"}</b>`;
      button.setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} mode`);
      button.setAttribute("aria-pressed", String(dark));
    }

    button.addEventListener("click", () => {
      const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(theme);
      try { localStorage.setItem("dashboardTheme", theme); } catch (_) {}
      update();
    });
    update();
  }

  function initAuthActions() {
    const topbar = document.querySelector(".topbar");
    if (!topbar || topbar.querySelector(".auth-actions")) return;
    let actions = topbar.querySelector(".topbar-actions");
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "topbar-actions";
      topbar.appendChild(actions);
    }
    const auth = document.createElement("div");
    auth.className = "auth-actions";
    actions.prepend(auth);

    const currentPage = location.pathname.split("/").pop() || "index.html";
    const loginActive = currentPage === "login.html";
    const signupActive = currentPage === "signup.html";

    function renderGuest() {
      auth.innerHTML = `
        <a class="auth-link${loginActive ? " active" : ""}" href="login.html">Login</a>
        <a class="auth-link${signupActive ? " active" : ""}" href="signup.html">Sign Up</a>
      `;
    }

    async function render() {
      try {
        const data = await fetchJSON(apiUrl("/auth/me"));
        if (!data.authenticated) {
          renderGuest();
          return;
        }
        applyRoleNavigation(data.user);
        auth.innerHTML = `
          <span class="auth-user"><strong>${escapeHtml(data.user.name)}</strong><small>${escapeHtml(data.user.email)}</small></span>
          <form class="auth-logout-form" method="post" action="${escapeHtml(apiUrl("/auth/logout"))}">
            <button type="submit" class="auth-logout">Logout</button>
          </form>
        `;
      } catch (_) {
        renderGuest();
      }
    }

    render();
  }

  function initFreshnessIndicator() {
    const actions = document.querySelector(".topbar-actions");
    if (!actions || actions.querySelector(".freshness-indicator")) return;
    const indicator = document.createElement("button");
    indicator.type = "button";
    indicator.className = "freshness-indicator checking";
    indicator.innerHTML = `<i aria-hidden="true"></i><span><small>Data freshness</small><strong>Checking…</strong></span>`;
    actions.prepend(indicator);
    let lastVersion = null;

    function relativeTime(value) {
      if (!value) return "Not available";
      const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
      if (seconds < 10) return "Updated just now";
      if (seconds < 60) return `Updated ${seconds}s ago`;
      const minutes = Math.floor(seconds / 60);
      if (minutes < 60) return `Updated ${minutes}m ago`;
      const hours = Math.floor(minutes / 60);
      return `Updated ${hours}h ago`;
    }

    async function check() {
      indicator.classList.add("checking");
      try {
        const data = await fetchJSON(apiUrl("/api/freshness"));
        indicator.className = `freshness-indicator ${data.in_sync ? "fresh" : "stale"}`;
        indicator.querySelector("strong").textContent = relativeTime(data.last_synced);
        indicator.title = `Workbook rows: ${Number(data.workbook_rows || 0).toLocaleString()}\nLast synchronized: ${data.last_synced || "Unknown"}`;
        if (lastVersion && data.last_synced && data.last_synced !== lastVersion) {
          indicator.querySelector("strong").textContent = "New data loaded";
          setTimeout(() => location.reload(), 700);
        }
        lastVersion = data.last_synced || lastVersion;
      } catch (error) {
        indicator.className = "freshness-indicator stale";
        indicator.querySelector("strong").textContent = "Sync unavailable";
        indicator.title = error.message;
      }
    }
    indicator.addEventListener("click", check);
    check();
    setInterval(check, 20000);
  }

  function initGlobalSearch() {
    const topbar = document.querySelector(".topbar");
    const nav = document.querySelector("nav.tabs");
    if (!topbar || document.querySelector(".global-search")) return;
    const search = document.createElement("section");
    search.className = "global-search";
    search.setAttribute("role", "search");
    search.innerHTML = `<label><span class="sr-only">Search all dashboard data</span><i aria-hidden="true">⌕</i><input type="search" autocomplete="off" placeholder="Search employees, projects, tasks, meetings, and more…" aria-controls="global-search-results" aria-expanded="false"><kbd>Ctrl K</kbd></label><div class="global-search-results" id="global-search-results" hidden><div class="global-search-status" aria-live="polite"></div><div class="global-search-groups"></div></div>`;
    (nav || topbar).insertAdjacentElement("afterend", search);
    const input = search.querySelector("input");
    const panel = search.querySelector(".global-search-results");
    const status = search.querySelector(".global-search-status");
    const groups = search.querySelector(".global-search-groups");
    let requestNumber = 0;

    function close() {
      panel.hidden = true;
      input.setAttribute("aria-expanded", "false");
    }

    function render(data, query) {
      status.textContent = data.total ? `${data.total.toLocaleString()} matches across the dashboard` : `No results for “${query}”`;
      groups.textContent = "";
      const grouped = new Map();
      (data.results || []).forEach(result => {
        if (!grouped.has(result.sheet_name)) grouped.set(result.sheet_name, []);
        grouped.get(result.sheet_name).push(result);
      });
      grouped.forEach((results, sheetName) => {
        const group = document.createElement("section");
        const heading = document.createElement("h3");
        heading.textContent = sheetName;
        group.appendChild(heading);
        results.forEach(result => {
          const link = document.createElement("a");
          link.href = `${result.page}?q=${encodeURIComponent(query)}`;
          const title = document.createElement("strong");
          title.textContent = result.title;
          const detail = document.createElement("span");
          detail.textContent = (result.details || []).join(" · ");
          link.append(title, detail);
          group.appendChild(link);
        });
        groups.appendChild(group);
      });
      panel.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    const searchAll = debounce(async () => {
      const query = input.value.trim();
      const current = ++requestNumber;
      if (query.length < 2) { close(); return; }
      panel.hidden = false;
      status.textContent = "Searching…";
      groups.textContent = "";
      try {
        const data = await fetchJSON(apiUrl(`/api/search?q=${encodeURIComponent(query)}`));
        if (current === requestNumber) render(data, query);
      } catch (error) {
        status.textContent = error.message;
      }
    }, 220);
    input.addEventListener("input", searchAll);
    input.addEventListener("keydown", event => {
      if (event.key === "Escape") { input.value = ""; close(); }
      if (event.key === "ArrowDown") { event.preventDefault(); panel.querySelector("a")?.focus(); }
    });
    panel.addEventListener("keydown", event => { if (event.key === "Escape") { close(); input.focus(); } });
    document.addEventListener("keydown", event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); input.focus(); }
    });
    document.addEventListener("click", event => { if (!search.contains(event.target)) close(); });
  }

  function initMobileNavigation() {
    const nav = document.querySelector("nav.tabs");
    if (!nav || document.querySelector(".mobile-menu-toggle")) return;
    nav.id = nav.id || "dashboard-navigation";
    const current = nav.querySelector("a.active")?.textContent.trim() || "Dashboard";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "mobile-menu-toggle";
    button.setAttribute("aria-controls", nav.id);
    button.setAttribute("aria-expanded", "false");
    button.innerHTML = `<span><small>Section</small><strong>${escapeHtml(current)}</strong></span><i aria-hidden="true">⌄</i>`;
    nav.insertAdjacentElement("beforebegin", button);

    function close() {
      nav.classList.remove("mobile-open");
      button.setAttribute("aria-expanded", "false");
    }
    button.addEventListener("click", () => {
      const open = !nav.classList.contains("mobile-open");
      nav.classList.toggle("mobile-open", open);
      button.setAttribute("aria-expanded", String(open));
    });
    nav.addEventListener("click", event => { if (event.target.closest("a")) close(); });
    matchMedia("(min-width: 701px)").addEventListener?.("change", event => { if (event.matches) close(); });
  }

  function initAccessibility() {
    const main = document.querySelector("main");
    if (!main) return;
    main.id = main.id || "dashboard-main";
    main.tabIndex = -1;
    if (!document.querySelector(".skip-link")) {
      const skip = document.createElement("a");
      skip.className = "skip-link";
      skip.href = `#${main.id}`;
      skip.textContent = "Skip to dashboard content";
      document.body.prepend(skip);
    }
  }

  return { CHART_COLORS, pillColor, escapeHtml, formatNumber, debounce, fetchTables, fetchData, sparklineSeries, kpiGrid, barChart, donutChart, trendChart, filterBar, alertPanel, dataTable, initChat, showError, initLoadingSkeletons, initThemeToggle, initAuthActions, initFreshnessIndicator, initGlobalSearch, initMobileNavigation, initAccessibility };
})();

document.addEventListener("DOMContentLoaded", () => {
  const isAuthPage = document.body.classList.contains("auth-page");
  App.initAccessibility();
  App.initAuthActions();
  App.initThemeToggle();
  App.initMobileNavigation();
  if (!isAuthPage) {
    App.initFreshnessIndicator();
    App.initGlobalSearch();
    App.initLoadingSkeletons();
    App.initChat();
  }
  document.querySelector("nav.tabs a.active")?.setAttribute("aria-current", "page");
});
