// Energy Tariff Assistant plan browser. Plain JavaScript, no build step.
// Data: data/library.json (index entries + validation, written by scripts/build_site.py),
// the plan files themselves, and data/coverage.json (from plans/retailers.json).
"use strict";

const REPO = "https://github.com/Roving-Ronin/energy-tariff-assistant";
const ISSUE_FORM = `${REPO}/issues/new`;
const MAX_URL_LENGTH = 7500; // same limit as share.py
const DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DAY_SETS = { all: [0, 1, 2, 3, 4, 5, 6], weekdays: [0, 1, 2, 3, 4], weekends: [5, 6] };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const STATES = ["NSW", "ACT", "VIC", "QLD", "SA", "TAS", "WA", "NT"];
// Periods are coloured by price, so a rate in the wrong place stands out: import runs from green (cheapest)
// to red (dearest), feed-in from grey (lowest) to dark blue (highest).
const SCALES = {
  buy: ["#2f9e44", "#82c91e", "#fab005", "#fd7e14", "#e03131", "#a61e4d"],
  sell: ["#adb5bd", "#74c0fc", "#339af0", "#1c7ed6", "#1864ab"],
};
const RESET_LABEL = { day: "per day", month: "per month", bill: "per bill" };
const STREAM_LABEL = { import: "Import (buying)", export: "Feed-in (selling)" };

const state = { library: null, coverage: null, plans: new Map() };
const app = document.getElementById("app");

// ---------- small helpers ----------

// Build an element. Text is always set as text, never parsed as HTML: plan files come from contributors.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style") el.style.cssText = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function getJSON(url) {
  const response = await fetch(url, { cache: "no-cache" });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response.json();
}

function dollars(value) {
  if (typeof value !== "number") return "?";
  return `$${value.toFixed(Math.max(2, decimals(value)))}`;
}
function decimals(value) {
  const text = String(value);
  return text.includes(".") ? text.split(".")[1].length : 0;
}
function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function hhmm(minutes) {
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}
function minutesOf(text) {
  const [hours, mins] = String(text).split(":").map(Number);
  return hours * 60 + mins;
}
// "Nov – Mar", or "Apr – May, Sep – Oct" for months that aren't one run (December wraps to January)
function monthRange(months) {
  const set = new Set(months);
  if (set.size === 12) return "All year";
  const starts = [...set].filter((m) => !set.has(m === 1 ? 12 : m - 1)).sort((a, b) => a - b);
  return starts.map((start) => {
    let end = start;
    while (set.has(end === 12 ? 1 : end + 1)) end = end === 12 ? 1 : end + 1;
    return start === end ? MONTHS[start - 1] : `${MONTHS[start - 1]}\u00a0–\u00a0${MONTHS[end - 1]}`;
  }).join(", ");
}

function seasonName(season, seasons) {
  const months = (seasons || {})[season];
  return months && months.length ? monthRange(months) : title(season.replace(/_/g, " "));
}

function title(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : "";
}

// Hash routes: #/?filters, #/plan/<file>, #/coverage?filters
function route() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = raw.split("?");
  return { path, params: new URLSearchParams(query) };
}
function go(path, params) {
  const query = params && [...params].length ? `?${params}` : "";
  const target = `#${path}${query}`;
  if (location.hash !== target) history.replaceState(null, "", target);
}

function status(entry) {
  const now = today();
  if (entry.effective_to && entry.effective_to < now) return "expired";
  if (entry.effective_from > now) return "upcoming";
  return "current";
}

function badges(entry) {
  const out = [];
  if (entry.errors.length) out.push(h("span", { class: "badge err" }, `${entry.errors.length} error${entry.errors.length > 1 ? "s" : ""}`));
  if (entry.warnings.length) out.push(h("span", { class: "badge warn" }, `${entry.warnings.length} warning${entry.warnings.length > 1 ? "s" : ""}`));
  const confirmed = entry.confirmations || 0;
  const needed = state.library.confirmations_needed || 2;
  if (entry.needs_review) {
    out.push(h("span", { class: "badge review", title: `Not yet confirmed by ${needed} people on the plan, or a problem was reported` }, "Needs review"));
    if (confirmed) out.push(h("span", { class: "badge plain", title: "People on this plan who confirmed it matches their bill" },
      `${confirmed} of ${needed} confirmations`));
  } else if (confirmed) {
    out.push(h("span", { class: "badge confirmed", title: "Confirmed to match their bill by people on this plan" },
      confirmed > 1 ? `Confirmed by ${confirmed}` : "Confirmed"));
  }
  const when = status(entry);
  out.push(h("span", { class: when === "current" ? "badge ok" : "badge plain" }, title(when)));
  return out;
}

