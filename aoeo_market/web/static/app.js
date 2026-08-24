/* AoEO Market dashboard — single-page app backed by the /api JSON endpoints. */

"use strict";

const $ = (sel) => document.querySelector(sel);
const charts = {};
const RARITY_COLORS = {
  Junk: "#64748b",
  Common: "#94a3b8",
  Uncommon: "#4ade80",
  Rare: "#38bdf8",
  Epic: "#a78bfa",
  Legendary: "#fbbf24",
  unknown: "#64748b",
};

Chart.defaults.color = "#cbd5e1";
Chart.defaults.borderColor = "rgba(148,163,184,0.15)";
Chart.defaults.font.family = "'Segoe UI', system-ui, sans-serif";

async function api(path) {
  const r = await fetch(path);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

function fmtPrice(n) {
  if (n == null) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
const fmtInt = (n) => (n == null ? "—" : n.toLocaleString("en-US"));
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" }) : "—");
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleDateString() : "—");
const fmtDays = (s) => (s == null ? "—" : (s / 86400).toFixed(1) + "d");
const fmtDur = (s) => {
  if (s == null) return "—";
  const h = s / 3600;
  return h < 48 ? h.toFixed(1) + " h" : (h / 24).toFixed(1) + " d";
};
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function itemLink(itemId, name) {
  const label = name || itemId;
  const title = name ? ` title="${esc(itemId)}"` : "";
  return `<a href="#item/${encodeURIComponent(itemId)}" class="item-link"${title}>${esc(label)}</a>`;
}

function rarityBadge(name) {
  if (!name) return "";
  const color = RARITY_COLORS[name] || RARITY_COLORS.unknown;
  return `<span class="badge" style="color:${color};border-color:${color}">${esc(name)}</span>`;
}

function makeChart(canvasId, config) {
  if (charts[canvasId]) charts[canvasId].destroy();
  charts[canvasId] = new Chart($(canvasId), config);
}

/* --- tabs ---------------------------------------------------------------- */

function showTab(name) {
  document.querySelectorAll("main > section").forEach((s) => (s.hidden = s.id !== "tab-" + name));
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
}

document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => {
    if (b.dataset.tab === "item") return;
    history.replaceState(null, "", window.location.pathname);
    showTab(b.dataset.tab);
  })
);

/* --- overview ------------------------------------------------------------ */

async function loadOverview() {
  const o = await api("/api/overview");
  $("#empty-banner").hidden = o.latest !== null;
  $("#kpi-listings").textContent = fmtInt(o.active_listings);
  $("#kpi-items").textContent = fmtInt(o.distinct_items);
  $("#kpi-snapshots").textContent = fmtInt(o.snapshot_count);
  $("#kpi-last").textContent = o.latest ? fmtTime(o.latest.captured_at) : "—";
  $("#snapshot-info").textContent = o.latest ? `snapshot ${fmtTime(o.latest.captured_at)} · ${fmtInt(o.active_listings)} listings` : "no data yet";

  makeChart("#chart-supply", {
    type: "line",
    data: {
      labels: o.supply_history.map((s) => fmtTime(s.t)),
      datasets: [{
        label: "active listings",
        data: o.supply_history.map((s) => s.count),
        borderColor: "#fbbf24",
        backgroundColor: "rgba(251,191,36,0.08)",
        fill: true,
        tension: 0.25,
        pointRadius: 0,
      }],
    },
    options: { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } },
  });

  makeChart("#chart-prices", {
    type: "bar",
    data: {
      labels: o.price_distribution.map((b) => b.label),
      datasets: [{ label: "listings", data: o.price_distribution.map((b) => b.count), backgroundColor: "#38bdf8" }],
    },
    options: { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } },
  });

  makeChart("#chart-types", {
    type: "doughnut",
    data: {
      labels: o.type_breakdown.map((t) => t.name),
      datasets: [{ data: o.type_breakdown.map((t) => t.count), backgroundColor: ["#fbbf24", "#38bdf8", "#a78bfa", "#4ade80", "#f472b6"] }],
    },
  });

  makeChart("#chart-rarity", {
    type: "bar",
    data: {
      labels: o.rarity_breakdown.map((r) => r.name),
      datasets: [{
        data: o.rarity_breakdown.map((r) => r.count),
        backgroundColor: o.rarity_breakdown.map((r) => RARITY_COLORS[r.name] || RARITY_COLORS.unknown),
      }],
    },
    options: { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } },
  });

  $("#movers").innerHTML = o.top_movers
    .map(
      (m) => `<tr>
        <td>${itemLink(m.item_id, m.name)}</td>
        <td class="num">${fmtPrice(m.median_before)}</td>
        <td class="num">${fmtPrice(m.median_now)}</td>
        <td class="num ${m.change_pct >= 0 ? "up" : "down"}">${m.change_pct >= 0 ? "+" : ""}${m.change_pct}%</td>
      </tr>`
    )
    .join("") || '<tr><td colspan="4" class="muted">need at least two data points</td></tr>';
}

