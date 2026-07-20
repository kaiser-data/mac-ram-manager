/* RAM Manager dashboard: polls /api/stats, renders tiles, history chart,
   process groups, brew services, and launchd startup items. */
"use strict";

const REFRESH_MS = 5000;
const GB = 1024 ** 3;
const MB = 1024 ** 2;

const CHART = { width: 1040, height: 220, top: 12, right: 16, bottom: 24, left: 46 };
const PRESSURE = {
  1: { cls: "good", icon: "✓", text: "Pressure: normal" },
  2: { cls: "warning", icon: "▲", text: "Pressure: warning" },
  4: { cls: "critical", icon: "✕", text: "Pressure: critical" },
};

const $ = (id) => document.getElementById(id);
let latest = null;

/* ---------- helpers ---------- */

function fmtBytes(bytes) {
  if (bytes >= GB) return (bytes / GB).toFixed(2) + " GB";
  if (bytes >= MB) return Math.round(bytes / MB) + " MB";
  return Math.round(bytes / 1024) + " KB";
}

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  toast.style.background = isError ? "var(--status-critical)" : "";
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-RAM-Token": window.RAM_TOKEN,
      ...(options.headers || {}),
    },
  });
  const payload = await response.json();
  if (!payload.ok) throw new Error(payload.error || "request failed");
  return payload.data;
}

async function runAction(path, body, confirmText) {
  if (confirmText && !window.confirm(confirmText)) return;
  try {
    await api(path, { method: "POST", body: JSON.stringify(body || {}) });
    showToast("Done");
    await refresh();
  } catch (error) {
    showToast(error.message, true);
  }
}

/* ---------- KPI tiles ---------- */