// ---------- plan maths (mirrors engine/model.py and engine/resolver.py) ----------

function windowDays(value) {
  if (value === undefined) return DAY_SETS.all;
  if (typeof value === "string") return DAY_SETS[value] || [];
  return value.map((name) => DAY_NAMES.indexOf(name)).filter((i) => i >= 0);
}

// Pieces of one listed day a window covers, end exclusive. A window ending at or before its start
// wraps: start to midnight and midnight to end, both on the listed day (as Window.segments does).
function segments(win) {
  if (win.all_day) return [[0, 1440]];
  const start = minutesOf(win.start);
  const end = minutesOf(win.end);
  if (end > start) return [[start, end]];
  const pieces = [[start, 1440]];
  if (end > 0) pieces.push([0, end]);
  return pieces;
}

function covers(win, weekday, minute, season) {
  if (!windowDays(win.days).includes(weekday)) return false;
  if (win.seasons && win.seasons.length && !win.seasons.includes(season)) return false;
  return segments(win).some(([lo, hi]) => lo <= minute && minute < hi);
}

function defaultPeriod(periods) {
  return periods.find((p) => p.default) || (periods.length === 1 ? periods[0] : null);
}

// First non-default period whose window covers the moment, else the default (Resolver._period).
function periodAt(periods, weekday, minute, season) {
  for (const period of periods) {
    if (period.default) continue;
    if ((period.windows || []).some((w) => covers(w, weekday, minute, season))) return period;
  }
  return defaultPeriod(periods);
}

// One day as [startMinute, endMinute, period] runs.
function dayRuns(periods, weekday, season) {
  const cuts = new Set([0, 1440]);
  for (const period of periods) {
    for (const win of period.windows || []) for (const [lo, hi] of segments(win)) { cuts.add(lo); cuts.add(hi); }
  }
  const points = [...cuts].sort((a, b) => a - b);
  const runs = [];
  for (let i = 0; i < points.length - 1; i++) {
    const period = periodAt(periods, weekday, points[i], season);
    const last = runs[runs.length - 1];
    if (last && last[2] === period) last[1] = points[i + 1];
    else runs.push([points[i], points[i + 1], period]);
  }
  return runs;
}

function dayGroupLabel(days) {
  const key = days.join(",");
  if (key === "0,1,2,3,4,5,6") return "Every day";
  if (key === "0,1,2,3,4") return "Weekdays";
  if (key === "5,6") return "Weekends";
  return days.map((d) => DAY_LABELS[d]).join(", ");
}

// Rows for a stream: one per season (or one if no seasons), days with the same pattern grouped.
function timelineRows(plan, periods) {
  const seasons = Object.entries(plan.seasons || {});
  const usesSeasons = periods.some((p) => (p.windows || []).some((w) => w.seasons && w.seasons.length));
  let list = [[null, null]];
  if (usesSeasons && seasons.length) {
    const covered = new Set(seasons.flatMap(([, months]) => months));
    const rest = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].filter((m) => !covered.has(m));
    list = rest.length ? [...seasons, [null, rest]] : seasons;
  }
  const rows = [];
  for (const [season, months] of list) {
    const groups = [];
    for (let day = 0; day < 7; day++) {
      const runs = dayRuns(periods, day, season);
      const key = runs.map(([a, b, p]) => `${a}-${b}-${p ? p.id : "?"}`).join("|");
      const group = groups.find((g) => g.key === key);
      if (group) group.days.push(day);
      else groups.push({ key, runs, days: [day] });
    }
    for (const group of groups) {
      const label = dayGroupLabel(group.days);
      const seasonLabel = months ? (season ? seasonName(season, plan.seasons) : `Other months (${monthRange(months)})`) : null;
      rows.push({ label, seasonLabel, runs: group.runs });
    }
  }
  return rows;
}