/* --- listings ------------------------------------------------------------ */

let listingsCache = [];

async function loadListings() {
  listingsCache = await api("/api/listings");
  const types = [...new Set(listingsCache.map((l) => l.item_type))].sort();
  const sel = $("#ls-type");
  sel.innerHTML = '<option value="">all types</option>' + types.map((t) => `<option>${esc(t)}</option>`).join("");
  renderListings();
}

let lsDir = -1; // price desc default
function renderListings() {
  const q = $("#ls-q").value.trim().toLowerCase();
  const type = $("#ls-type").value;
  const sort = $("#ls-sort").value;
  const rows = listingsCache
    .filter((l) => (!type || l.item_type === type) && (!q || l.item_id.toLowerCase().includes(q) || (l.name && l.name.toLowerCase().includes(q))))
    .sort((a, b) => {
      let r = 0;
      if (sort === "item" || sort === "type" || sort === "seller") r = String(a[sort === "item" ? "item_id" : sort === "seller" ? "seller_empire_id" : "item_type"]).localeCompare(String(b[sort === "item" ? "item_id" : sort === "seller" ? "seller_empire_id" : "item_type"]));
      else if (sort === "price") r = a.unit_price - b.unit_price;
      else r = a[sort === "expiry" ? "seconds_till_expiry" : "item_" + sort] - b[sort === "expiry" ? "seconds_till_expiry" : "item_" + sort];
      return r * lsDir;
    });
  $("#ls-count").textContent = `${rows.length} / ${listingsCache.length} listings`;
  $("#listings-body").innerHTML = rows
    .map(
      (l) => `<tr>
        <td>${itemLink(l.item_id, l.name)} ${rarityBadge(l.rarity)}</td>
        <td>${esc(l.item_type)}</td>
        <td class="num">${l.item_level}</td>
        <td class="num">${l.item_count}</td>
        <td class="num">${fmtPrice(l.unit_price)}${l.item_count > 1 ? ` <span class="muted">(×${l.item_count})</span>` : ""}</td>
        <td class="num">${fmtDays(l.seconds_till_expiry)}</td>
        <td>${esc(String(l.seller_empire_id))}</td>
      </tr>`
    )
    .join("");
}

$("#ls-q").addEventListener("input", renderListings);
$("#ls-type").addEventListener("change", renderListings);
$("#ls-sort").addEventListener("change", () => (lsDir = -1, $("#ls-dir").textContent = "↓", renderListings()));
$("#ls-dir").addEventListener("click", () => (lsDir = -lsDir, $("#ls-dir").textContent = lsDir < 0 ? "↓" : "↑", renderListings()));

/* --- best sellers -------------------------------------------------------- */

let bestOrder = "median_time";
let bestDir = "asc";

async function loadBestSellersChart() {
  const rows = await api("/api/best-sellers?order=median_time&dir=asc");
  const top = rows.slice(0, 10).reverse(); // fastest at the top
  makeChart("#chart-best-sellers", {
    type: "bar",
    data: {
      labels: top.map((r) => {
        const label = r.name || r.item_id;
        return label.length > 26 ? label.slice(0, 26) + "…" : label;
      }),
      datasets: [{ label: "median time-to-sale", data: top.map((r) => r.median_time / 3600), backgroundColor: "#4ade80" }],
    },
    options: {
      indexAxis: "y",
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (i) => fmtDur(i.parsed.x * 3600) } } },
      scales: { x: { title: { display: true, text: "hours" }, beginAtZero: true } },
    },
  });
}

