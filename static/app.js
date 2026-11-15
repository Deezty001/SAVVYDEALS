"use strict";

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => "$" + Math.round(Number(n) || 0).toLocaleString("en-AU");
const TIER_LABEL = { STEAL: "Steal", GOOD_DEAL: "Good deal", OFFER: "Make an offer", FAIR: "Fair", OVERPRICED: "Overpriced", SUSPECT: "Suspicious" };

// Only ever put http(s) links into href attributes.
const safeHref = (u) => { try { const x = new URL(u); return x.protocol === "https:" ? x.href : "#"; } catch { return "#"; } };

const state = { pollTimer: null, view: "all", region: "NSW", deals: [], open: new Set(), catalog: [], autoTimer: null, scanning: false, settings: null };

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 5000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("Login required"); }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error */ }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = Array.isArray(d) ? d.map((e) => e.msg).join("; ") : (d || `Request failed (${res.status})`);
    throw new Error(msg);
  }
  return data;
}

// ---------- deals ----------
function dealParams() {
  const p = new URLSearchParams({
    state: state.region,
    source: $("source").value,
    socket_or_series: $("series").value,
    sort_by: $("sort").value,
    search: $("search").value.trim(),
  });
  const v = state.view;
  if (v === "STEAL" || v === "GOOD_OR_BETTER" || v === "OFFER") p.set("tier", v);
  if (v === "drops") p.set("drops_only", "true");
  if (v === "bundles") p.set("bundles_only", "true");
  if (v === "saved") p.set("saved_only", "true");
  return p;
}

let loadSeq = 0;
async function loadDeals() {
  const seq = ++loadSeq;
  try {
    const data = await api("/api/deals?" + dealParams());
    if (seq !== loadSeq) return; // a newer request superseded this one
    state.deals = data.deals;
    const s = data.stats;
    $("c-all").textContent = s.total_deals;
    $("c-steal").textContent = s.steals_count;
    $("c-good").textContent = s.steals_count + s.good_deals_count;
    $("c-drops").textContent = s.drops_count;
    $("c-bundles").textContent = s.bundles_count;
    $("c-saved").textContent = s.saved_count;
    const good = s.steals_count + s.good_deals_count;
    $("subhead").innerHTML = `<b>${s.total_deals.toLocaleString("en-AU")}</b> listings tracked (${s.fb_count.toLocaleString("en-AU")} Facebook · ${s.gt_count.toLocaleString("en-AU")} Gumtree) · <b>${good.toLocaleString("en-AU")}</b> worth buying · <b>${s.drops_count.toLocaleString("en-AU")}</b> price drops`;
    state.total = data.total_matching;
    renderDeals(state.total);
  } catch (e) {
    $("list").innerHTML = `<div class="sa-empty">Couldn't load deals: ${esc(e.message)}</div>`;
  }
}

// When the ad went up: Facebook gives us the real listing time; otherwise fall back to when we first saw it.
function listingDate(d) {
  const real = Number(d.listed_at) > 0;
  const ms = real ? d.listed_at * 1000 : Date.parse((d.discovered_at || "").replace(" ", "T").replace(/Z?$/, "Z"));
  if (!ms || Number.isNaN(ms)) return null;
  const mins = Math.max(0, (Date.now() - ms) / 60000);
  const rel = mins < 60 ? `${Math.max(1, Math.round(mins))}m ago` : mins < 1440 ? `${Math.round(mins / 60)}h ago` : `${Math.round(mins / 1440)}d ago`;
  const abs = new Date(ms).toLocaleDateString("en-AU", { day: "numeric", month: "short", year: new Date(ms).getFullYear() === new Date().getFullYear() ? undefined : "numeric" });
  const full = new Date(ms).toLocaleString("en-AU", { dateStyle: "medium", timeStyle: "short" });
  return { real, rel, abs, full };
}

function postedTag(d) {
  const p = d.posted_at || "";
  let out = "";
  if (p.includes("Dropped")) out += `<span class="tag drop">${esc(p)}</span>`;
  else if (p.includes("Just Listed")) out += `<span class="tag">Just listed</span>`;
  const t = listingDate(d);
  if (t) out += `<span class="tag" title="${esc((t.real ? "Listed " : "First seen ") + t.full)}">${t.real ? "" : "seen "}${esc(t.abs)} · ${esc(t.rel)}</span>`;
  return out;
}