// Price used for colouring: the rate, or a tiered period's first tier.
function headlineRate(period, periods) {
  if (!period.tiers) return period.rate;
  const tier = period.tiers[0];
  if (tier.rate !== undefined) return tier.rate;
  const other = periods.find((p) => p.id === tier.use_period);
  return other && !other.tiers ? other.rate : 0;
}

function colourMap(periods, scale) {
  const colours = SCALES[scale];
  const prices = [...new Set(periods.map((p) => headlineRate(p, periods)))].sort((a, b) => a - b);
  const map = new Map();
  for (const period of periods) {
    const rank = prices.indexOf(headlineRate(period, periods));
    const step = prices.length > 1 ? Math.round((rank * (colours.length - 1)) / (prices.length - 1)) : 0;
    map.set(period.id, colours[step]);
  }
  return map;
}

function rateText(period, periods) {
  if (!period.tiers) return [dollars(period.rate)];
  let previous = 0;
  const parts = period.tiers.map((tier) => {
    const price = tier.use_period
      ? `${(periods.find((p) => p.id === tier.use_period) || { name: tier.use_period }).name} rate`
      : dollars(tier.rate);
    if (tier.up_to_kwh === undefined) return `then ${price}`;
    const range = previous === 0 ? `first ${tier.up_to_kwh} kWh` : `${previous}–${tier.up_to_kwh} kWh`;
    previous = tier.up_to_kwh;
    return `${range} at ${price}`;
  });
  const reset = RESET_LABEL[period.tier_reset || "day"] || period.tier_reset;
  return [parts.join(", "), h("span", { class: "muted" }, ` (caps ${reset})`)];
}

function windowText(win, seasons) {
  const days = typeof win.days === "string" || win.days === undefined
    ? title(win.days || "all") === "All" ? "All days" : title(win.days)
    : win.days.map((d) => title(d)).join(", ");
  const time = win.all_day ? "all day" : `${win.start}–${win.end}`;
  const when = win.seasons && win.seasons.length ? ` (${win.seasons.map((s) => seasonName(s, seasons)).join(", ")})` : "";
  return `${days} ${time}${when}`;
}

// ---------- views ----------

function setNav(name) {
  for (const link of document.querySelectorAll("[data-nav]")) link.classList.toggle("active", link.dataset.nav === name);
}

const LIST_COLUMNS = [
  ["retailer", "Retailer"], ["plan", "Plan"], ["region", "State"], ["dnsp", "Distributor"],
  ["customer_type", "Customer"], ["effective_from", "Effective"], ["plan_id", "Plan ID"],
  ["source_checked", "Checked"], [null, "Status"],
];