async function loadBestSellers() {
  const rows = await api(`/api/best-sellers?order=${bestOrder}&dir=${bestDir}`);
  document.querySelectorAll("#tab-best-sellers th a").forEach((a) => a.classList.toggle("active", a.dataset.order === bestOrder));
  $("#best-body").innerHTML = rows
    .map(
      (r) => `<tr>
        <td>${itemLink(r.item_id, r.name)} ${rarityBadge(r.rarity)}</td>
        <td>${esc(r.item_type)}</td>
        <td class="num">${r.item_level}</td>
        <td>${esc(r.rarity || "—")}</td>
        <td class="num"><b>${fmtDur(r.median_time)}</b></td>
        <td class="num">${fmtDur(r.min_time)}</td>
        <td class="num">${fmtDur(r.max_time)}</td>
        <td class="num">${fmtInt(r.timed_sales)}</td>
        <td class="num">${fmtInt(r.expired)}</td>
        <td class="num">${fmtInt(r.active_count)}</td>
        <td class="num">${fmtPrice(r.current_median_unit_price)}</td>
      </tr>`
    )
    .join("") || '<tr><td colspan="11" class="muted">no fully observed sales yet — this view fills in as more data is collected</td></tr>';
}

document.querySelectorAll("#tab-best-sellers th a").forEach((a) =>
  a.addEventListener("click", () => {
    if (bestOrder === a.dataset.order) bestDir = bestDir === "asc" ? "desc" : "asc";
    else (bestOrder = a.dataset.order), (bestDir = a.dataset.order === "median_time" || a.dataset.order === "min_time" ? "asc" : "desc");
    loadBestSellers();
  })
);

/* --- best value ---------------------------------------------------------- */

let valueOrder = "value_ratio";
let valueDir = "desc";
const fmtRatio = (r) => (r == null ? "—" : (r >= 10 ? r.toFixed(0) : r.toFixed(1)) + "×");

async function loadBestValueChart() {
  const rows = await api("/api/best-value?order=value_ratio&dir=desc");
  const top = rows.slice(0, 10).reverse();
  makeChart("#chart-best-value", {
    type: "bar",
    data: {
      labels: top.map((r) => {
        const label = r.name || r.item_id;
        return label.length > 26 ? label.slice(0, 26) + "…" : label;
      }),
      datasets: [{ label: "value ratio", data: top.map((r) => r.value_ratio), backgroundColor: "#a78bfa" }],
    },
    options: {
      indexAxis: "y",
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (i) => fmtRatio(i.parsed.x) } } },
      scales: { x: { title: { display: true, text: "× cheaper than typical rarity price" }, beginAtZero: true } },
    },
  });
}

async function loadBestValue() {
  const rows = await api(`/api/best-value?order=${valueOrder}&dir=${valueDir}`);
  document.querySelectorAll("#tab-best-value th a").forEach((a) => a.classList.toggle("active", a.dataset.order === valueOrder));
  $("#value-body").innerHTML = rows
    .map(
      (r) => `<tr>
        <td>${itemLink(r.item_id, r.name)}</td>
        <td>${esc(r.item_type)}</td>
        <td class="num">${r.item_level}</td>
        <td>${rarityBadge(r.rarity)}</td>
        <td class="num"><b>${fmtRatio(r.value_ratio)}</b></td>
        <td class="num">${fmtPrice(r.median_unit_price)}</td>
        <td class="num">${fmtPrice(r.current_median_unit_price)}</td>
        <td class="num">${fmtPrice(r.current_min_unit_price)}</td>
        <td class="num">${r.cheaper_than_pct}%</td>
        <td class="num">${fmtInt(r.active_count)}</td>
      </tr>`
    )
    .join("") || '<tr><td colspan="10" class="muted">no rarity-tagged items observed yet</td></tr>';
}