const ICON_PC = `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>`;
const ICON_PART = `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/></svg>`;
const placeholder = (d, cls) => `<div class="${cls}">${d.is_bundle ? ICON_PC : ICON_PART}</div>`;
const BADGE_VARIANT = { STEAL: "filled", GOOD_DEAL: "done", OFFER: "open", FAIR: "", OVERPRICED: "alert", SUSPECT: "alert" };
const CAT_LABEL = { GPU: "GPU", CPU: "CPU", RAM: "RAM", MOBO: "Motherboard", MOTHERBOARD: "Motherboard", SSD: "Storage", STORAGE: "Storage", PSU: "PSU", CASE: "Case", COOLER: "Cooler" };
const isAssumed = (p) => /\(assumed/i.test(p.canonical_name);
const catLabel = (c) => CAT_LABEL[String(c || "").toUpperCase()] || (c ? c.charAt(0).toUpperCase() + c.slice(1).toLowerCase() : "Part");
const badge = (tier) => `<span class="sa-badge" data-variant="${BADGE_VARIANT[tier] || ""}"><i></i>${esc(TIER_LABEL[tier] || tier)}</span>`;

// The description FB gives us is often just "AU$1,800 • Blayney, NSW"; only show it when it adds something.
function cleanDesc(d) {
  const t = (d.description || "").replace("[Deep Inspected] ", "").trim();
  return t.length >= 40 && !/^AU\$[\d,]+\s*[•·]/.test(t) ? t : "";
}

function renderDeals(total) {
  const list = $("list");
  $("shown").textContent = state.deals.length ? `Showing ${state.deals.length.toLocaleString("en-AU")} of ${(total || state.deals.length).toLocaleString("en-AU")}` : "";
  if (!state.deals.length) {
    list.innerHTML = `<div class="sa-empty"><strong>No listings match these filters</strong><br>Try widening them, or pull fresh listings.<br><br><button class="sa-btn sa-btn-outline" data-act="reset">Reset filters</button> <button class="sa-btn sa-btn-primary" data-act="scan">Scan now</button></div>`;
    return;
  }
  const rows = state.deals.map((d) => {
    const open = state.open.has(d.id);
    if (open) return expandedDeal(d);
    const net = Math.round(d.net_profit_aud ?? d.profit_margin_aud);
    const profit = net > 0 ? `<span class="pos">+${money(net)} net</span>` : `<span class="neg">${net === 0 ? "Break-even" : "−" + money(-net) + " net"}</span>`;
    const thumb = d.image_url
      ? `<div class="lphoto"><img src="${esc(safeHref(d.image_url))}" alt="" loading="lazy" referrerpolicy="no-referrer" data-hide-on-error data-bundle="${d.is_bundle ? 1 : 0}" /></div>`
      : placeholder(d, "lphoto");
    const src = d.source.includes("Facebook") ? "Facebook" : "Gumtree";
    const conf = Math.round(d.confidence || 0);
    const disc = Math.round(d.discount_pct || 0);
    const chips = (d.matched_parts || []).filter((p) => !isAssumed(p)).slice(0, 6).map((p) => `<span class="spec"><em>${esc(catLabel(p.category))}</em><span>${esc(p.canonical_name)}</span></span>`).join("");
    const desc = cleanDesc(d);
    return `<article class="deal t-${esc(d.deal_tier)}${open ? " open" : ""}" data-id="${esc(d.id)}">
      <div class="lrow" role="button" tabindex="0" data-act="toggle" aria-expanded="${open}">
        ${thumb}
        <div class="lmain">
          <h3 class="ltitle">${esc(d.title)}</h3>
          <div class="lmeta">${badge(d.deal_tier)}<span>${esc(src)} · ${esc(placeLabel(d))}</span>${postedTag(d)}${d.needs_read ? `<span class="tag">needs deep read</span>` : ""}${flagTags(d)}</div>
          ${chips ? `<div class="specs">${chips}</div>` : ""}
          ${desc ? `<p class="ldesc">${esc(desc)}</p>` : ""}
        </div>
        <div class="lside">
          <span class="lprice">${money(d.price_aud)}</span>
          <span class="lworth">worth ${money(d.estimated_value_aud)}${disc > 0 ? ` · ${disc}% under` : ""}</span>
          <span class="lprofit">${profit}</span>
          <span class="lconf">${conf}/100 <progress class="meter" max="100" value="${conf}"></progress></span>
        </div>
      </div>
    </article>`;
  });
  const shown = state.deals.length;
  const more = total > shown ? `<div class="empty-more">Showing the top ${shown} of ${total.toLocaleString("en-AU")}. Narrow the filters to see the rest.</div>` : "";
  list.innerHTML = rows.join("") + more;
}

function placeLabel(d) {
  if (!d.suburb) return d.location;
  const name = d.suburb.split(" ").map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
  return d.drive_min ? `${name} (~${Math.round(d.drive_min)} min)` : name;
}

function flagTags(d) {
  const out = [];
  for (const x of (d.flags || "").split(",").filter(Boolean)) {
    if (x.startsWith("falling:")) out.push(`<span class="tag drop">falling price: ${esc(x.slice(8))}</span>`);
    else if (x === "interstate_pc") out.push(`<span class="tag drop">interstate PC</span>`);
    else if (x.startsWith("relist_or_crosspost")) out.push(`<span class="tag">relisted</span>`);
    else if (x.startsWith("too_cheap")) out.push(`<span class="tag drop">too cheap: likely scam</span>`);
  }
  if (d.kind && d.kind !== "private") out.push(`<span class="tag">${esc(d.kind)}</span>`);
  return out.join("");
}

// Expanded listing: the row's photo grows in place; header, description, specs and actions sit beside it.
function expandedDeal(d) {
  const inspected = (d.description || "").includes("[Deep Inspected]");
  const desc = (d.description || "").replace("[Deep Inspected] ", "").trim();
  const isFB = d.source.includes("Facebook");
  const saved = d.status === "SAVED";
  const net = Math.round(d.net_profit_aud ?? d.profit_margin_aud);
  const profit = net > 0 ? `<span class="pos">+${money(net)} net</span>` : `<span class="neg">${net === 0 ? "Break-even" : "−" + money(-net) + " net"}</span>`;
  const conf = Math.round(d.confidence || 0);
  const disc = Math.round(d.discount_pct || 0);
  const src = isFB ? "Facebook" : "Gumtree";
  const chips = (d.matched_parts || []).filter((p) => !isAssumed(p)).slice(0, 6).map((p) => `<span class="spec"><em>${esc(catLabel(p.category))}</em><span>${esc(p.canonical_name)}</span></span>`).join("");
  const photo = d.image_url
    ? `<div class="dx-photo"><img src="${esc(safeHref(d.image_url))}" alt="" referrerpolicy="no-referrer" data-hide-on-error data-bundle="${d.is_bundle ? 1 : 0}" /></div>`
    : placeholder(d, "dx-photo");
  const parts = (d.matched_parts || []).map((p) => {
    const assumed = isAssumed(p);
    const name = p.canonical_name.replace(/\s*\(assumed[^)]*\)/i, "");
    return `<div><span class="k">${esc(name)}</span><small>${money(p.avg_used_aud)} ${assumed ? "est." : "used"}</small></div>`;
  }).join("");
  const t = listingDate(d);
  const listed = t ? [t.real ? "Listed" : "First seen", `${t.abs} · ${t.rel}`] : (d.posted_at ? ["Listed", d.posted_at.replace(/^Live FB Ad$/, "Live listing").replace(/^\W+/, "")] : null);
  const facts = [
    ["Seller", d.kind && d.kind !== "private" ? d.kind : "Private"],
    ["Location", placeLabel(d)],
    ["Marketplace", isFB ? "Facebook Marketplace" : "Gumtree"],
    listed,
  ].filter(Boolean).map(([k, v]) => `<div><span class="lbl">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join("");
  return `<article class="deal open t-${esc(d.deal_tier)}" data-id="${esc(d.id)}">
    <div class="dx">
      <div data-act="toggle" role="button" tabindex="0" aria-expanded="true" aria-label="Collapse listing" class="dx-photo-btn">${photo}</div>
      <div class="dx-body">
        <div class="dx-head" data-act="toggle" role="button" tabindex="0" aria-expanded="true">
          <div class="dx-title">
            <h3 class="ltitle">${esc(d.title)}</h3>
            <div class="lmeta">${badge(d.deal_tier)}<span>${esc(src)} · ${esc(placeLabel(d))}</span>${postedTag(d)}${d.needs_read ? `<span class="tag">needs deep read</span>` : ""}${flagTags(d)}</div>
            ${chips ? `<div class="specs">${chips}</div>` : ""}
          </div>
          <div class="lside">
            <span class="lprice">${money(d.price_aud)}</span>
            <span class="lworth">worth ${money(d.estimated_value_aud)}${disc > 0 ? ` · ${disc}% under` : ""}</span>
            <span class="lprofit">${profit}</span>
            <span class="lconf">${conf}/100 <progress class="meter" max="100" value="${conf}"></progress></span>
          </div>
        </div>
        ${desc ? `<div><h4>Description</h4><p>${esc(desc.slice(0, 800))}${desc.length > 800 ? "…" : ""}</p></div>` : ""}
        <div class="dx-specs">
          <h4>Specs</h4>
          ${parts ? `<div class="dx-grid dx-parts">${parts}</div>` : ""}
          <div class="dx-grid dx-facts">${facts}</div>
        </div>
        <div class="acts">
          <a class="sa-btn sa-btn-primary sa-btn-sm" href="${esc(safeHref(d.url))}" target="_blank" rel="noopener noreferrer">Open listing ↗</a>
          <button class="sa-btn sa-btn-outline sa-btn-sm" data-act="offer">Copy offer message</button>
          ${isFB ? `<button class="sa-btn sa-btn-outline sa-btn-sm" data-act="inspect">${inspected ? "Re-read specs" : "Read full specs"}</button>` : ""}
          <button class="sa-btn sa-btn-outline sa-btn-sm ${saved ? "on" : ""}" data-act="save">${saved ? "★ Saved" : "☆ Save"}</button>
          <button class="sa-btn sa-btn-outline sa-btn-sm" data-act="notify">Send alert</button>
          <span class="spacer"></span>
          <button class="sa-btn sa-btn-ghost sa-btn-sm" data-act="hide">Hide</button>
        </div>
      </div>
    </div>
  </article>`;
}

async function dealAction(act, id, btn) {
  const d = state.deals.find((x) => x.id === id);
  try {
    if (act === "toggle") {
      state.open.has(id) ? state.open.delete(id) : state.open.add(id);
      renderDeals(state.total);
      return;
    }
    if (act === "save") {
      await api(`/api/deals/${encodeURIComponent(id)}/status`, { method: "PATCH", body: { status: d.status === "SAVED" ? "NEW" : "SAVED" } });
    } else if (act === "hide") {
      await api(`/api/deals/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("Hidden.");
    } else if (act === "offer") {
      const offer = d.offer_aud || Math.round(Math.min(d.price_aud * 0.85, d.estimated_value_aud * 0.78) / 10) * 10;
      const suburb = (d.suburb || d.location || "your area").split(",")[0];
      await navigator.clipboard.writeText(d.offer_message || `Hi, is the ${d.title} still available? Would you take $${offer}? I can pick up from ${suburb} with cash.`);
      toast(`Copied a $${offer} offer message. Send it yourself.`);
      return;
    } else if (act === "notify") {
      const r = await api(`/api/deals/${encodeURIComponent(id)}/notify`, { method: "POST" });
      toast(r.ok ? "Alert sent." : (r.message || "Alert failed."));
      return;
    } else if (act === "inspect") {
      btn.disabled = true; btn.textContent = "Reading…";
      const r = await api(`/api/deals/${encodeURIComponent(id)}/inspect`, { method: "POST" });
      toast(r.excluded || !r.ok ? (r.message || "No extra specs found.") : `Specs updated. Estimated value ${money(r.estimated_value_aud)}.`);
    }
    await loadDeals();
  } catch (e) {
    toast(e.message);
    if (btn) btn.disabled = false;
  }
}