function renderList(params) {
  setNav("list");
  const plans = state.library.plans;
  const pick = (key) => params.get(key) || "";
  const customer = params.has("customer") ? pick("customer") : "residential";
  const filters = {
    customer, region: pick("state"), dnsp: pick("dnsp"), retailer: pick("retailer"),
    q: pick("q").toLowerCase(), confirmed: params.get("confirmed") === "1", all: params.get("all") === "1",
  };
  const sort = pick("sort") || "-effective_from";

  const matches = (entry, skip) =>
    (skip === "customer" || !filters.customer || entry.customer_type === filters.customer) &&
    (skip === "region" || !filters.region || entry.region === filters.region) &&
    (skip === "dnsp" || !filters.dnsp || entry.dnsp === filters.dnsp) &&
    (skip === "retailer" || !filters.retailer || entry.retailer === filters.retailer) &&
    (!filters.q || [entry.retailer, entry.plan, entry.plan_id, entry.dnsp, entry.file].join(" ").toLowerCase().includes(filters.q)) &&
    (!filters.confirmed || !entry.needs_review) &&
    (filters.all || status(entry) !== "expired");

  const optionsFor = (key, skip) => [...new Set(plans.filter((e) => matches(e, skip)).map((e) => e[key]).filter(Boolean))].sort();

  const update = (key, value) => {
    const next = new URLSearchParams(params);
    if (value === "" && key !== "customer") next.delete(key); else next.set(key, value);
    if (key === "state") { next.delete("dnsp"); next.delete("retailer"); }
    if (key === "dnsp") next.delete("retailer");
    go("/", next);
    render();
  };
  const select = (label, key, value, options, allLabel) =>
    h("label", {}, label,
      h("select", { onchange: (e) => update(key, e.target.value) },
        h("option", { value: "" }, allLabel),
        options.map((o) => h("option", { value: o, selected: o === value }, o))));

  const rows = plans.filter((e) => matches(e));
  const [desc, sortKey] = sort.startsWith("-") ? [true, sort.slice(1)] : [false, sort];
  rows.sort((a, b) => {
    const x = a[sortKey] || ""; const y = b[sortKey] || "";
    return (x < y ? -1 : x > y ? 1 : 0) * (desc ? -1 : 1);
  });

  const stateOptions = [...new Set([...STATES.filter((s) => plans.some((e) => e.region === s)), ...optionsFor("region", "region")])];
  let searchTimer;
  const view = h("div", {},
    h("h1", {}, "Plan library"),
    h("p", { class: "muted" },
      `${plans.length} plan${plans.length === 1 ? "" : "s"} in the library. Pick one in Home Assistant under `,
      h("em", {}, "Choose from the plan library"), ", or copy its JSON from the plan page."),
    h("div", { class: "filters" },
      h("label", {}, "Customer",
        h("select", { onchange: (e) => update("customer", e.target.value) },
          [["residential", "Residential"], ["business", "Business"], ["", "Both"]].map(([v, t]) =>
            h("option", { value: v, selected: v === filters.customer }, t)))),
      select("State", "state", filters.region, stateOptions, "All states"),
      select("Distributor", "dnsp", filters.dnsp, optionsFor("dnsp", "dnsp"), "All distributors"),
      select("Retailer", "retailer", filters.retailer, optionsFor("retailer", "retailer"), "All retailers"),
      h("label", {}, "Search",
        h("input", {
          type: "search", value: pick("q"), placeholder: "Plan name, plan ID…",
          oninput: (e) => { clearTimeout(searchTimer); const v = e.target.value; searchTimer = setTimeout(() => update("q", v), 250); },
        })),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: filters.confirmed, onchange: (e) => update("confirmed", e.target.checked ? "1" : "") }), "Confirmed plans only"),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: filters.all, onchange: (e) => update("all", e.target.checked ? "1" : "") }), "Include expired"),
    ),
  );

  if (!rows.length) {
    view.append(h("div", { class: "card" },
      h("p", {}, "No library plans match these filters yet."),
      filters.confirmed ? h("p", {}, "Plans show as Confirmed once 2 people on them have pressed Confirm this plan. Untick Confirmed plans only to see the rest.") : null,
      h("p", {}, "You can build your plan step by step in Home Assistant and share it, or ",
        h("a", { href: `${ISSUE_FORM}?template=plan_submission.yml` }, "submit it here"), ".")));
  } else {
    const head = h("tr", {}, LIST_COLUMNS.map(([key, label]) => {
      if (!key) return h("th", {}, label);
      const cls = sortKey === key ? (desc ? "sortable sorted-desc" : "sortable sorted-asc") : "sortable";
      return h("th", { class: cls, onclick: () => update("sort", sortKey === key && !desc ? `-${key}` : key) }, label);
    }));
    const body = rows.map((entry) => h("tr", {},
      h("td", {}, entry.retailer),
      h("td", {}, h("a", { href: `#/plan/${encodeURIComponent(entry.file)}` }, entry.plan)),
      h("td", {}, entry.region),
      h("td", {}, entry.dnsp || "—"),
      h("td", {}, title(entry.customer_type)),
      h("td", {}, entry.effective_from, entry.effective_to ? ` to ${entry.effective_to}` : ""),
      h("td", {}, entry.plan_id || "—"),
      h("td", {}, entry.source_checked || "—"),
      h("td", {}, badges(entry)),
    ));
    view.append(h("div", { class: "table-wrap card", style: "padding:0" }, h("table", {}, h("thead", {}, head), h("tbody", {}, body))),
      h("p", { class: "muted" }, `${rows.length} shown.`));
  }
  app.replaceChildren(view);
}

