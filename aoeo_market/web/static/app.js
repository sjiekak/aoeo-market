/* Merchant Zeno dashboard — single-page app backed by the /api JSON endpoints. */

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

// chartjs-plugin-zoom registers itself when its script loads; the guard keeps
// the dashboard working (without zoom) if that CDN file is unreachable.
if (window.ChartZoom) Chart.register(ChartZoom);

// The snapshot data only changes when the server ingests a new snapshot, so a
// response is immutable for the life of the page: responses are memoized per
// URL and a repeated request (re-opening a tab, asking the same search twice,
// re-sorting a view whose fetch is already in flight) is served from memory.
const apiCache = new Map();

async function api(path) {
	if (apiCache.has(path)) return apiCache.get(path);
	const r = await fetch(path);
	const body = await r.json().catch(() => ({}));
	if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
	apiCache.set(path, body);
	return body;
}

function fmtPrice(n) {
	if (n == null) return "—";
	if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
	if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
	return String(n);
}
const fmtInt = (n) => (n == null ? "—" : n.toLocaleString("en-US"));
// Every instant is rendered in the browser's local timezone: UTC is the
// representation the API speaks, not what the dashboard displays. The en-GB
// locale pins the day-first order (DD/MM/YYYY) and a 24-hour clock, instead of
// the visitor's locale (en-US defaults to month-first and a 12-hour clock).
const fmtLocal = (date) =>
	date.toLocaleString("en-GB", {
		dateStyle: "short",
		timeStyle: "short",
	});
const fmtTime = (t) => (t ? fmtLocal(new Date(t * 1000)) : "—");
const fmtInstant = (iso) => (iso ? fmtLocal(new Date(iso)) : "—");
// The stored absolute expiry is an ISO-8601 UTC instant; the cell shows how
// long is left as of *now* (past instants read "expired") and its title is the
// local-time rendering. Callers pass one `now` for a whole render pass so the
// clock is read once per table, not once per row. Rows recorded before the
// column existed are null.
const fmtExpiry = (iso, now) => {
	if (!iso) return "—";
	const days = (Date.parse(iso) - now) / 86400000;
	return days >= 0 ? days.toFixed(1) + "d" : "expired";
};
const fmtDur = (s) => {
	if (s == null) return "—";
	const h = s / 3600;
	return h < 48 ? h.toFixed(1) + " h" : (h / 24).toFixed(1) + " d";
};
// A bin edge arrives as a raw float. fmtPrice only rounds the k/M magnitudes,
// so round to a readable precision first: otherwise a bin reads "0.84–1.378553".
const fmtBinEdge = (v) => fmtPrice(Number(v.toPrecision(3)));
const fmtBin = (b) =>
	b.bin_start === b.bin_end
		? fmtBinEdge(b.bin_start)
		: `${fmtBinEdge(b.bin_start)}–${fmtBinEdge(b.bin_end)}`;