// ---------- deep scan (background job) ----------
function renderProgress(st) {
  const box = $("progress");
  const p = st.progress;
  if (!st.running && !(p && p.stopped)) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const pct = p && p.chunks_total ? Math.round((p.chunk / p.chunks_total) * 100) : 0;
  const where = p ? `${esc(p.state)}${p.states.length > 1 ? ` (${p.states.indexOf(p.state) + 1}/${p.states.length})` : ""}` : "";
  const msg = st.running
    ? `Deep scan ${where}: ${pct}% · ${p.listings_seen} listings read · ${p.matched} matched`
    : `Deep scan stopped: ${esc(p.stopped)} · ${p.listings_seen} listings read · ${p.matched} matched`;
  box.innerHTML = `<div class="progress-in"><div class="row"><span>${msg}</span>${st.running ? '<button class="sa-btn sa-btn-outline sa-btn-sm" data-cancel>Cancel</button>' : ""}</div>
    <div class="bar"><span class="w"></span></div></div>`;
  box.querySelector(".w").style.width = pct + "%"; // CSSOM is allowed by the CSP, inline style attributes are not
  if (p && p.errors && p.errors.length) showBanner(p.errors);
}

async function pollStatus() {
  try {
    const st = await api("/api/scan/status");
    renderProgress(st);
    if (st.running) { await loadDeals(); return; }
  } catch { /* keep polling */ }
  clearInterval(state.pollTimer); state.pollTimer = null;
  $("deepBtn").disabled = false; $("scanBtn").disabled = false;
  loadDeals();
}