document.querySelectorAll("#tab-best-value th a").forEach((a) =>
  a.addEventListener("click", () => {
    if (valueOrder === a.dataset.order) valueDir = valueDir === "asc" ? "desc" : "asc";
    else (valueOrder = a.dataset.order), (valueDir = "desc");
    loadBestValue();
  })
);

/* --- not on sale --------------------------------------------------------- */

let nosOrder = "median_unit_price";
let nosDir = "desc";

async function loadNotOnSale() {
  const rows = await api(`/api/not-on-sale?order=${nosOrder}&dir=${nosDir}`);
  document.querySelectorAll("#tab-not-on-sale th a").forEach((a) => a.classList.toggle("active", a.dataset.order === nosOrder));
  $("#nos-body").innerHTML = rows
    .map(
      (r) => `<tr>
        <td>${itemLink(r.item_id, r.name)} ${rarityBadge(r.rarity)}</td>
        <td>${esc(r.item_type)}</td>
        <td class="num">${r.item_level}</td>
        <td>${esc(r.rarity || "—")}</td>
        <td class="num">${fmtPrice(r.median_unit_price)}</td>
        <td class="num">${fmtPrice(r.min_unit_price)}</td>
        <td class="num">${fmtPrice(r.max_unit_price)}</td>
        <td class="num">${fmtInt(r.times_listed)}</td>
        <td class="num">${fmtTime(r.last_seen)}</td>
      </tr>`
    )
    .join("") || '<tr><td colspan="9" class="muted">nothing here — every known item is currently listed</td></tr>';
}

document.querySelectorAll("#tab-not-on-sale th a").forEach((a) =>
  a.addEventListener("click", () => {
    if (nosOrder === a.dataset.order) nosDir = nosDir === "desc" ? "asc" : "desc";
    else (nosOrder = a.dataset.order), (nosDir = "desc");
    loadNotOnSale();
  })
);

/* --- recently removed ---------------------------------------------------- */

async function loadRemoved() {
  const windowSecs = $("#removed-window").value;
  const rows = await api("/api/recently-removed" + (windowSecs ? `?window=${encodeURIComponent(windowSecs)}` : ""));
  $("#removed-count").textContent = `${rows.length} listing${rows.length === 1 ? "" : "s"}`;
  $("#removed-body").innerHTML = rows
    .map(
      (r) => `<tr>
        <td>${itemLink(r.item_id, r.name)} ${rarityBadge(r.rarity)}</td>
        <td>${esc(r.item_type)}</td>
        <td>${esc(r.rarity || "—")}</td>
        <td class="num">${fmtPrice(r.item_price)}</td>
        <td><span class="badge ${r.reason === "EXPIRED" ? "expired" : "removed"}">${r.reason}</span></td>
        <td class="num">${fmtTime(r.vanished_at)}</td>
        <td>${esc(String(r.seller_empire_id))}</td>
      </tr>`
    )
    .join("") || '<tr><td colspan="7" class="muted">nothing vanished in this frame</td></tr>';
}

$("#removed-window").addEventListener("change", loadRemoved);

/* --- item detail --------------------------------------------------------- */

const HIST_BINS = [
  [0, "<100"], [100, "100–299"], [300, "300–999"], [1000, "1k–2.9k"], [3000, "3k–9.9k"],
  [10000, "10k–29.9k"], [30000, "30k–99.9k"], [100000, "100k–299k"], [300000, "300k–999k"], [1000000, "1M+"],
];