function timelineView(plan, periods, scale) {
  const colours = colourMap(periods, scale);
  const rows = timelineRows(plan, periods);
  const wrap = h("div", { class: "timeline" });
  let lastSeason = null;
  for (const row of rows) {
    if (row.seasonLabel && row.seasonLabel !== lastSeason) {
      wrap.append(h("div", { class: "muted", style: "font-size:13px;margin-top:8px" }, row.seasonLabel));
      lastSeason = row.seasonLabel;
    }
    const bar = h("div", { class: "tl-bar" }, row.runs.map(([start, end, period]) => {
      const name = period ? period.name : "Not covered";
      return h("div", {
        class: "tl-seg",
        style: `width:${((end - start) / 1440) * 100}%;background:${period ? colours.get(period.id) : "repeating-linear-gradient(45deg,#c92a2a,#c92a2a 4px,#fff 4px,#fff 8px)"}`,
        title: `${name}: ${hhmm(start)}–${hhmm(end)}`,
      }, end - start >= 120 ? h("span", {}, name) : null);
    }));
    wrap.append(h("div", { class: "tl-row" }, h("div", { class: "tl-label" }, row.label), bar));
  }
  wrap.append(h("div", { class: "tl-axis" }, h("div"), h("div", {}, ["00", "03", "06", "09", "12", "15", "18", "21", "24"].map((t) => h("span", {}, t)))));
  const legend = h("div", { class: "legend" }, periods.map((p) => h("span", {}, h("i", { style: `background:${colours.get(p.id)}` }), p.name)));
  return [wrap, legend];
}

function ratesTable(periods, seasons) {
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Period"), h("th", {}, "Rate ", h("span", { style: "text-transform:none" }, "($/kWh)")), h("th", {}, "When"))),
    h("tbody", {}, periods.map((p) => h("tr", {},
      h("td", { title: `Meter id: ${p.id}` }, p.name),
      h("td", {}, rateText(p, periods)),
      h("td", {}, p.default ? "All other times" : (p.windows || []).map((w) => h("div", {}, windowText(w, seasons)))),
    )))));
}

function streamCard(plan, label, periods, extra, scale = "buy") {
  return h("div", { class: "card" },
    h("h2", { style: "margin-top:0" }, label),
    extra || null,
    timelineView(plan, periods, scale),
    ratesTable(periods, plan.seasons));
}

function reportLink(plan, entry) {
  const planText = JSON.stringify(plan, null, 2);
  const fields = {
    template: "plan_submission.yml",
    title: `[Plan] ${plan.retailer} ${plan.plan} (${plan.region}${plan.dnsp ? `, ${plan.dnsp}` : ""})`,
    kind: "Correction to a library plan",
    "retailer-plan": `${plan.retailer} ${plan.plan}`,
    dnsp: plan.dnsp || "",
    "plan-id": `${plan.plan_id || "unknown"}, from ${plan.effective_from}`,
    rates: "What's wrong, and what it should be:\n\n",
    source: `Reported from the plan browser; library file ${entry.file}`,
  };
  if (plan.customer_type) fields["customer-type"] = title(plan.customer_type);
  if (STATES.includes(plan.region)) fields.state = plan.region;
  const withPlan = `${ISSUE_FORM}?${new URLSearchParams({ ...fields, "plan-json": planText })}`;
  return withPlan.length <= MAX_URL_LENGTH ? withPlan : `${ISSUE_FORM}?${new URLSearchParams(fields)}`;
}

// A GitHub issue with everything filled in: the person only presses Submit; Plan feedback records it.
function confirmLink(plan, entry) {
  return `${ISSUE_FORM}?${new URLSearchParams({
    template: "plan_confirmation.yml",
    title: `[Confirm] ${plan.retailer} ${plan.plan} (${plan.region}${plan.dnsp ? `, ${plan.dnsp}` : ""})`,
    "plan-file": entry.file,
    "plan-id": plan.plan_id || "",
  })}`;
}