function watchScan() {
  $("deepBtn").disabled = true; $("scanBtn").disabled = true;
  if (!state.pollTimer) state.pollTimer = setInterval(pollStatus, 4000);
  pollStatus();
}

async function startDeepScan() {
  const all = state.region === "ALL";
  if (!confirm(`Deep scan ${all ? "ALL states" : state.region}? It searches every part we can value (about 160 queries per state), splitting each into price bands so nothing is capped at 24 results. Can take 10+ minutes per state. Facebook may block heavy scraping.`)) return;
  try {
    await api("/api/scan/deep", { method: "POST", body: {} });
    toast("Deep scan started. Results appear as they are found.");
    watchScan();
  } catch (e) { toast(e.message); }
}

// ---------- scan health ----------
const HEALTH_WORD = { healthy: "healthy", degraded: "degraded", down: "DOWN", idle: "idle" };
const RANK = { down: 3, degraded: 2, healthy: 1, idle: 0 };

async function loadHealth() {
  try {
    const h = await api("/api/health");
    state.health = h;
    const worst = Object.entries(h).sort((a, b) => RANK[b[1].status] - RANK[a[1].status])[0];
    const status = worst ? worst[1].status : "idle";
    $("healthDot").className = `dot ${status}`;
    $("healthLabel").textContent = status === "healthy" ? "Scans healthy" : status === "idle" ? "Scan health" : `${worst[0][0].toUpperCase() + worst[0].slice(1)} ${HEALTH_WORD[status]}`;
    if ($("healthDlg").open) renderHealth();
  } catch { /* health is advisory; never block the dashboard */ }
}