const esc = (s) =>
	String(s).replace(
		/[&<>"']/g,
		(c) =>
			({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
				c
			],
	);

// Wire item ids are case-insensitive (listings keep the server's spelling while
// the catalog and the Dismantler map key everything lowercased), and the API
// resolves either case — so every generated path is lowercase and the same item
// always has one URL.
const itemHref = (itemId) => `/item/${encodeURIComponent(String(itemId).toLowerCase())}`;

function itemLink(itemId, name) {
	const label = name || itemId;
	const title = name ? ` title="${esc(itemId)}"` : "";
	return `<a href="${itemHref(itemId)}" class="item-link"${title}>${esc(label)}</a>`;
}

function rarityBadge(name) {
	if (!name) return "";
	const color = RARITY_COLORS[name] || RARITY_COLORS.unknown;
	return `<span class="badge" style="color:${color};border-color:${color}">${esc(name)}</span>`;
}

// A design produces this item, so it can be crafted. Rendered as a badge that
// sits right beside the rarity tag (see rarityTag); nothing when not craftable.
function craftableBadge(craftable) {
	if (!craftable) return "";
	return '<span class="badge craftable" title="Craftable — a design produces this item">craftable</span>';
}

// The rarity tag as it appears next to an item name: the rarity badge followed
// by the craftable badge, each omitted when the row has no such value.
function rarityTag(row) {
	return [rarityBadge(row.rarity), craftableBadge(row.craftable)]
		.filter(Boolean)
		.join(" ");
}

function makeChart(canvasId, config) {
	if (charts[canvasId]) charts[canvasId].destroy();
	charts[canvasId] = new Chart($(canvasId), config);
}

/* --- responsive tables ----------------------------------------------------
   On a phone the CSS stacks each row into a card of label/value lines (see
   the ≤640px block in style.css).  That needs a little help from here:

     * every cell gets the text of its column header as `data-label`, the card
       line's caption — re-applied after each render, because the rows are
       rebuilt from scratch;
     * a sortable table is marked `sortable`, so the stylesheet keeps its
       header buttons (the only way to sort on a phone) above the cards;
     * every table gets an empty twin scroll bar *above* it, mirroring the one
       below, so a long table can be panned without first scrolling to its end.
*/
function labelRows(table) {
	const headers = [...table.querySelectorAll("thead th")].map((th) =>
		th.textContent.trim(),
	);
	for (const row of table.querySelectorAll("tbody tr")) {
		[...row.children].forEach((cell, i) => {
			// an empty-state row spans the table: it is a note, not a card
			if (headers[i] && !cell.hasAttribute("colspan")) {
				cell.dataset.label = headers[i];
			}
		});
	}
}

function addTopScrollBar(wrap, table) {
	const bar = document.createElement("div");
	const spacer = document.createElement("div");
	bar.className = "table-top";
	spacer.className = "table-top-spacer";
	bar.appendChild(spacer);
	wrap.before(bar);

	// The spacer carries the table's own width and the bar shares the wrapper's
	// padding, so both elements have the same scroll range and stay in step.
	const fit = () => {
		spacer.style.width = `${table.offsetWidth}px`;
		bar.hidden = table.offsetWidth <= wrap.clientWidth;
	};
	bar.addEventListener("scroll", () => {
		wrap.scrollLeft = bar.scrollLeft;
	});
	wrap.addEventListener("scroll", () => {
		bar.scrollLeft = wrap.scrollLeft;
	});
	// A render can change the table's width without resizing the wrapper, and a
	// tab switch or a window resize changes both: watch the table, and keep the
	// window listener as the cheap safety net for engines without ResizeObserver.
	if (window.ResizeObserver) new ResizeObserver(fit).observe(table);
	window.addEventListener("resize", fit);
	fit();
}

function prepareTables() {
	document.querySelectorAll(".table-wrap").forEach((wrap) => {
		const table = wrap.querySelector("table");
		if (!table) return;
		if (table.querySelector("thead th button")) {
			table.classList.add("sortable");
		}
		const body = table.querySelector("tbody");
		if (body) {
			new MutationObserver(() => labelRows(table)).observe(body, {
				childList: true,
			});
		}
		labelRows(table);
		addTopScrollBar(wrap, table);
	});
}

// The time-series charts (market supply, item price history) share one x axis:
// epoch-milliseconds on a linear scale, tick labels in local time, and the tick
// count left to Chart.js so both render the same axis. A fresh object per chart
// keeps Chart.js from mutating a config shared between two instances.
const timeAxis = () => ({
	type: "linear",
	ticks: { callback: (v) => fmtTime(v / 1000) },
});

/* --- column sorting (shared by every sortable tab) ----------------------- */

// Every sortable column cycles through three states when its header is
// clicked: unsorted → min first (↑) → max first (↓) → unsorted again.
// Clicking a different column starts it sorted min-first. The arrows are
// rendered in CSS from the .sort-min/.sort-max classes, and the <th> carries
// an aria-sort attribute for assistive tech.
function wireColumnSort(tabId, { apply, order = null, dir = null }) {
	const state = { order, dir };
	const buttons = () =>
		document.querySelectorAll(`#tab-${tabId} th button[data-order]`);
	function sync() {
		buttons().forEach((btn) => {
			const th = btn.closest("th");
			const active = btn.dataset.order === state.order;
			btn.classList.toggle("active", active);
			btn.classList.remove("sort-min", "sort-max");
			if (active) {
				th.setAttribute(
					"aria-sort",
					state.dir === "desc" ? "descending" : "ascending",
				);
				btn.classList.add(state.dir === "desc" ? "sort-max" : "sort-min");
			} else {
				th.removeAttribute("aria-sort");
			}
		});
	}
	buttons().forEach((btn) => {
		btn.addEventListener("click", () => {
			const col = btn.dataset.order;
			if (state.order !== col) {
				state.order = col;
				state.dir = "asc"; // a new column starts sorted min-first
			} else if (state.dir === "asc") {
				state.dir = "desc";
			} else {
				state.order = null;
				state.dir = null;
			}
			sync();
			apply(state);
		});
	});
	sync();
	// Exposed so a view selector can drive the sort direction and still leave
	// the header arrows correct.
	return Object.assign(state, { sync });
}

function orderQuery(order, dir) {
	return order
		? `?order=${encodeURIComponent(order)}&dir=${encodeURIComponent(dir)}`
		: "";
}

/* --- tabs ---------------------------------------------------------------- */

// Each tab fetches its data the first time it is shown, so a page that only
// needs one view — the landing search, or an item page — never pays for the
// others. The loaders are function declarations, so referring to them here is
// safe even though they are defined further down.  "search" is deliberately
// absent: it is the landing view and fetches nothing until its form is
// submitted.
const TAB_LOADERS = {
	overview: loadOverview,
	listings: loadListings,
	"best-sellers": loadBestSellersTab,
	"best-value": loadBestValueTab,
	"not-on-sale": loadNotOnSale,
	removed: loadRemoved,
};
const loadedTabs = new Set();

function loadTab(name) {
	const load = TAB_LOADERS[name];
	if (!load || loadedTabs.has(name)) return;
	loadedTabs.add(name);
	load().catch((e) => console.error(e));
}

function showTab(name) {
	document.querySelectorAll("main > section").forEach((s) => {
		s.hidden = s.id !== `tab-${name}`;
	});
	document.querySelectorAll("nav button").forEach((b) => {
		b.classList.toggle("active", b.dataset.tab === name);
	});
	loadTab(name);
}

document.querySelectorAll("nav button").forEach((b) => {
	b.addEventListener("click", () => {
		if (b.dataset.tab === "item") return;
		// An item lives on its own page, so a tab click leaves it for the
		// dashboard URL (without a reload) before switching the view.
		if (itemIdFromPath() !== null) {
			history.pushState(null, "", "/");
			$("#nav-item").hidden = true;
		}
		showTab(b.dataset.tab);
	});
});

/* --- search (the landing view) ------------------------------------------- */

// The only view that is useful empty: it runs entirely on submit, so opening
// the dashboard costs no API traffic at all.
const renderSearch = (rows) =>
	rows
		.map(
			(r) => `<tr>
        <td>${itemName(r)}</td>
        <td>${esc(r.type || r.kind || "—")}</td>
        <td>${rarityTag(r)}</td>
        <td class="num">${
					r.listed_now
						? fmtPrice(r.current_median_unit_price)
						: `<span class="muted" title="historical median">${fmtPrice(r.median_unit_price)}</span>`
				}</td>
        <td class="num">${r.listed_now ? fmtInt(r.active_count) : '<span class="muted">not listed</span>'}</td>
      </tr>`,
		)
		.join("");

async function runSearch(query) {
	const q = query.trim();
	const summary = $("#search-summary");
	if (!q) {
		$("#search-table").hidden = true;
		$("#search-body").innerHTML = "";
		summary.textContent = "";
		return;
	}
	summary.textContent = `searching for “${q}”…`;
	const rows = await api("/api/search?q=" + encodeURIComponent(q));
	summary.textContent = rows.length
		? `${rows.length} match${rows.length === 1 ? "" : "es"} for “${q}”`
		: `no item id or name matches “${q}”`;
	$("#search-table").hidden = rows.length === 0;
	$("#search-body").innerHTML = renderSearch(rows);
}

document.querySelector("#search-form").addEventListener("submit", (e) => {
	e.preventDefault();
	runSearch($("#search-q").value).catch((err) => {
		$("#search-summary").textContent = err.message;
	});
});

/* --- overview ------------------------------------------------------------ */

// While the supply chart's time axis is zoomed or panned, rescale the y axis
// to the points visible in the current window (with ~10% headroom) so small
// variations don't flatten against the full-history range. On reset the
// default 0-based auto scale returns.
function fitSupplyY(chart) {
	const y = chart.scales.y;
	const { min: x0, max: x1 } = chart.scales.x;
	if (!chart.isZoomedOrPanned() || x0 == null || x1 == null) {
		if (y.options.min !== undefined || y.options.max !== undefined) {
			delete y.options.min;
			delete y.options.max;
			chart.update("none");
		}
		return;
	}
	let lo = Infinity;
	let hi = -Infinity;
	for (const ds of chart.data.datasets) {
		for (const p of ds.data) {
			if (p.x >= x0 && p.x <= x1) {
				if (p.y < lo) lo = p.y;
				if (p.y > hi) hi = p.y;
			}
		}
	}
	if (!Number.isFinite(lo)) return; // window with no points — leave as is
	const pad = Math.max((hi - lo) * 0.1, hi * 0.02, 1);
	y.options.min = Math.max(0, lo - pad);
	y.options.max = hi + pad;
	chart.update("none");
}

async function loadOverview() {
	// The fastest-sellers card belongs to this tab, so its rows are fetched
	// here; the Best sellers tab reads the same memoized response.
	const [o, sellers] = await Promise.all([api("/api/overview"), api(BEST_SELLERS_URL)]);
	$("#empty-banner").hidden = o.latest !== null;
	$("#kpi-listings").textContent = fmtInt(o.active_listings);
	$("#kpi-items").textContent = fmtInt(o.distinct_items);
	$("#kpi-snapshots").textContent = fmtInt(o.snapshot_count);
	$("#kpi-last").textContent = o.latest ? fmtTime(o.latest.captured_at) : "—";
	$("#snapshot-info").textContent = o.latest
		? `snapshot ${fmtTime(o.latest.captured_at)} · ${fmtInt(o.active_listings)} listings`
		: "no data yet";

	// X axis is epoch-milliseconds on a linear scale so the time axis can be
	// panned (drag) and zoomed (scroll wheel / pinch). The zoom plugin clamps
	// the window to the data range, so you can never zoom out past the edges.
	makeChart("#chart-supply", {
		type: "line",
		data: {
			datasets: [
				{
					label: "active listings",
					data: o.supply_history.map((s) => ({ x: s.t * 1000, y: s.count })),
					borderColor: "#fbbf24",
					backgroundColor: "rgba(251,191,36,0.08)",
					fill: true,
					tension: 0.25,
					pointRadius: 0,
				},
			],
		},
		options: {
			scales: {
				x: timeAxis(),
				y: { beginAtZero: true },
			},
			plugins: {
				legend: { display: false },
				tooltip: {
					callbacks: { title: (items) => fmtTime(items[0].parsed.x / 1000) },
				},
				zoom: {
					pan: {
						enabled: true,
						mode: "x",
						onPanComplete: ({ chart }) => fitSupplyY(chart),
					},
					zoom: {
						wheel: { enabled: true },
						pinch: { enabled: true },
						mode: "x",
						onZoom: ({ chart }) => fitSupplyY(chart),
						onZoomComplete: ({ chart }) => {
							$("#supply-reset").hidden = !chart.isZoomedOrPanned();
							fitSupplyY(chart);
						},
					},
					limits: { x: { min: "original", max: "original" } },
				},
			},
		},
	});
	$("#supply-reset").addEventListener("click", () =>
		charts["#chart-supply"].resetZoom(),
	);

	makeChart("#chart-prices", {
		type: "bar",
		data: {
			labels: o.price_distribution.map((b) => b.label),
			datasets: [
				{
					label: "listings",
					data: o.price_distribution.map((b) => b.count),
					backgroundColor: "#38bdf8",
				},
			],
		},
		options: {
			plugins: { legend: { display: false } },
			scales: { y: { beginAtZero: true } },
		},
	});

	makeChart("#chart-types", {
		type: "doughnut",
		data: {
			labels: o.type_breakdown.map((t) => t.name),
			datasets: [
				{
					data: o.type_breakdown.map((t) => t.count),
					backgroundColor: [
						"#fbbf24",
						"#38bdf8",
						"#a78bfa",
						"#4ade80",
						"#f472b6",
					],
				},
			],
		},
	});

	makeChart("#chart-rarity", {
		type: "bar",
		data: {
			labels: o.rarity_breakdown.map((r) => r.name),
			datasets: [
				{
					data: o.rarity_breakdown.map((r) => r.count),
					backgroundColor: o.rarity_breakdown.map(
						(r) => RARITY_COLORS[r.name] || RARITY_COLORS.unknown,
					),
				},
			],
		},
		options: {
			plugins: { legend: { display: false } },
			scales: { y: { beginAtZero: true } },
		},
	});

	renderBestSellersChart(sellers);

	$("#movers").innerHTML =
		o.top_movers
			.map(
				(m) => `<tr>
        <td>${itemName(m)} ${rarityTag(m)}</td>
        <td class="num">${fmtPrice(m.median_before)}</td>
        <td class="num">${fmtPrice(m.median_now)}</td>
        <td class="num ${m.change_pct >= 0 ? "up" : "down"}">${m.change_pct >= 0 ? "+" : ""}${m.change_pct}%</td>
      </tr>`,
			)
			.join("") ||
		'<tr><td colspan="4" class="muted">need at least two data points</td></tr>';
}

/* --- listings ------------------------------------------------------------ */

let listingsCache = [];

// One accessor per sortable column. Strings compare lexically and numbers
// numerically; null/undefined sort last in "min" order (first in "max"),
// matching the API's null placement. Seller is deliberately absent: the API
// redacts the id, so there is nothing to show or sort by.
const LISTING_SORTS = {
	item: (l) => l.item_id,
	type: (l) => l.item_type,
	level: (l) => l.item_level,
	count: (l) => l.item_count,
	price: (l) => l.unit_price,
	expiry: (l) => l.expires_at,
};

function cmpValues(a, b) {
	if (a == null && b == null) return 0;
	if (a == null) return 1;
	if (b == null) return -1;
	if (typeof a === "number" && typeof b === "number") return a - b;
	// Code-point order, matching the API's ORDER BY: localeCompare would rank
	// "Halloween" before "HDW" where the server ranks it after, so a header
	// click would disagree with the same ordering from the API.
	const sa = String(a);
	const sb = String(b);
	if (sa === sb) return 0;
	return sa < sb ? -1 : 1;
}

// Sort a copy of *rows* by the accessor for *order*, leaving the cache alone.
// The accessor runs once per row instead of once per comparison, and the
// comparator is stable, so ties keep the order the API returned — the same
// result the server would produce for the same ordering.
function sortedRows(rows, order, dir, accessors) {
	if (!order) return rows.slice();
	const key = accessors[order];
	const sign = dir === "desc" ? -1 : 1;
	return rows
		.map((row) => [key(row), row])
		.sort((a, b) => cmpValues(a[0], b[0]) * sign)
		.map((pair) => pair[1]);
}

// Collapse a burst of events — typing in a filter box — into one render.
function debounce(fn, ms) {
	let timer;
	return (...args) => {
		clearTimeout(timer);
		timer = setTimeout(() => fn(...args), ms);
	};
}

// The classic default view: cheapest per unit last (price, max-first).
const listingSort = wireColumnSort("listings", {
	order: "price",
	dir: "desc",
	apply: renderListings,
});

async function loadListings() {
	listingsCache = await api("/api/listings");
	const types = [...new Set(listingsCache.map((l) => l.item_type))].sort();
	const sel = $("#ls-type");
	sel.innerHTML =
		'<option value="">all types</option>' +
		types.map((t) => `<option>${esc(t)}</option>`).join("");
	renderListings();
}

function renderListings() {
	const { order, dir } = listingSort;
	const q = $("#ls-q").value.trim().toLowerCase();
	const type = $("#ls-type").value;
	const rows = sortedRows(
		listingsCache.filter(
			(l) =>
				(!type || l.item_type === type) &&
				(!q ||
					l.item_id.toLowerCase().includes(q) ||
					(l.name && l.name.toLowerCase().includes(q))),
		),
		order,
		dir,
		LISTING_SORTS,
	);
	$("#ls-count").textContent =
		`${rows.length} / ${listingsCache.length} listings`;
	const now = Date.now();
	$("#listings-body").innerHTML = rows
		.map(
			(l) => `<tr>
        <td>${itemName(l)} ${rarityTag(l)}</td>
        <td>${esc(l.item_type)}</td>
        <td class="num">${l.item_level}</td>
        <td class="num">${l.item_count}</td>
        <td class="num">${fmtPrice(l.unit_price)}${l.item_count > 1 ? ` <span class="muted">(×${l.item_count})</span>` : ""}</td>
        <td class="num" title="${esc(l.expires_at ? fmtInstant(l.expires_at) : "no absolute expiry stored")}">${fmtExpiry(l.expires_at, now)}</td>
      </tr>`,
		)
		.join("");
}

// Typing filters the cached rows, so it is debounced: without it every
// keystroke rebuilt the whole table of ~1,800 rows.
$("#ls-q").addEventListener("input", debounce(renderListings, 120));
$("#ls-type").addEventListener("change", renderListings);

/* --- best sellers -------------------------------------------------------- */

// One accessor per sortable column, mirroring the ordering the API applies
// (rarity by rank, so Legendary sorts above Epic rather than alphabetically).
// "sales" is the row's ``sales`` field, exactly as the API sorts it — note the
// cell shows ``timed_sales``, a pre-existing mismatch left as it was.
const BEST_SELLER_SORTS = {
	item: (r) => r.item_id,
	type: (r) => r.item_type,
	level: (r) => r.item_level,
	rarity: (r) => r.rarity_rank,
	median_time: (r) => r.median_time,
	min_time: (r) => r.min_time,
	max_time: (r) => r.max_time,
	sales: (r) => r.sales,
	expired: (r) => r.expired,
	active_count: (r) => r.active_count,
	current_median_unit_price: (r) => r.current_median_unit_price,
	last_seen: (r) => r.last_seen,
};

let bestSellerCache = [];

// The panel — not the API — sets the floor: too few observed sales and a
// median time-to-sale says more about the sample than about the item. Both
// this tab's table and the Overview tab's chart read this one response.
const BEST_SELLERS_URL = "/api/best-sellers?min_sales=5";

// The Overview tab's fastest-sellers card: the ten fastest, whatever the table
// is sorted by. The rows come from the Overview loader (the same memoized
// response the table below uses).
function renderBestSellersChart(rows) {
	const top = sortedRows(rows, "median_time", "asc", BEST_SELLER_SORTS).slice(0, 10);
	makeChart("#chart-best-sellers", {
		type: "bar",
		data: {
			labels: top.map((r) => {
				const label = r.name || r.item_id;
				return label.length > 26 ? label.slice(0, 26) + "…" : label;
			}),
			datasets: [
				{
					label: "median time-to-sale",
					data: top.map((r) => r.median_time / 3600),
					backgroundColor: "#4ade80",
				},
			],
		},
		options: {
			indexAxis: "y",
			plugins: {
				legend: { display: false },
				tooltip: { callbacks: { label: (i) => fmtDur(i.parsed.x * 3600) } },
			},
			scales: {
				x: { title: { display: true, text: "hours" }, beginAtZero: true },
			},
		},
	});
}

function renderBestSellers() {
	const rows = sortedRows(
		bestSellerCache,
		bestSort.order,
		bestSort.dir,
		BEST_SELLER_SORTS,
	);
	$("#best-body").innerHTML =
		rows
			.map(
				(r) => `<tr>
        <td>${itemName(r)} ${rarityTag(r)}</td>
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
      </tr>`,
			)
			.join("") ||
		'<tr><td colspan="11" class="muted">no fully observed sales yet — this view fills in as more data is collected</td></tr>';
}

// A column click re-sorts the cached rows — the API used to be re-queried for
// every header click. The response is memoized per URL, so an already-loaded
// Overview tab costs no second request.
async function loadBestSellersTab() {
	bestSellerCache = await api(BEST_SELLERS_URL);
	renderBestSellers();
}

const bestSort = wireColumnSort("best-sellers", {
	order: "median_time",
	dir: "asc",
	apply: renderBestSellers,
});

/* --- best value (crafting) ----------------------------------------------- */

// value_ratio = unit price / crafting cost, so 2× means the item sells for
// twice what its ingredients cost to buy.
const fmtRatio = (r) =>
	r == null ? "—" : (r >= 10 ? r.toFixed(0) : r.toFixed(1)) + "×";

// One accessor per sortable column, mirroring the ordering the API applies.
const VALUE_SORTS = {
	item: (r) => r.item_id,
	type: (r) => r.type,
	rarity: (r) => r.rarity_rank,
	craft_cost: (r) => r.craft_cost,
	price: (r) => r.price,
	value_ratio: (r) => r.value_ratio,
	listed_now: (r) => r.listed_now,
};

let valueCache = [];

function renderBestValue() {
	const rows = sortedRows(
		valueCache,
		valueSort.order,
		valueSort.dir,
		VALUE_SORTS,
	);
	$("#value-body").innerHTML =
		rows
			.map(
				(r) => `<tr>
        <td>${itemName(r)}</td>
        <td>${esc(r.type || "—")}</td>
        <td>${rarityTag(r)}</td>
        <td class="num">${fmtPrice(r.craft_cost)}</td>
        <td class="num" title="${esc(r.listed_now ? "current median" : "historical median")}">${fmtPrice(r.price)}</td>
        <td class="num"><b>${fmtRatio(r.value_ratio)}</b></td>
        <td class="num">${r.listed_now ? fmtInt(r.active_count) : '<span class="muted">not listed</span>'}</td>
      </tr>`,
			)
			.join("") ||
		'<tr><td colspan="7" class="muted">no craftable item has been observed yet</td></tr>';
}

// One request feeds the whole tab; best/worst and every column header re-sort
// the cached rows instead of re-querying the API.
async function loadBestValueTab() {
	valueCache = await api("/api/best-value");
	renderBestValue();
}

const valueSort = wireColumnSort("best-value", {
	order: "value_ratio",
	dir: "desc",
	apply: renderBestValue,
});

// Best/worst is only the direction of the ratio ordering; a column header
// click takes over from there.
$("#value-view").addEventListener("change", () => {
	valueSort.order = "value_ratio";
	valueSort.dir = $("#value-view").value;
	valueSort.sync();
	renderBestValue();
});

/* --- not on sale --------------------------------------------------------- */

async function loadNotOnSale() {
	const rows = await api(
		"/api/not-on-sale" + orderQuery(nosSort.order, nosSort.dir),
	);
	$("#nos-body").innerHTML =
		rows
			.map(
				(r) => `<tr>
        <td>${itemName(r)} ${rarityTag(r)}</td>
        <td>${esc(r.item_type)}</td>
        <td class="num">${r.item_level}</td>
        <td>${esc(r.rarity || "—")}</td>
        <td class="num">${fmtPrice(r.median_unit_price)}</td>
        <td class="num">${fmtPrice(r.min_unit_price)}</td>
        <td class="num">${fmtPrice(r.max_unit_price)}</td>
        <td class="num">${fmtInt(r.times_listed)}</td>
        <td class="num">${fmtTime(r.last_seen)}</td>
      </tr>`,
			)
			.join("") ||
		'<tr><td colspan="9" class="muted">nothing here — every known item is currently listed</td></tr>';
}

const nosSort = wireColumnSort("not-on-sale", {
	order: "median_unit_price",
	dir: "desc",
	apply: loadNotOnSale,
});

/* --- recently removed ---------------------------------------------------- */

async function loadRemoved() {
	const windowSecs = $("#removed-window").value;
	const rows = await api(
		"/api/recently-removed" +
			(windowSecs ? `?window=${encodeURIComponent(windowSecs)}` : ""),
	);
	$("#removed-count").textContent =
		`${rows.length} listing${rows.length === 1 ? "" : "s"}`;
	$("#removed-body").innerHTML =
		rows
			.map(
				(r) => `<tr>
        <td>${itemName(r)} ${rarityTag(r)}</td>
        <td>${esc(r.item_type)}</td>
        <td>${esc(r.rarity || "—")}</td>
        <td class="num">${fmtPrice(r.item_price)}</td>
        <td><span class="badge ${r.reason === "EXPIRED" ? "expired" : "removed"}">${r.reason}</span></td>
        <td class="num">${fmtTime(r.vanished_at)}</td>
      </tr>`,
			)
			.join("") ||
		'<tr><td colspan="6" class="muted">nothing vanished in this frame</td></tr>';
}

$("#removed-window").addEventListener("change", loadRemoved);

/* --- item detail --------------------------------------------------------- */

// Sprite clipping lives entirely in the browser, mirroring celeste-search:
// the .webp sheet is a background-image and a background-position shows one
// 64px cell. sprites.json maps kind -> icon -> position, and "@sheets" carries
// each sheet's column/row count so the cell can be clipped at any pixel size
// (background-size: cols*100% rows*100%).
const SPRITE_SHEETS = {
	advisor: "advisors",
	blueprint: "blueprints",
	consumable: "consumables",
	design: "designs",
	item: "items",
	material: "materials",
};
let spritesIndex = null;
let spritesPromise = null;
function loadSprites() {
	if (!spritesPromise) {
		spritesPromise = fetch("/static/sprites.json")
			.then((r) => (r.ok ? r.json() : {}))
			.then((idx) => (spritesIndex = idx || {}))
			.catch(() => (spritesIndex = {}));
	}
	return spritesPromise;
}

function spriteIconStyle(kind, icon) {
	const sheet = SPRITE_SHEETS[kind];
	if (!sheet || !icon || !spritesIndex) return null;
	const pos = (spritesIndex[kind] || {})[icon];
	const meta = (spritesIndex["@sheets"] || {})[kind];
	if (pos == null || !meta) return null;
	return {
		url: `/static/sprites/${sheet}.webp`,
		pos,
		bgSize: `${meta.cols * 100}% ${meta.rows * 100}%`,
	};
}

function itemIconHtml(kind, icon) {
	const s = spriteIconStyle(kind, icon);
	if (!s) return "";
	return `<span class="item-icon-inline" style="background-image:url('${s.url}');background-position:${s.pos};background-size:${s.bgSize}" aria-hidden="true"></span>`;
}

function itemName(row) {
	return itemIconHtml(row.kind, row.icon) + itemLink(row.item_id, row.name);
}

// A material's sprite icon as a real link to its item page, with the name as
// the tooltip; the name next to it links there too.
function materialIconLink(row) {
	const name = row.name || row.item_id;
	const icon = itemIconHtml(row.kind, row.icon);
	if (!icon) return "";
	return `<a href="${itemHref(row.item_id)}" class="material-icon" title="${esc(name)}">${icon}</a>`;
}

function renderRecipe(recipe) {
	const card = $("#item-recipe-card");
	if (!recipe) {
		card.hidden = true;
		return;
	}
	card.hidden = false;
	$("#item-recipe-meta").textContent = [recipe.school, recipe.level != null ? `level ${recipe.level}` : null]
		.filter(Boolean)
		.join(" · ");
	$("#item-recipe-materials").innerHTML = recipe.materials
		.map((m) => {
			const line = m.unit_price != null && m.quantity != null ? m.unit_price * m.quantity : null;
			const price = m.unit_price != null ? `${fmtPrice(m.unit_price)}/unit` : "no price";
			return `<li class="material">
        ${materialIconLink(m)}
        <span class="material-name">${itemLink(m.item_id, m.name)}</span>
        <span class="material-qty">×${fmtInt(m.quantity)}</span>
        <span class="material-price">${price}${line != null ? ` · ${fmtPrice(line)}` : ""}</span>
      </li>`;
		})
		.join("");
	$("#item-recipe-cost").innerHTML =
		`Estimated craft cost <b>${recipe.cost != null ? fmtPrice(recipe.cost) : "—"}</b> ` +
		`<span class="muted">(${recipe.materials_priced}/${recipe.materials.length} priced)</span>`;
}

function renderDismantle(dismantle) {
	const card = $("#item-dismantle-card");
	if (!dismantle) {
		card.hidden = true;
		return;
	}
	card.hidden = false;
	$("#item-dismantle-meta").textContent = [dismantle.type, dismantle.rarity, dismantle.school].filter(Boolean).join(" · ");
	$("#item-dismantle-materials").innerHTML = dismantle.materials
		.map((m) => `<li class="material">${materialIconLink(m)}<span class="material-name">${itemLink(m.item_id, m.name)}</span></li>`)
		.join("");
}

function renderItemImage(it) {
	const img = $("#item-image");
	const s = spriteIconStyle(it.kind, it.icon);
	if (s) {
		img.style.backgroundImage = `url("${s.url}")`;
		img.style.backgroundPosition = s.pos;
		img.style.backgroundSize = s.bgSize;
		img.hidden = false;
	} else {
		img.hidden = true;
	}
}

async function loadItem(itemId) {
	const it = await api("/api/item/" + encodeURIComponent(itemId));
	// Each item is its own page, so it gets its own document title.
	document.title = `${it.name || it.item_id} — Merchant Zeno`;
	$("#item-title").textContent = it.name || it.item_id;
	const nav = it.name || it.item_id;
	$("#nav-item").textContent = nav.length > 24 ? nav.slice(0, 24) + "…" : nav;
	let meta = `${esc(it.item_id)} · ${esc(it.item_type)} · level ${it.item_level} · ${rarityBadge(it.rarity) || "rarity unknown"}`;
	if (it.craftable) meta += ` ${craftableBadge(it.craftable)}`;
	if (it.civilization) meta += ` · ${esc(it.civilization)}`;
	if (it.age != null) meta += ` · age ${it.age}`;
	$("#item-meta").innerHTML = meta;
	$("#item-desc").textContent = it.description || "";
	$("#item-desc").hidden = !it.description;
	await loadSprites();
	renderItemImage(it);
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
				x: timeAxis(),
				y: { beginAtZero: true },
			},
			plugins: {
				legend: { display: false },
				tooltip: {
					callbacks: { title: (items) => fmtTime(items[0].parsed.x / 1000) },
				},
			},
		},
	});

	const hist = it.histogram || [];
	makeChart("#chart-item-histogram", {
		type: "bar",
		data: {
			labels: hist.map(fmtBin),
			datasets: [
				{ data: hist.map((b) => b.count), backgroundColor: "#38bdf8" },
			],
		},
		options: {
			plugins: { legend: { display: false } },
			scales: { y: { beginAtZero: true } },
		},
	});

	const now = Date.now();
	$("#item-current").innerHTML =
		cur
			.map(
				(c) => `<tr>
        <td class="num">${fmtPrice(c.unit_price)}</td>
        <td class="num">${fmtPrice(c.item_price)}</td>
        <td class="num">${c.item_count}</td>
        <td class="num" title="${esc(c.expires_at ? fmtInstant(c.expires_at) : "no absolute expiry stored")}">${fmtExpiry(c.expires_at, now)}</td>
      </tr>`,
			)
			.join("") ||
		'<tr><td colspan="4" class="muted">not currently listed</td></tr>';

	$("#item-previous").innerHTML =
		(it.previous || [])
			.map(
				(p) => `<tr>
        <td class="num">${fmtPrice(p.unit_price)}</td>
        <td class="num">${fmtPrice(p.item_price)}</td>
        <td class="num">${p.item_count}</td>
        <td>${fmtTime(p.first_seen)}</td>
        <td class="num">${fmtTime(p.vanished_at)}</td>
        <td><span class="badge ${p.reason === "EXPIRED" ? "expired" : "removed"}">${p.reason}</span></td>
      </tr>`,
			)
			.join("") ||
		'<tr><td colspan="6" class="muted">no previous listings recorded</td></tr>';

	renderRecipe(it.recipe);
	renderDismantle(it.dismantle);
	$("#item-craft-row").hidden = !it.recipe && !it.dismantle;
}