async function renderPlan(file) {
  setNav("list");
  const entry = state.library.plans.find((e) => e.file === file);
  if (!entry) {
    app.replaceChildren(h("div", { class: "card" }, h("p", {}, "That plan isn't in the library. "), h("a", { href: "#/" }, "Back to the plans")));
    return;
  }
  app.replaceChildren(h("p", { class: "muted" }, "Loading the plan…"));
  let plan = state.plans.get(file);
  if (!plan) {
    try { plan = await getJSON(file); state.plans.set(file, plan); } catch (err) {
      app.replaceChildren(h("div", { class: "notice err" }, `Couldn't load ${file}: ${err.message}`));
      return;
    }
  }
  const text = JSON.stringify(plan, null, 2);
  const copyButton = h("button", { class: "btn primary", onclick: async () => {
    try { await navigator.clipboard.writeText(text); copyButton.textContent = "Copied"; } catch { copyButton.textContent = "Copy failed: use Download"; }
    setTimeout(() => { copyButton.textContent = "Copy JSON"; }, 2000);
  } }, "Copy JSON");
  const download = h("a", { class: "btn", href: file, download: file.split("/").pop() }, "Download");
  const source = plan.source || {};
  const supply = plan.supply_charge || {};
  const holidays = (plan.holidays || {}).treat_as || "none";

  const view = h("div", {},
    h("p", {}, h("a", { href: "#/" }, "← All plans")),
    h("div", { class: "plan-head" },
      h("div", {},
        h("h1", {}, `${plan.retailer} · ${plan.plan}`),
        h("div", {}, badges(entry))),
      h("div", { class: "actions" }, copyButton, download,
        h("a", { class: "btn", href: confirmLink(plan, entry),
          title: "I'm on this plan and it matches my bill (opens a filled-in GitHub issue: just press Submit)" },
          "Confirm this plan"),
        h("a", { class: "btn", href: reportLink(plan, entry) }, "Report a problem"),
        h("a", { class: "btn", href: `${REPO}/blob/main/${entry.file}` }, "View on GitHub"))),
    h("div", { class: "facts card" },
      fact("State", plan.region), fact("Distributor", plan.dnsp || "—"), fact("Customer", title(plan.customer_type)),
      fact("Plan ID", plan.plan_id || "—"),
      fact("Effective", `${plan.effective_from}${plan.effective_to ? ` to ${plan.effective_to}` : " onwards"}`),
      fact("Supply charge", supply.rate !== undefined ? `${dollars(supply.rate)} ${supply.unit === "per_day" ? "per day" : supply.unit || ""}` : "—"),
      fact("Time zone", plan.timezone),
      fact("Public holidays", holidays === "none" ? "Same as any other day" : `Priced as a ${title(holidays)}`),
      fact("Source", [title(source.type || "?"), /^https?:\/\//i.test(source.url || "") ? h("span", {}, " · ", h("a", { href: source.url, rel: "noopener noreferrer" }, "link")) : null]),
      fact("Checked", source.checked || "—")),
  );

  if (entry.errors.length) view.append(h("div", { class: "notice err" }, "The validator found errors:", h("ul", {}, entry.errors.map((e) => h("li", {}, e)))));
  if (entry.warnings.length) view.append(h("div", { class: "notice warn" }, "Warnings:", h("ul", {}, entry.warnings.map((w) => h("li", {}, w)))));
  if (entry.needs_review) view.append(h("div", { class: "notice warn" }, `Needs review: it shows as Confirmed once ${state.library.confirmations_needed || 2} people on the plan have confirmed it. If you're on it, compare it with your bill: press Confirm this plan if it matches, or Report a problem if it doesn't.`));

  const streams = h("div", { class: "grid-2", style: "margin-top:16px" });
  for (const key of ["import", "export"]) {
    if (plan[key] && plan[key].periods) streams.append(streamCard(plan, STREAM_LABEL[key], plan[key].periods, null, key === "export" ? "sell" : "buy"));
  }
  for (const load of plan.controlled_load || []) {
    const charge = load.supply_charge ? h("p", { class: "muted" }, `Supply charge ${dollars(load.supply_charge.rate)} per day`) : null;
    streams.append(streamCard(plan, `Controlled load: ${load.name}`, load.periods, charge));
  }
  view.append(streams);

  if ((plan.demand || []).length) {
    view.append(h("h2", {}, "Demand charges"), h("div", { class: "card table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Charge"), h("th", {}, "Rate"), h("th", {}, "When"))),
      h("tbody", {}, plan.demand.map((d) => h("tr", {},
        h("td", {}, d.name), h("td", {}, `${dollars(d.rate)} ${d.unit === "per_kw_per_month" ? "per kW per month" : "per kW per day"}`),
        h("td", {}, d.windows.map((w) => h("div", {}, windowText(w, plan.seasons))))))))));
  }
  if (plan.notes) view.append(h("div", { class: "card", style: "margin-top:16px" }, h("strong", {}, "Notes: "), plan.notes));


  view.append(h("details", {}, h("summary", {}, "Plan JSON"), h("pre", { class: "json" }, text)));
  app.replaceChildren(view);
  document.title = `${plan.retailer} ${plan.plan}: Energy Tariff Assistant`;
}