function renderHealth() {
  const h = state.health || {};
  $("healthBody").innerHTML = Object.entries(h).map(([src, v]) => `
    <div class="health-card">
      <h3><span class="dot ${esc(v.status)}"></span>${esc(src)}: ${esc(HEALTH_WORD[v.status] || v.status)}</h3>
      ${v.reasons.length ? `<ul>${v.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      <div class="muted small">Last success: ${esc(v.last_ok_at || "never")} · ${v.attempts_last_hour} attempts and ${v.listings_seen_last_hour} listings read in the last hour</div>
      <div class="health-recent">${v.recent.map((r) => `<span>${esc(r.ts.slice(11, 16))} · ${esc(r.strategy)} · ${esc(r.status)} · ${r.parsed} read · ${esc(r.query)}${r.note ? " · " + esc(r.note) : ""}</span>`).join("")}</div>
    </div>`).join("") || `<p class="muted">No scans recorded yet.</p>`;
}

// ---------- scanning ----------
function showBanner(errors) {
  const b = $("banner");
  if (!errors || !errors.length) { b.classList.add("hidden"); return; }
  b.innerHTML = `Scan finished with problems:<ul>${errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`;
  b.classList.remove("hidden");
}

async function runScan(body, label) {
  setTimeout(loadHealth, 8000);
  if (state.scanning) return;
  state.scanning = true;
  $("scanBtn").disabled = true;
  $("scanLabel").textContent = "Scanning…";
  toast(`Scanning ${label}…`);
  try {
    const r = await api("/api/scan/live", { method: "POST", body });
    const alerts = r.alerts_sent ? ` · ${r.alerts_sent} alert${r.alerts_sent > 1 ? "s" : ""} sent` : "";
    toast(`${r.matched_deals} matching deals from ${r.raw_scraped_count} listings${alerts}.`);
    showBanner(r.errors);
    await loadDeals();
  } catch (e) {
    toast(e.message);
  } finally {
    state.scanning = false;
    $("scanBtn").disabled = false;
    $("scanLabel").textContent = "Scan now";
  }
}

function toggleAuto() {
  const btn = $("autoBtn");
  if (state.autoTimer) {
    clearInterval(state.autoTimer); state.autoTimer = null;
    btn.querySelector(".switch-label").textContent = "Auto: off"; btn.setAttribute("aria-pressed", "false");
    toast("Auto-scan off.");
  } else {
    btn.querySelector(".switch-label").textContent = "Auto: every 5 min"; btn.setAttribute("aria-pressed", "true");
    runScan({ sources: ["facebook"], full_fb_sweep: false }, "Facebook");
    state.autoTimer = setInterval(() => runScan({ sources: ["facebook"], full_fb_sweep: false }, "Facebook"), 5 * 60 * 1000);
    toast("Auto-scan on (every 5 minutes while this tab is open).");
  }
}

// ---------- check an ad ----------
async function checkAd() {
  const raw_text = $("pasteText").value.trim();
  if (!raw_text) return;
  const url = $("pasteUrl").value.trim();
  const box = $("pasteResult");
  box.classList.remove("hidden");
  try {
    const r = await api("/api/smart-paste", { method: "POST", body: { raw_text, url: url || "https://www.facebook.com/marketplace/", save_to_feed: true } });
    if (!r.ok || r.excluded || r.deal_tier === "UNMATCHED") {
      box.innerHTML = `<div class="result">${esc(r.error || r.reason || "No known parts matched.")}</div>`;
      return;
    }
    const parts = (r.matched_parts || []).map((p) => `<div class="row"><span>${esc(p.canonical_name)}</span><span class="muted num">${money(p.avg_used_aud)}</span></div>`).join("");
    const diff = Math.round(r.profit_margin_aud);
    box.innerHTML = `<div class="result">
      <div class="row"><strong>Asking ${money(r.detected_price_aud)}</strong>${badge(r.deal_tier)}</div>
      ${parts}
      <div class="row"><span class="muted">Estimated value</span><strong class="num">${money(r.estimated_value_aud)}</strong></div>
      <div class="row"><span class="muted">${diff >= 0 ? "Below value" : "Above value"}</span><strong class="num">${money(Math.abs(diff))} (${esc(r.discount_pct)}%)</strong></div>
    </div>`;
    loadDeals();
  } catch (e) {
    box.innerHTML = `<div class="result">${esc(e.message)}</div>`;
  }
}

// ---------- price guide ----------
async function openCatalog() {
  const data = await api("/api/catalog");
  state.catalog = data.parts;
  renderCatalog();
}
function renderCatalog() {
  const q = $("catalogSearch").value.toLowerCase();
  const rows = state.catalog.filter((p) => `${p.canonical_name} ${p.socket_or_series} ${p.category}`.toLowerCase().includes(q));
  $("catalogBody").innerHTML = rows.map((p) => `<tr data-part="${p.id}">
    <td>${esc(p.canonical_name)}<div class="muted small">${esc(p.socket_or_series)}</div></td>
    <td><input class="sa-inline-input num" data-f="avg" type="number" min="1" value="${esc(p.avg_used_aud)}" aria-label="Average price"></td>
    <td><input class="sa-inline-input num" data-f="good" type="number" min="1" value="${esc(p.good_deal_aud)}" aria-label="Good deal price"></td>
    <td><input class="sa-inline-input num" data-f="steal" type="number" min="1" value="${esc(p.steal_aud)}" aria-label="Steal price"></td>
    <td><button class="sa-btn sa-btn-outline sa-btn-sm" data-save-part>Save</button></td></tr>`).join("");
}
async function savePart(tr) {
  const v = (f) => parseFloat(tr.querySelector(`[data-f="${f}"]`).value);
  try {
    await api(`/api/catalog/${tr.dataset.part}`, { method: "PUT", body: { avg_used_aud: v("avg"), good_deal_aud: v("good"), steal_aud: v("steal") } });
    toast("Price saved. New scans use it; re-scan to re-value existing listings.");
  } catch (e) { toast(e.message); }
}

// ---------- settings ----------
const csv = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);

async function loadSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  state.region = s.state;
  $("region").value = s.state;
  $("heading").textContent = s.city;
  $("setState").value = s.state;
  $("setCity").value = s.city;
  $("setRadius").value = s.radius_km;
  $("setTier").value = s.auto_notify_tier;
  $("setDisc").value = s.min_discount_pct;
  $("setProfit").value = s.min_profit_aud;
  $("setTgChat").value = s.telegram_chat_id;
  $("setQueries").value = s.search_queries.join(", ");
  $("setExcludes").value = s.exclude_keywords.join(", ");
  $("setDiscord").value = ""; $("setTgToken").value = "";
  $("discordNote").textContent = s.discord_configured ? "Saved. Leave blank to keep it, or type a new URL to replace it." : "Not set.";
  $("tgNote").textContent = s.telegram_configured ? "Saved. Leave blank to keep it." : "Not set.";
}

async function saveSettings(ev) {
  ev.preventDefault();
  const secret = (id) => { const v = $(id).value.trim(); return v === "" ? null : v; }; // null = keep stored secret
  try {
    await api("/api/settings", { method: "PUT", body: {
      state: $("setState").value, city: $("setCity").value, radius_km: Number($("setRadius").value) || 50,
      min_discount_pct: Number($("setDisc").value) || 0, min_profit_aud: Number($("setProfit").value) || 0,
      auto_notify_tier: $("setTier").value,
      discord_webhook_url: secret("setDiscord"), telegram_bot_token: secret("setTgToken"),
      telegram_chat_id: $("setTgChat").value.trim(),
      search_queries: csv($("setQueries").value), exclude_keywords: csv($("setExcludes").value),
    } });
    await loadSettings();
    $("settingsDlg").close();
    toast("Settings saved.");
    loadDeals();
  } catch (e) { toast(e.message); }
}

async function buildBookmarklet() {
  const c = await api("/api/ingest-config");
  // Runs on the Facebook page: collects visible marketplace cards and POSTs them with the secret token.
  const code = `(async()=>{const a=[...document.querySelectorAll('a[href*="/marketplace/item/"]')];const items=a.map(x=>{const l=(x.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean);const p=l.find(s=>/\\$/.test(s))||'';const m=p.match(/\\$\\s*([\\d,]+)/);const loc=l.find(s=>/,\\s*(NSW|VIC|QLD|WA|SA|ACT|TAS|NT)\\b/.test(s))||'';const t=l.filter(s=>s!==p&&s!==loc).sort((u,v)=>v.length-u.length)[0]||'';const i=x.querySelector('img');return{title:t,description:l.join(' | '),price_aud:m?+m[1].replace(/,/g,''):0,location:loc,url:x.href.split('?')[0],image_url:i?i.src:'',posted_at:'Bookmarklet'}}).filter(i=>i.price_aud>0&&i.title);try{const r=await fetch(${JSON.stringify(c.endpoint)},{method:'POST',headers:{'Content-Type':'application/json','X-Ingest-Token':${JSON.stringify(c.token)}},body:JSON.stringify({listings:items})});const j=await r.json();alert('Deal Sniper: '+(j.matched_deals??j.detail)+' matched of '+items.length)}catch(e){alert('Deal Sniper: blocked by this page ('+e.message+'). Use the Playwright scanner instead.')}})()`;
  $("bookmarklet").href = "javascript:" + encodeURIComponent(code);
}

// ---------- wiring ----------
function resetFilters() {
  state.view = "all";
  $("search").value = ""; $("source").value = "ALL"; $("series").value = "ALL"; $("sort").value = "score";
  document.querySelectorAll(".sa-tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.view === "all")));
  loadDeals();
}

function initTheme() {
  const root = document.documentElement;
  try { const t = localStorage.getItem("theme"); if (t === "dark" || t === "light") root.dataset.theme = t; } catch { /* storage may be blocked */ }
  $("themeBtn").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch { /* ignore */ }
  });
}

function init() {
  initTheme();
  $("tabs").addEventListener("click", (e) => {
    const t = e.target.closest(".sa-tab"); if (!t) return;
    state.view = t.dataset.view;
    document.querySelectorAll(".sa-tab").forEach((x) => x.setAttribute("aria-selected", String(x === t)));
    loadDeals();
  });

  $("list").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    if (b.dataset.act === "toggle" && e.target.closest("a, button")) return;
    const act = b.dataset.act;
    if (act === "reset") return resetFilters();
    if (act === "scan") return runScan({ sources: ["facebook", "gumtree"], full_fb_sweep: false }, "Facebook and Gumtree");
    const id = b.closest(".deal")?.dataset.id;
    if (id) dealAction(act, id, b);
  });
  $("list").addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches?.(".lrow, .dx-head, .dx-photo-btn")) { e.preventDefault(); e.target.click(); }
  });
  // CSP forbids inline onerror, so swap broken photos for the placeholder via a capture-phase listener.
  $("list").addEventListener("error", (e) => {
    const img = e.target;
    if (!img.matches?.("[data-hide-on-error]")) return;
    const box = img.parentElement;
    box.innerHTML = img.dataset.bundle === "1" ? ICON_PC : ICON_PART;
  }, true);

  $("region").addEventListener("change", async (e) => {
    try {
      const r = await api("/api/region", { method: "POST", body: { state: e.target.value } });
      state.region = r.state;
      await loadSettings();
      await loadDeals();
      toast(`Switched to ${r.city}. Press Scan now to pull fresh listings.`);
    } catch (err) { toast(err.message); }
  });

  let t;
  $("search").addEventListener("input", () => { clearTimeout(t); t = setTimeout(loadDeals, 200); });
  $("search").addEventListener("keydown", (e) => {
    const q = $("search").value.trim();
    if (e.key === "Enter" && q) runScan({ sources: ["facebook", "gumtree"], custom_query: q }, `"${q}"`);
  });
  ["source", "series", "sort"].forEach((id) => $(id).addEventListener("change", loadDeals));

  $("scanBtn").addEventListener("click", () => runScan({ sources: ["facebook", "gumtree"], full_fb_sweep: false }, "Facebook and Gumtree"));
  $("autoBtn").addEventListener("click", toggleAuto);
  $("deepBtn").addEventListener("click", startDeepScan);
  $("progress").addEventListener("click", (e) => { if (e.target.closest("[data-cancel]")) api("/api/scan/cancel", { method: "POST" }).then(() => toast("Cancelling after the current batch…")); });

  document.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => {
    const id = b.dataset.open;
    $(id).showModal();
    if (id === "catalogDlg") openCatalog().catch((e) => toast(e.message));
    if (id === "healthDlg") loadHealth().then(renderHealth);
    if (id === "settingsDlg") { loadSettings(); buildBookmarklet().catch(() => {}); }
  }));
  document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));
  document.querySelectorAll("dialog").forEach((d) => d.addEventListener("click", (e) => { if (e.target === d) d.close(); }));

  $("pasteGo").addEventListener("click", checkAd);
  $("catalogSearch").addEventListener("input", renderCatalog);
  $("catalogBody").addEventListener("click", (e) => { const b = e.target.closest("[data-save-part]"); if (b) savePart(b.closest("tr")); });
  $("settingsForm").addEventListener("submit", saveSettings);
  $("bookmarklet").addEventListener("click", (e) => { e.preventDefault(); toast("Drag it to your bookmarks bar instead of clicking."); });

  loadHealth();
  setInterval(loadHealth, 60 * 1000);
  loadSettings().then(loadDeals).then(() => api("/api/scan/status")).then((st) => { if (st.running) watchScan(); }).catch((e) => toast(e.message));
}

document.addEventListener("DOMContentLoaded", init);