$("#item-back").addEventListener("click", () => {
	// Item links are real navigations, so the dashboard is normally one history
	// step back; a directly opened (shared) page falls back to the dashboard.
	if (document.referrer.startsWith(`${window.location.origin}/`)) window.history.back();
	else window.location.href = "/";
});

/* --- router + boot ------------------------------------------------------- */

// Every item has its own page at /item/<item_id>: the server answers that path
// with this shell and the router opens the item view from the URL, so item pages
// are shareable and the browser's own back/forward buttons work.
const ITEM_PREFIX = "/item/";

function itemIdFromPath() {
	const path = window.location.pathname;
	if (!path.startsWith(ITEM_PREFIX)) return null;
	try {
		return decodeURIComponent(path.slice(ITEM_PREFIX.length)) || null;
	} catch {
		return null; // malformed percent-escape: not an item page
	}
}

function route() {
	const itemId = itemIdFromPath();
	if (itemId === null) {
		$("#nav-item").hidden = true;
		showTab("search");
		return;
	}
	$("#nav-item").hidden = false;
	$("#nav-item").textContent =
		itemId.length > 24 ? itemId.slice(0, 24) + "…" : itemId;
	showTab("item");
	loadItem(itemId).catch((e) => {
		document.title = "Item not found — Merchant Zeno";
		$("#item-title").textContent = "not found";
		$("#item-meta").textContent = e.message;
	});
}

async function boot() {
	await loadSprites(); // icon positions are needed by the item icon and every table
	// The tables scroll and stack per row, so they are wired before the first
	// render (prepareTables observes each body and labels whatever it holds).
	prepareTables();
	// route() opens the one view the URL asks for, and showTab() fetches only
	// that tab's data — so an item page never requests the dashboard's data.
	route();
}
boot();