async function loadItem(itemId) {
  const it = await api("/api/item/" + encodeURIComponent(itemId));
  $("#item-title").textContent = it.name || it.item_id;
  const nav = (it.name || it.item_id);
  $("#nav-item").textContent = nav.length > 24 ? nav.slice(0, 24) + "…" : nav;
  let meta = `${esc(it.item_id)} · ${esc(it.item_type)} · level ${it.item_level} · ${rarityBadge(it.rarity) || "rarity unknown"}`;
  if (it.civilization) meta += ` · ${esc(it.civilization)}`;
  if (it.age != null) meta += ` · age ${it.age}`;
  $("#item-meta").innerHTML = meta;
  $("#item-desc").textContent = it.description || "";
  $("#item-desc").hidden = !it.description;
  const cur = it.current;
  $("#item-count").textContent = fmtInt(cur.length);
  const prices = cur.map((c) => c.unit_price).sort((a, b) => a - b);
  const med = prices.length ? prices[Math.floor(prices.length / 2)] : null;
  $("#item-min").textContent = fmtPrice(prices[0]);
  $("#item-med").textContent = fmtPrice(med);
  $("#item-max").textContent = fmtPrice(prices[prices.length - 1]);

  makeChart("#chart-item-history", {
    type: "line",
    data: {
      datasets: [
        {
          label: "median",
          data: it.series.map((s) => ({ x: s.t * 1000, y: s.median })),
          borderColor: "#fbbf24",
          backgroundColor: "rgba(251,191,36,0.1)",
          fill: true,
          tension: 0.2,
          pointRadius: 0,
        },
        {
          label: "listings",
          data: it.points.map((p) => ({ x: p.t * 1000, y: p.price })),
          backgroundColor: "rgba(56,189,248,0.45)",
          pointRadius: 1.5,
          showLine: false,
        },
      ],
    },
    options: {
      scales: {
        x: {
          type: "linear",
          ticks: { callback: (v) => fmtTime(v / 1000) },
        },
        y: { beginAtZero: true },
      },
      plugins: { legend: { display: false }, tooltip: { callbacks: { title: (items) => fmtTime(items[0].parsed.x / 1000) } } },
    },
  });

  const counts = HIST_BINS.map(() => 0);
  for (const p of it.points) {
    let idx = 0;
    HIST_BINS.forEach(([lo], i) => (p.price >= lo ? (idx = i) : null));
    counts[idx]++;
  }
  makeChart("#chart-item-histogram", {
    type: "bar",
    data: {
      labels: HIST_BINS.map(([, label]) => label),
      datasets: [{ data: counts, backgroundColor: "#38bdf8" }],
    },
    options: { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } },
  });

  $("#item-current").innerHTML = cur
    .map(
      (c) => `<tr>
        <td class="num">${fmtPrice(c.unit_price)}</td>
        <td class="num">${fmtPrice(c.item_price)}</td>
        <td class="num">${c.item_count}</td>
        <td class="num">${fmtDays(c.seconds_till_expiry)}</td>
        <td>${esc(String(c.seller_empire_id))}</td>
      </tr>`
    )
    .join("") || '<tr><td colspan="5" class="muted">not currently listed</td></tr>';
}

$("#item-back").addEventListener("click", () => {
  history.replaceState(null, "", window.location.pathname);
  showTab("listings");
});

/* --- router + boot ------------------------------------------------------- */

function route() {
  const hash = decodeURIComponent(window.location.hash);
  if (hash.startsWith("#item/")) {
    const itemId = hash.slice("#item/".length);
    $("#nav-item").hidden = false;
    $("#nav-item").textContent = itemId.length > 24 ? itemId.slice(0, 24) + "…" : itemId;
    showTab("item");
    loadItem(itemId).catch((e) => {
      $("#item-title").textContent = "error";
      $("#item-meta").textContent = e.message;
    });
  } else {
    $("#nav-item").hidden = true;
    showTab("overview");
  }
}
window.addEventListener("hashchange", route);

async function boot() {
  const data = loadOverview().catch((e) => console.error(e));
  const listings = loadListings().catch((e) => console.error(e));
  await Promise.all([data, listings]);
  showTab("overview");
  await Promise.all([
    loadBestSellers().catch((e) => console.error(e)),
    loadBestSellersChart().catch((e) => console.error(e)),
    loadBestValue().catch((e) => console.error(e)),
    loadBestValueChart().catch((e) => console.error(e)),
    loadNotOnSale().catch((e) => console.error(e)),
    loadRemoved().catch((e) => console.error(e)),
  ]);
  route();
}
boot();