function renderTiles(data) {
  const { memory, swap, pressure } = data;

  $("mem-used").textContent = (memory.usedBytes / GB).toFixed(1) + " GB";
  $("mem-total").textContent = (memory.totalBytes / GB).toFixed(0) + " GB";
  const memPct = (memory.usedBytes / memory.totalBytes) * 100;
  const memMeter = $("mem-meter");
  memMeter.style.width = memPct.toFixed(1) + "%";
  memMeter.classList.toggle("hot", memPct > 90);
  $("mem-detail").textContent =
    `app ${fmtBytes(memory.appBytes)} · wired ${fmtBytes(memory.wiredBytes)}`;

  $("swap-used").textContent = (swap.usedBytes / GB).toFixed(1) + " GB";
  $("swap-total").textContent = (swap.totalBytes / GB).toFixed(1) + " GB";
  const swapPct = swap.totalBytes ? (swap.usedBytes / swap.totalBytes) * 100 : 0;
  const swapMeter = $("swap-meter");
  swapMeter.style.width = swapPct.toFixed(1) + "%";
  swapMeter.classList.toggle("hot", swapPct > 75);
  $("swap-detail").textContent =
    `${memory.swapOuts.toLocaleString()} swap-outs since boot`;

  $("compressed").textContent = fmtBytes(memory.compressedBytes);
  $("cached").textContent = fmtBytes(memory.cachedBytes);

  const level = PRESSURE[pressure.level] || PRESSURE[1];
  $("pressure-pill").className = "pill " + level.cls;
  $("pressure-icon").textContent = level.icon;
  $("pressure-text").textContent = level.text;
  $("updated").textContent =
    "updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/* ---------- history chart ---------- */

function renderChart(history) {
  const svg = $("history-chart");
  const { width, height, top, right, bottom, left } = CHART;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  if (!history.length) { svg.innerHTML = ""; return; }

  const plotW = width - left - right;
  const plotH = height - top - bottom;
  const maxY = Math.max(...history.map((s) => s.usedBytes), 1) * 1.12;
  const t0 = history[0].t;
  const t1 = history[history.length - 1].t;
  const span = Math.max(t1 - t0, 1);

  const x = (t) => left + ((t - t0) / span) * plotW;
  const y = (v) => top + plotH - (v / maxY) * plotH;
  const path = (key) =>
    history.map((s, i) => `${i ? "L" : "M"}${x(s.t).toFixed(1)},${y(s[key]).toFixed(1)}`).join("");

  const gridLines = [0.25, 0.5, 0.75, 1].map((f) => {
    const gy = y(maxY * f).toFixed(1);
    return `<line x1="${left}" x2="${width - right}" y1="${gy}" y2="${gy}" stroke="var(--grid)" stroke-width="1"/>
      <text x="${left - 8}" y="${gy}" dy="0.32em" text-anchor="end" fill="var(--muted)" font-size="10">${(maxY * f / GB).toFixed(1)}G</text>`;
  }).join("");

  const last = history[history.length - 1];
  svg.innerHTML = `
    ${gridLines}
    <line x1="${left}" x2="${width - right}" y1="${top + plotH}" y2="${top + plotH}" stroke="var(--baseline)" stroke-width="1"/>
    <path d="${path("usedBytes")}" fill="none" stroke="var(--series-mem)" stroke-width="2" stroke-linejoin="round"/>
    <path d="${path("swapUsedBytes")}" fill="none" stroke="var(--series-swap)" stroke-width="2" stroke-linejoin="round"/>
    <text x="${width - right - 4}" y="${y(last.usedBytes) - 8}" text-anchor="end" fill="var(--series-mem)" font-size="11" font-weight="600">${(last.usedBytes / GB).toFixed(1)} GB</text>
    <text x="${width - right - 4}" y="${y(last.swapUsedBytes) - 8}" text-anchor="end" fill="var(--series-swap)" font-size="11" font-weight="600">${(last.swapUsedBytes / GB).toFixed(1)} GB</text>
    <line id="crosshair" y1="${top}" y2="${top + plotH}" stroke="var(--baseline)" stroke-width="1" visibility="hidden"/>
  `;
  attachHover(svg, history, x, y);
}

function attachHover(svg, history, x, y) {
  const tooltip = $("chart-tooltip");
  const crosshair = svg.querySelector("#crosshair");
  const wrap = $("chart-wrap");

  const onMove = (event) => {
    const rect = svg.getBoundingClientRect();
    const scale = CHART.width / rect.width;
    const svgX = (event.clientX - rect.left) * scale;
    let nearest = history[0];
    for (const sample of history) {
      if (Math.abs(x(sample.t) - svgX) < Math.abs(x(nearest.t) - svgX)) nearest = sample;
    }
    const cx = x(nearest.t);
    crosshair.setAttribute("x1", cx);
    crosshair.setAttribute("x2", cx);
    crosshair.setAttribute("visibility", "visible");

    const when = new Date(nearest.t * 1000)
      .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    tooltip.innerHTML = `<strong>${when}</strong>
      <div class="tt-row"><span class="swatch swatch-mem"></span>Memory ${(nearest.usedBytes / GB).toFixed(2)} GB</div>
      <div class="tt-row"><span class="swatch swatch-swap"></span>Swap ${(nearest.swapUsedBytes / GB).toFixed(2)} GB</div>`;
    tooltip.hidden = false;
    const wrapRect = wrap.getBoundingClientRect();
    const px = (cx / CHART.width) * rect.width;
    tooltip.style.left = Math.min(px + 14, wrapRect.width - tooltip.offsetWidth - 8) + "px";
    tooltip.style.top = "14px";
  };

  svg.onmousemove = onMove;
  svg.onmouseleave = () => {
    tooltip.hidden = true;
    crosshair.setAttribute("visibility", "hidden");
  };
}

/* ---------- process groups ---------- */

function renderProcesses(groups) {
  const container = $("process-list");
  const maxRss = Math.max(...groups.map((g) => g.rssBytes), 1);
  container.replaceChildren(...groups.map((group) => {
    const row = document.createElement("div");
    row.className = "proc-row";

    const name = document.createElement("div");
    name.className = "proc-name";
    name.textContent = group.name;
    name.title = group.name;
    const count = document.createElement("span");
    count.className = "proc-count";
    count.textContent = ` · ${group.processCount} proc${group.processCount > 1 ? "s" : ""}`;
    name.appendChild(count);

    const track = document.createElement("div");
    track.className = "proc-bar-track";
    const bar = document.createElement("div");
    bar.className = "proc-bar";
    bar.style.width = ((group.rssBytes / maxRss) * 100).toFixed(1) + "%";
    track.appendChild(bar);

    const mem = document.createElement("div");
    mem.className = "proc-mem";
    mem.textContent = fmtBytes(group.rssBytes);

    const actionsBox = document.createElement("div");
    actionsBox.className = "proc-actions";
    if (group.killable) {
      actionsBox.append(
        makeButton("Quit", "btn", () => runAction("/api/kill-group",
          { name: group.name },
          `Quit all ${group.processCount} '${group.name}' processes?`)),
        makeButton("Relaunch", "btn", () => runAction("/api/relaunch-group",
          { name: group.name },
          `Restart '${group.name}'? It will quit and reopen.`)),
        makeButton("Force", "btn btn-danger", () => runAction("/api/kill-group",
          { name: group.name, force: true },
          `FORCE KILL '${group.name}'? Unsaved data will be lost.`)),
      );
    }

    row.append(name, track, mem, actionsBox);
    return row;
  }));
}

function makeButton(label, className, onClick) {
  const button = document.createElement("button");
  button.className = className;
  button.textContent = label;
  button.onclick = onClick;
  return button;
}

/* ---------- brew services ---------- */

function renderServices(services) {
  const container = $("service-list");
  if (!services.length) {
    container.innerHTML = '<p class="muted">no Homebrew services found</p>';
    return;
  }
  container.replaceChildren(...services.map((service) => {
    const row = document.createElement("div");
    row.className = "service-row";

    const label = document.createElement("span");
    label.textContent = service.name + " ";
    const status = document.createElement("span");
    const isOn = ["started", "running", "scheduled"].includes(service.status);
    status.className = "svc-status " + (isOn ? "on" : "off");
    status.textContent = isOn ? "● " + service.status : "○ " + service.status;
    label.appendChild(status);

    const buttons = document.createElement("div");
    buttons.className = "proc-actions";
    if (isOn) {
      buttons.append(
        makeButton("Stop", "btn", () => runAction("/api/service",
          { name: service.name, action: "stop" },
          `Stop service '${service.name}'? This also removes it from autostart.`)),
        makeButton("Restart", "btn", () => runAction("/api/service",
          { name: service.name, action: "restart" }, null)),
      );
    } else {
      buttons.append(makeButton("Start", "btn", () => runAction("/api/service",
        { name: service.name, action: "start" }, null)));
    }

    row.append(label, buttons);
    return row;
  }));
}

/* ---------- launchd startup items ---------- */

function renderAgents(agents) {
  const container = $("agent-list");
  if (!container) return;
  const manageable = agents.filter((agent) => !agent.protected);
  if (!manageable.length) {
    container.innerHTML = '<p class="muted">no third-party startup items</p>';
    return;
  }
  container.replaceChildren(...manageable.map((agent) => {
    const row = document.createElement("div");
    row.className = "agent-row";

    const info = document.createElement("div");
    info.className = "agent-label";
    info.textContent = agent.label;
    info.title = agent.path;
    const meta = document.createElement("div");
    meta.className = "agent-meta";
    meta.textContent = [
      agent.running ? `running (pid ${agent.pid})` : "not running",
      agent.disabled ? "autostart off" : "autostart on",
    ].join(" · ");
    info.appendChild(document.createElement("br"));
    info.appendChild(meta);

    const buttons = document.createElement("div");
    buttons.className = "proc-actions";
    buttons.append(
      agent.running
        ? makeButton("Stop", "btn", () => runAction("/api/launchd",
            { label: agent.label, action: "stop" },
            `Stop '${agent.label}' now?`))
        : makeButton("Start", "btn", () => runAction("/api/launchd",
            { label: agent.label, action: "start" }, null)),
      agent.disabled
        ? makeButton("Enable autostart", "btn", () => runAction("/api/launchd",
            { label: agent.label, action: "enable" }, null))
        : makeButton("Disable autostart", "btn btn-danger", () => runAction("/api/launchd",
            { label: agent.label, action: "disable" },
            `Stop '${agent.label}' from starting at login?`)),
    );

    row.append(info, buttons);
    return row;
  }));
}

/* ---------- main loop ---------- */

async function refresh() {
  try {
    latest = await api("/api/stats");
    renderTiles(latest);
    renderChart(latest.history || []);
    renderProcesses(latest.topGroups || []);
    renderServices(latest.services || []);
    renderAgents(latest.agents || []);
  } catch (error) {
    showToast("stats refresh failed: " + error.message, true);
  }
}

$("purge-btn").onclick = () =>
  runAction("/api/purge", {}, "Purge the disk cache now?");

refresh();
setInterval(refresh, REFRESH_MS);