function fact(label, value) {
  return h("div", {}, h("span", {}, label), value);
}

function renderCoverage(params) {
  setNav("coverage");
  const view = h("div", {}, h("h1", {}, "Coverage"));
  if (!state.coverage) {
    view.append(h("p", { class: "muted" }, "Retailer coverage isn't available yet."));
    app.replaceChildren(view);
    return;
  }
  const region = params.get("state") || "";
  const library = state.library.plans.filter((e) => status(e) !== "expired");
  const count = (fn) => library.filter(fn).length;
  const distributors = Object.entries(state.coverage.distributors || {})
    .filter(([, d]) => !region || d.state === region)
    .sort(([a, x], [b, y]) => (x.state + a).localeCompare(y.state + b));

  view.append(
    h("p", { class: "muted" },
      "Retailers selling electricity plans in each distributor's area, from the Energy Made Easy plan data",
      state.coverage.updated_at ? ` (updated ${state.coverage.updated_at.slice(0, 10)})` : "",
      ", against the plans in this library. Distributors with no library plans are where contributions help most."),
    h("div", { class: "filters" },
      h("label", {}, "State",
        h("select", { onchange: (e) => { const p = new URLSearchParams(); if (e.target.value) p.set("state", e.target.value); go("/coverage", p); render(); } },
          h("option", { value: "" }, "All states"),
          STATES.map((s) => h("option", { value: s, selected: s === region }, s))))),
    h("div", { class: "card table-wrap", style: "padding:0" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "State"), h("th", {}, "Distributor"), h("th", { class: "num" }, "Retailers on EME"), h("th", { class: "num" }, "Library plans"), h("th", {}, "Retailers with library plans"))),
      h("tbody", {}, distributors.map(([name, d]) => {
        const plans = library.filter((e) => e.dnsp === name);
        const retailers = [...new Set(plans.map((e) => e.retailer))].sort();
        return h("tr", {},
          h("td", {}, d.state), h("td", {}, name),
          h("td", { class: "num" }, (d.retailers || []).length),
          h("td", { class: "num" }, plans.length
            ? h("a", { href: `#/?${new URLSearchParams({ customer: "", state: d.state, dnsp: name })}` }, plans.length)
            : h("span", { class: "badge warn" }, "none yet")),
          h("td", {}, retailers.join(", ") || "—"));
      })))),
  );

  const retailers = (state.coverage.retailers || []).filter((r) => !region || (r.states || []).includes(region));
  view.append(h("h2", {}, `Retailers (${retailers.length})`),
    h("div", { class: "card table-wrap", style: "padding:0" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Retailer"), h("th", {}, "States"), h("th", { class: "num" }, "Plans on EME"), h("th", { class: "num" }, "Library plans"))),
      h("tbody", {}, retailers.map((r) => {
        const n = count((e) => e.retailer.toLowerCase() === r.name.toLowerCase() && (!region || e.region === region));
        return h("tr", {}, h("td", {}, r.name), h("td", {}, (r.states || []).join(", ")),
          h("td", { class: "num" }, r.plans ?? "—"), h("td", { class: "num" }, n || "—"));
      })))));
  app.replaceChildren(view);
}

function render() {
  const { path, params } = route();
  document.title = "Energy Tariff Assistant: plan library";
  if (path.startsWith("/plan/")) renderPlan(decodeURIComponent(path.slice(6)));
  else if (path === "/coverage") renderCoverage(params);
  else renderList(params);
}

async function start() {
  try {
    state.library = await getJSON("data/library.json");
  } catch (err) {
    app.replaceChildren(h("div", { class: "notice err" }, `Couldn't load the library: ${err.message}`));
    return;
  }
  try { state.coverage = await getJSON("data/coverage.json"); } catch { state.coverage = null; }
  document.getElementById("built").textContent = ` Library built ${state.library.built_at.replace("T", " ").slice(0, 16)} UTC.`;
  window.addEventListener("hashchange", render);
  render();
}

start();
