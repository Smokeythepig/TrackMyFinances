/* ── Utilities ──────────────────────────────────────────────────────────── */

const $ = id => document.getElementById(id);
const fmt = n => n == null ? "—" : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(n);
const fmtDate = s => s ? new Date(s + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "";
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let toastTimer;
function toast(msg, type = "info") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "show";
  el.style.borderColor = type === "error" ? "var(--red)" : type === "ok" ? "var(--green)" : "var(--border)";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = "", 3000);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/* ── Navigation ─────────────────────────────────────────────────────────── */

document.querySelectorAll(".nav-item").forEach(el => {
  el.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
    document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
    el.classList.add("active");
    $(`page-${el.dataset.page}`).classList.add("active");
    if (el.dataset.page === "transactions") loadTransactions();
    if (el.dataset.page === "income") renderIncome();
    if (el.dataset.page === "budgets") loadBudgets();
    if (el.dataset.page === "recurring") loadRecurring();
    if (el.dataset.page === "networth") { renderNetWorth(); loadGoals(); }
    if (el.dataset.page === "alerts") loadAlerts();
  });
});

/* ── State ──────────────────────────────────────────────────────────────── */

let state = {
  accounts: [], transactions: [], manual: [], history: [],
  summary: null, monthly: [], merchants: [], recurring: [], budgets: [], categories: [],
  income: null, goals: [],
};

/* ── Charts ─────────────────────────────────────────────────────────────── */

let charts = {};

const CHART_DEFAULTS = {
  plugins: { legend: { labels: { color: "#8b8fa8", boxWidth: 12, font: { size: 11 } } } },
};

function destroyChart(id) {
  if (charts[id]) { charts[id].destroy(); delete charts[id]; }
}

function buildAllocationChart(accounts) {
  destroyChart("allocation");
  const groups = {};
  accounts.forEach(a => {
    const t = a.type || "other";
    groups[t] = (groups[t] || 0) + (a.ledger ?? a.available ?? 0);
  });
  const labels = Object.keys(groups);
  const data = labels.map(k => Math.abs(groups[k]));
  charts["allocation"] = new Chart($("chart-allocation"), {
    type: "doughnut",
    data: {
      labels,
      datasets: [{ data, backgroundColor: ["#6c63ff","#4ecca3","#fbb86c","#ff7b7b","#60a5fa"], borderWidth: 0 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { ...CHART_DEFAULTS.plugins, tooltip: { callbacks: { label: ctx => ` ${fmt(ctx.raw)}` } } },
    },
  });
}

function buildSpendingChart(transactions) {
  destroyChart("spending");
  const cutoff = new Date(); cutoff.setDate(cutoff.getDate() - 30);
  const cats = {};
  transactions.filter(t => parseFloat(t.amount) < 0 && !t.is_transfer).forEach(t => {
    const d = new Date(t.date + "T00:00:00");
    if (d < cutoff) return;
    const cat = t.category || "uncategorized";
    cats[cat] = (cats[cat] || 0) + Math.abs(parseFloat(t.amount));
  });
  const sorted = Object.entries(cats).sort((a, b) => b[1] - a[1]).slice(0, 10);
  charts["spending"] = new Chart($("chart-spending"), {
    type: "bar",
    data: {
      labels: sorted.map(x => x[0]),
      datasets: [{ label: "Spent", data: sorted.map(x => x[1]), backgroundColor: "#6c63ff", borderRadius: 6 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, indexAxis: "y",
      plugins: { ...CHART_DEFAULTS.plugins, legend: { display: false } },
      scales: {
        x: { ticks: { color: "#8b8fa8", callback: v => fmt(v) }, grid: { color: "#2e3347" } },
        y: { ticks: { color: "#8b8fa8" }, grid: { display: false } },
      },
    },
  });
}

function buildCashFlowChart(monthly) {
  destroyChart("cashflow");
  charts["cashflow"] = new Chart($("chart-cashflow"), {
    type: "bar",
    data: {
      labels: monthly.map(m => { const [y, mo] = m.month.split("-"); return new Date(+y, +mo - 1).toLocaleString("default", { month: "short", year: "2-digit" }); }),
      datasets: [
        { label: "Income", data: monthly.map(m => m.income), backgroundColor: "#4ecca3", borderRadius: 4 },
        { label: "Expenses", data: monthly.map(m => m.expenses), backgroundColor: "#ff5a5f", borderRadius: 4 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { ...CHART_DEFAULTS.plugins, tooltip: { callbacks: { label: ctx => ` ${ctx.dataset.label}: ${fmt(ctx.raw)}` } } },
      scales: {
        x: { ticks: { color: "#8b8fa8" }, grid: { color: "#2e3347" } },
        y: { ticks: { color: "#8b8fa8", callback: v => fmt(v) }, grid: { color: "#2e3347" } },
      },
    },
  });
}

function buildNetWorthChart(history) {
  destroyChart("networth");
  charts["networth"] = new Chart($("chart-networth"), {
    type: "line",
    data: {
      labels: history.map(h => fmtDate(h.snapped_at)),
      datasets: [{
        label: "Net Worth",
        data: history.map(h => h.net_worth),
        borderColor: "#6c63ff",
        backgroundColor: "rgba(108,99,255,0.12)",
        fill: true,
        tension: 0.3,
        pointRadius: 3,
        pointBackgroundColor: "#6c63ff",
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { ...CHART_DEFAULTS.plugins, tooltip: { callbacks: { label: ctx => ` ${fmt(ctx.raw)}` } } },
      scales: {
        x: { ticks: { color: "#8b8fa8" }, grid: { color: "#2e3347" } },
        y: { ticks: { color: "#8b8fa8", callback: v => fmt(v) }, grid: { color: "#2e3347" } },
      },
    },
  });
}

/* ── Dashboard ──────────────────────────────────────────────────────────── */

function computeTotals() {
  const accts = state.accounts.filter(a => !a.hidden);
  const manual = state.manual;
  const linkedAssets = accts.filter(a => a.type !== "credit" && a.type !== "loan")
    .reduce((s, a) => s + (a.ledger ?? a.available ?? 0), 0);
  const linkedLiabilities = accts.filter(a => a.type === "credit" || a.type === "loan")
    .reduce((s, a) => s + Math.abs(a.ledger ?? a.available ?? 0), 0);
  const manualAssets = manual.filter(m => m.entry_type === "asset").reduce((s, m) => s + m.amount, 0);
  const manualLiabilities = manual.filter(m => m.entry_type === "liability").reduce((s, m) => s + m.amount, 0);
  const totalAssets = linkedAssets + manualAssets;
  const totalLiabilities = linkedLiabilities + manualLiabilities;
  return { totalAssets, totalLiabilities, netWorth: totalAssets - totalLiabilities };
}

function deltaBadge(cur, prev, invert = false) {
  if (!prev) return "";
  const diff = cur - prev;
  if (Math.abs(diff) < 0.005) return "same as last month";
  const good = invert ? diff < 0 : diff > 0;
  const arrow = diff > 0 ? "▲" : "▼";
  return `<span class="${good ? "delta-good" : "delta-bad"}">${arrow} ${fmt(Math.abs(diff))}</span> vs last month`;
}

function renderDashboard() {
  const { netWorth } = computeTotals();
  $("stat-networth").textContent = fmt(netWorth);

  const s = state.summary;
  if (s) {
    $("stat-income").textContent = fmt(s.current.income);
    $("stat-income-delta").innerHTML = deltaBadge(s.current.income, s.previous.income);
    $("stat-spent").textContent = fmt(s.current.spending);
    $("stat-spent-delta").innerHTML = deltaBadge(s.current.spending, s.previous.spending, true);
    $("stat-savings").textContent = s.current.savings_rate != null ? s.current.savings_rate + "%" : "—";
    $("stat-savings").className = "stat-value " + (s.current.savings_rate > 0 ? "green" : s.current.savings_rate < 0 ? "red" : "");
    $("stat-savings-sub").textContent = s.current.savings_rate != null ? `net ${fmt(s.current.net)} this month` : "";
  }

  const inc = state.income;
  if (inc?.next_paydays?.length) {
    const next = new Date(inc.next_paydays[0] + "T00:00:00");
    const days = Math.round((next - new Date().setHours(0, 0, 0, 0)) / 86400000);
    $("stat-payday").textContent = next.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    $("stat-payday-sub").textContent = days === 0 ? "today!" : `in ${days} day${days === 1 ? "" : "s"}${inc.avg_paycheck ? ` · ~${fmt(inc.avg_paycheck)}` : ""}`;
  }

  const activeSubs = state.recurring.filter(r => r.active && !r.ignored);
  const monthlySubCost = activeSubs.reduce((sum, r) => sum + r.monthly_cost, 0);
  $("stat-subs").textContent = fmt(monthlySubCost);
  $("stat-subs-sub").textContent = `${activeSubs.length} active — see Recurring`;

  buildAllocationChart(state.accounts.filter(a => !a.hidden));
  buildSpendingChart(state.transactions);
  buildCashFlowChart(state.monthly);

  // Top merchants
  $("top-merchants").innerHTML = state.merchants.length ? state.merchants.slice(0, 8).map(m => `
    <div class="mini-row">
      <div class="mini-label">${esc(m.description)} <span class="mini-sub">×${m.count}</span></div>
      <div class="mini-amount red">${fmt(m.spent)}</div>
    </div>`).join("") : `<p class="empty">No spending data yet.</p>`;

  // Recent activity
  $("recent-activity").innerHTML = state.transactions.slice(0, 8).map(t => {
    const amt = parseFloat(t.amount);
    return `
    <div class="mini-row">
      <div class="mini-label">${esc(t.description)} <span class="mini-sub">${fmtDate(t.date)}</span></div>
      <div class="mini-amount ${amt > 0 ? "green" : "red"}">${amt > 0 ? "+" : ""}${fmt(amt)}</div>
    </div>`;
  }).join("") || `<p class="empty">No transactions yet.</p>`;
}

/* ── Accounts page ──────────────────────────────────────────────────────── */

function renderAccounts() {
  const list = $("accounts-list");
  const institutions = [...new Set(state.accounts.map(a => a.institution_name))];
  $("sync-banner-sub").textContent =
    `${institutions.length} institutions · ${state.accounts.length} accounts · syncs on refresh + every 6h`;

  if (!state.accounts.length) {
    list.innerHTML = `<p class="empty">No accounts yet. Connect SimpleFIN below.</p>`;
  } else {
    const groups = {};
    state.accounts.forEach(a => (groups[a.institution_name] ||= []).push(a));
    list.innerHTML = Object.entries(groups).map(([inst, accts]) => {
      const total = accts.filter(a => !a.hidden).reduce((s, a) => {
        const b = a.ledger ?? a.available ?? 0;
        return s + (a.type === "credit" || a.type === "loan" ? -Math.abs(b) : b);
      }, 0);
      return `
      <div class="inst-group">
        <div class="inst-head">
          <span class="inst-name">${esc(inst)}</span>
          <span class="inst-total ${total < 0 ? "red" : ""}">${fmt(total)}</span>
        </div>
        ${accts.map(a => accountCard(a)).join("")}
      </div>`;
    }).join("");
  }
}

function accountCard(a) {
      const bal = a.ledger != null ? a.ledger : a.available;
      const subText = [a.subtype, a.last_four ? `••••${a.last_four}` : ""].filter(Boolean).join(" · ");
      const isImported = a.enrollment_id === "manual";
      return `
        <div class="account-card ${a.hidden ? "acct-hidden" : ""}">
          <div class="acct-info">
            <div class="acct-name">${esc(a.name)}</div>
            <div class="acct-sub">${esc(subText)}</div>
          </div>
          <div style="display:flex;align-items:center;gap:.8rem">
            ${a.hidden ? `<span class="badge badge-other" title="Excluded from net worth and analytics">hidden</span>` : ""}
            ${isImported ? `<span class="badge badge-other">imported</span>` : ""}
            <span class="badge badge-${esc(a.type || 'other')}">${esc(a.type || "—")}</span>
            <div class="acct-balance">
              <div class="bal-main">${fmt(bal)}</div>
              ${a.ledger != null && a.available != null && a.available !== a.ledger ? `<div class="bal-ledger">Avail ${fmt(a.available)}</div>` : ""}
            </div>
            <button class="btn-secondary btn-small" title="${a.hidden ? "Include in net worth and analytics" : "Exclude from net worth and analytics"}" onclick="toggleAccountHidden('${esc(a.id)}', ${a.hidden ? "false" : "true"})">${a.hidden ? "Show" : "Hide"}</button>
            ${isImported ? `<button class="btn-danger" onclick="removeImportedAccount('${esc(a.id)}')">✕</button>` : ""}
          </div>
        </div>`;
}

async function toggleAccountHidden(id, hidden) {
  await api(`/api/accounts/${id}/hidden`, { method: "PUT", body: JSON.stringify({ hidden }) });
  toast(hidden ? "Account hidden from totals" : "Account included in totals", "ok");
  await loadAll();
}

function showSimplefin(connected) {
  $("simplefin-disconnected").style.display = connected ? "none" : "block";
  $("sync-banner").style.display = connected ? "flex" : "none";
}

$("form-simplefin").addEventListener("submit", async e => {
  e.preventDefault();
  const btn = e.target.querySelector("button");
  btn.disabled = true; btn.textContent = "Connecting…";
  try {
    const res = await api("/api/simplefin/connect", {
      method: "POST",
      body: JSON.stringify({ token: new FormData(e.target).get("token") }),
    });
    if (res.errors?.length) toast("Connected with warnings: " + res.errors[0], "error");
    else toast(`SimpleFIN connected — ${res.accounts} account(s) synced`, "ok");
    e.target.reset();
    showSimplefin(true);
    await loadAll();
  } catch (err) {
    let msg = err.message;
    try { msg = JSON.parse(msg).error || msg; } catch {}
    toast(msg, "error");
  } finally {
    btn.disabled = false; btn.textContent = "Connect";
  }
});

$("btn-simplefin-disconnect").addEventListener("click", async () => {
  if (!confirm("Disconnect SimpleFIN? This deletes all locally cached SimpleFIN accounts and transactions.")) return;
  await api("/api/simplefin", { method: "DELETE" });
  toast("SimpleFIN disconnected", "ok");
  showSimplefin(false);
  await loadAll();
});

async function removeImportedAccount(id) {
  if (!confirm("Delete this imported account and all its transactions?")) return;
  await api(`/api/accounts/${id}`, { method: "DELETE" });
  toast("Imported account deleted", "ok");
  await loadAll();
}

$("form-import").addEventListener("submit", async e => {
  e.preventDefault();
  const form = e.target;
  const fd = new FormData(form);
  const file = fd.get("file");
  if (!file || !file.size) { toast("Choose a CSV file first.", "error"); return; }
  const btn = form.querySelector("button[type=submit]");
  btn.disabled = true; btn.textContent = "Importing…";
  try {
    const csvText = await file.text();
    const res = await api("/api/import/csv", {
      method: "POST",
      body: JSON.stringify({
        account_name: fd.get("account_name"),
        account_type: fd.get("account_type"),
        sign: fd.get("sign"),
        balance: fd.get("balance") || null,
        csv: csvText,
      }),
    });
    toast(`Imported ${res.imported} transactions${res.inverted ? " (amounts inverted)" : ""}`, "ok");
    form.reset();
    await loadAll();
  } catch (err) {
    let msg = err.message;
    try { msg = JSON.parse(msg).error || msg; } catch {}
    toast("Import failed: " + msg, "error");
  } finally {
    btn.disabled = false; btn.textContent = "Import CSV";
  }
});

/* ── Transactions page ──────────────────────────────────────────────────── */

function txnQuery() {
  const p = new URLSearchParams();
  const q = $("txn-search").value.trim();
  if (q) p.set("q", q);
  if ($("txn-account-filter").value) p.set("account_id", $("txn-account-filter").value);
  if ($("txn-category-filter").value) p.set("category", $("txn-category-filter").value);
  if ($("txn-start").value) p.set("start", $("txn-start").value);
  if ($("txn-end").value) p.set("end", $("txn-end").value);
  if ($("txn-hide-transfers").checked) p.set("hide_transfers", "1");
  return p;
}

let txnRows = [];

async function loadTransactions() {
  const p = txnQuery();
  p.set("limit", "1000");
  txnRows = await api("/api/transactions?" + p.toString());
  renderTransactions();
}

function renderTransactions() {
  const body = $("txn-body");
  const rows = txnRows;

  const total = rows.reduce((s, t) => s + parseFloat(t.amount), 0);
  const spent = rows.filter(t => t.amount < 0).reduce((s, t) => s - parseFloat(t.amount), 0);
  const received = rows.filter(t => t.amount > 0).reduce((s, t) => s + parseFloat(t.amount), 0);
  $("txn-summary").innerHTML = rows.length
    ? `${rows.length} transactions · <span class="green">in ${fmt(received)}</span> · <span class="red">out ${fmt(spent)}</span> · net <b>${fmt(total)}</b>`
    : "";

  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="6" class="empty">No transactions.</td></tr>`;
    return;
  }

  body.innerHTML = rows.map(t => {
    const amt = parseFloat(t.amount);
    const amtClass = amt > 0 ? "amount-pos" : "amount-neg";
    const catLabel = t.category || "—";
    return `<tr class="${t.is_transfer ? "row-transfer" : ""}">
      <td>${fmtDate(t.date)}</td>
      <td>${esc(t.description) || "—"}${t.is_transfer ? ` <span class="transfer-tag">transfer</span>` : ""}</td>
      <td>${esc(t.account_name) || "—"}</td>
      <td><span class="cat-chip ${t.overridden ? "overridden" : ""}" data-txn="${esc(t.id)}" data-cat="${esc(t.category || "")}" title="Click to recategorize">${esc(catLabel)}</span></td>
      <td class="${amtClass}">${fmt(Math.abs(amt))}</td>
      <td><span class="status-pill ${esc(t.status)}">${esc(t.status) || "—"}</span></td>
    </tr>`;
  }).join("");

  body.querySelectorAll(".cat-chip").forEach(chip => {
    chip.addEventListener("click", () => editCategory(chip));
  });
}

function editCategory(chip) {
  const txnId = chip.dataset.txn;
  const current = chip.dataset.cat;
  const input = document.createElement("input");
  input.className = "cat-edit";
  input.value = current;
  input.setAttribute("list", "category-list");
  chip.replaceWith(input);
  input.focus();
  input.select();

  const save = async () => {
    const val = input.value.trim();
    try {
      await api(`/api/transactions/${txnId}/category`, {
        method: "PUT",
        body: JSON.stringify({ category: val }),
      });
      toast(val ? `Recategorized as "${val}"` : "Category reset", "ok");
    } catch (e) {
      toast("Failed: " + e.message, "error");
    }
    await Promise.all([loadTransactions(), loadCategories()]);
  };

  input.addEventListener("keydown", e => {
    if (e.key === "Enter") input.blur();
    if (e.key === "Escape") { input.value = current; input.blur(); }
  });
  input.addEventListener("blur", save, { once: true });
}

$("btn-export").addEventListener("click", () => {
  window.location.href = "/api/export/transactions.csv?" + txnQuery().toString();
});

/* ── Income page ────────────────────────────────────────────────────────── */

function buildIncomeChart(monthly, target) {
  destroyChart("income");
  const datasets = [{
    type: "bar", label: "Income", data: monthly.map(m => m.income),
    backgroundColor: "#4ecca3", borderRadius: 4,
  }];
  if (target) {
    datasets.push({
      type: "line", label: "Target / mo", data: monthly.map(() => target / 12),
      borderColor: "#fbb86c", borderDash: [6, 4], pointRadius: 0, fill: false,
    });
  }
  charts["income"] = new Chart($("chart-income"), {
    data: {
      labels: monthly.map(m => { const [y, mo] = m.month.split("-"); return new Date(+y, +mo - 1).toLocaleString("default", { month: "short", year: "2-digit" }); }),
      datasets,
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { ...CHART_DEFAULTS.plugins, tooltip: { callbacks: { label: ctx => ` ${ctx.dataset.label}: ${fmt(ctx.raw)}` } } },
      scales: {
        x: { ticks: { color: "#8b8fa8" }, grid: { color: "#2e3347" } },
        y: { ticks: { color: "#8b8fa8", callback: v => fmt(v) }, grid: { color: "#2e3347" } },
      },
    },
  });
}

function renderIncome() {
  const inc = state.income;
  if (!inc) return;
  const c = inc.comp;

  $("inc-ytd").textContent = fmt(inc.ytd_total);
  $("inc-ytd-sub").textContent = inc.ytd_other > 0 ? `${fmt(inc.ytd_paychecks)} paychecks + ${fmt(inc.ytd_other)} other` : "";
  $("inc-avg").textContent = inc.avg_paycheck ? fmt(inc.avg_paycheck) : "—";
  $("inc-avg-sub").textContent = c ? `base check ≈ ${fmt(c.per_check_net)}` : "";

  if (c) {
    $("inc-net-ote").textContent = fmt(c.est_net_ote);
    $("inc-net-ote-sub").textContent = `${fmt(c.ote)} gross · ~${c.effective_tax_rate}% tax`;
    $("inc-commission").textContent = c.commission_progress_pct != null ? c.commission_progress_pct + "%" : "—";
    $("inc-commission").className = "stat-value " + (c.commission_progress_pct >= 100 ? "green" : "");
    $("inc-commission-sub").textContent = c.commission_target
      ? `≈ ${fmt(c.commission_gross_ytd)} gross of ${fmt(c.commission_target)}`
      : "";
    $("inc-pace").textContent = c.pace_pct != null ? c.pace_pct + "%" : "—";
    $("inc-pace").className = "stat-value " + (c.pace_pct >= 100 ? "green" : c.pace_pct >= 85 ? "" : "red");
    $("inc-pace-sub").textContent = `vs est. take-home ${fmt(c.est_net_ote)}/yr`;
    const form = $("form-comp");
    if (!form.base.value) { form.base.value = c.base; form.commission.value = c.commission_target; }
  } else {
    $("inc-net-ote").textContent = "—";
    $("inc-net-ote-sub").textContent = "enter your comp below";
    $("inc-commission").textContent = "—";
    $("inc-pace").textContent = "—";
  }

  buildIncomeChart(state.monthly, c ? c.est_net_ote : null);

  $("paycheck-body").innerHTML = inc.paychecks.length ? inc.paychecks.map(p => `
    <tr>
      <td>${fmtDate(p.date)}</td>
      <td>${esc(p.description)}</td>
      <td class="amount-pos">${fmt(p.amount)}</td>
      <td>${p.commission ? `<span class="commission-tag">+${fmt(p.commission)}</span>` : `<span class="mini-sub">base</span>`}</td>
    </tr>
  `).join("") : `<tr><td colspan="4" class="empty">No payroll deposits detected yet.</td></tr>`;
}

$("form-comp").addEventListener("submit", async e => {
  e.preventDefault();
  const fd = new FormData(e.target);
  await api("/api/income/comp", {
    method: "PUT",
    body: JSON.stringify({ base: parseFloat(fd.get("base")) || 0, commission: parseFloat(fd.get("commission")) || 0 }),
  });
  toast("Compensation saved", "ok");
  state.income = await api("/api/income/summary");
  renderIncome();
});

/* ── Savings goals ──────────────────────────────────────────────────────── */

async function loadGoals() {
  state.goals = await api("/api/goals");
  renderGoals();
}

function renderGoals() {
  const sel = $("goal-account-select");
  const withGoal = new Set(state.goals.map(g => g.account_id));
  sel.innerHTML = `<option value="">Choose account…</option>` +
    state.accounts.filter(a => a.type !== "credit" && a.type !== "loan" && !withGoal.has(a.id))
      .map(a => `<option value="${esc(a.id)}">${esc(a.institution_name)} – ${esc(a.name)}</option>`).join("");

  const list = $("goals-list");
  if (!state.goals.length) {
    list.innerHTML = `<p class="empty">No goals yet. Pick an account (e.g. a SoFi vault) and set a target.</p>`;
    return;
  }
  list.innerHTML = state.goals.map(g => {
    const bal = g.balance ?? 0;
    const pct = Math.min(100, g.target > 0 ? (bal / g.target) * 100 : 0);
    const done = bal >= g.target;
    return `
      <div class="budget-card">
        <div class="budget-head">
          <div class="budget-name">${esc(g.name)} <span class="mini-sub">${esc(g.institution_name)}</span></div>
          <div class="budget-nums">
            <span class="${done ? "green" : ""}">${fmt(bal)}</span>
            <span class="mini-sub">of ${fmt(g.target)}</span>
            <button class="btn-danger" onclick="deleteGoal('${esc(g.account_id)}')">✕</button>
          </div>
        </div>
        <div class="progress-track"><div class="progress-fill ${done ? "" : ""}" style="width:${pct}%"></div></div>
        <div class="budget-foot">${done ? `<span class="green">Goal reached! 🎉</span>` : `${fmt(g.target - bal)} to go (${pct.toFixed(0)}%)`}</div>
      </div>`;
  }).join("");
}

$("form-goal").addEventListener("submit", async e => {
  e.preventDefault();
  const fd = new FormData(e.target);
  if (!fd.get("account_id")) return;
  await api("/api/goals", { method: "POST", body: JSON.stringify({ account_id: fd.get("account_id"), target: parseFloat(fd.get("target")) }) });
  e.target.reset();
  toast("Goal set", "ok");
  await loadGoals();
});

async function deleteGoal(accountId) {
  await api(`/api/goals/${accountId}`, { method: "DELETE" });
  await loadGoals();
}

/* ── Budgets page ───────────────────────────────────────────────────────── */

async function loadBudgets() {
  state.budgets = await api("/api/budgets");
  renderBudgets();
}

function renderBudgets() {
  const budgets = state.budgets;
  const totalBudget = budgets.reduce((s, b) => s + b.monthly_limit, 0);
  const totalSpent = budgets.reduce((s, b) => s + b.spent, 0);
  $("budget-total").textContent = fmt(totalBudget);
  $("budget-spent").textContent = fmt(totalSpent);
  $("budget-remaining").textContent = fmt(totalBudget - totalSpent);
  $("budget-remaining").className = "stat-value " + (totalBudget - totalSpent >= 0 ? "green" : "red");

  const list = $("budgets-list");
  if (!budgets.length) {
    list.innerHTML = `<p class="empty">No budgets yet. Add one above — categories come from your transactions (dining, shopping, groceries…).</p>`;
    return;
  }
  list.innerHTML = budgets.map(b => {
    const pct = Math.min(100, b.monthly_limit > 0 ? (b.spent / b.monthly_limit) * 100 : 0);
    const over = b.spent > b.monthly_limit;
    const warn = !over && pct >= 80;
    return `
      <div class="budget-card">
        <div class="budget-head">
          <div class="budget-name">${esc(b.category)}</div>
          <div class="budget-nums">
            <span class="${over ? "red" : ""}">${fmt(b.spent)}</span>
            <span class="mini-sub">of ${fmt(b.monthly_limit)}</span>
            <button class="btn-danger" onclick="deleteBudget('${encodeURIComponent(b.category)}')">✕</button>
          </div>
        </div>
        <div class="progress-track">
          <div class="progress-fill ${over ? "over" : warn ? "warn" : ""}" style="width:${pct}%"></div>
        </div>
        <div class="budget-foot">${over
          ? `<span class="red">Over by ${fmt(b.spent - b.monthly_limit)}</span>`
          : `${fmt(b.monthly_limit - b.spent)} left this month`}</div>
      </div>`;
  }).join("");
}

$("form-budget").addEventListener("submit", async e => {
  e.preventDefault();
  const fd = new FormData(e.target);
  await api("/api/budgets", {
    method: "POST",
    body: JSON.stringify({ category: fd.get("category"), monthly_limit: parseFloat(fd.get("monthly_limit")) }),
  });
  e.target.reset();
  toast("Budget saved", "ok");
  await loadBudgets();
});

async function deleteBudget(encodedCategory) {
  await api(`/api/budgets/${encodedCategory}`, { method: "DELETE" });
  toast("Budget removed", "ok");
  await loadBudgets();
}

/* ── Recurring page ─────────────────────────────────────────────────────── */

async function loadRecurring() {
  const res = await api("/api/recurring");
  state.recurring = res.items;
  state.recurringDismissed = res.dismissed_count;
  renderRecurring();
}

function renderRecurring() {
  const recs = state.recurring;
  const active = recs.filter(r => r.active && !r.ignored);
  const monthly = active.reduce((s, r) => s + r.monthly_cost, 0);
  $("rec-count").textContent = active.length;
  $("rec-monthly").textContent = fmt(monthly);
  $("rec-yearly").textContent = fmt(monthly * 12);

  const note = $("dismissed-note");
  if (state.recurringDismissed > 0) {
    note.style.display = "block";
    note.innerHTML = `${state.recurringDismissed} removed as not recurring · <a href="#" onclick="restoreDismissed();return false" style="color:var(--accent)">restore all</a>`;
  } else {
    note.style.display = "none";
  }

  const list = $("recurring-list");
  if (!recs.length) {
    list.innerHTML = `<p class="empty">No recurring charges detected yet. They appear after ≥3 charges from the same merchant at a regular interval — or add one manually above.</p>`;
    return;
  }
  list.innerHTML = recs.map(r => `
    <div class="recurring-card ${!r.active || r.ignored ? "inactive" : ""}">
      <div class="acct-info">
        <div class="acct-name">${esc(r.description)}</div>
        <div class="acct-sub">${r.manual
          ? `${r.cadence} · added manually${r.next_expected ? ` · next ${fmtDate(r.next_expected)}` : ""}`
          : `${r.cadence}${r.variable ? " (variable)" : ""} · ${r.occurrences}× · last ${fmtDate(r.last_date)}${r.active ? ` · next ~${fmtDate(r.next_expected)}` : ""}`}</div>
      </div>
      <div style="display:flex;align-items:center;gap:.8rem">
        ${r.manual ? `<span class="badge badge-investment">manual</span>` : ""}
        ${!r.active ? `<span class="badge badge-loan">inactive</span>` : ""}
        ${r.ignored ? `<span class="badge badge-other">ignored</span>` : ""}
        <div class="acct-balance">
          <div class="bal-main">${fmt(r.avg_amount)}</div>
          <div class="bal-ledger">≈ ${fmt(r.monthly_cost)}/mo</div>
        </div>
        ${r.manual
          ? `<button class="btn-danger" title="Delete this subscription" onclick="deleteSubscription(${r.manual_id})">✕</button>`
          : `<button class="btn-secondary btn-small" title="Keep in list but exclude from totals" onclick="toggleIgnore('${encodeURIComponent(r.merchant_key)}', ${!r.ignored})">${r.ignored ? "Unignore" : "Ignore"}</button>
             <button class="btn-danger" title="Not actually recurring — remove from this list" onclick="dismissRecurring('${encodeURIComponent(r.merchant_key)}')">✕</button>`}
      </div>
    </div>`).join("");
}

async function toggleIgnore(encodedKey, ignored) {
  await api("/api/recurring/ignore", { method: "POST", body: JSON.stringify({ merchant_key: decodeURIComponent(encodedKey), ignored }) });
  await loadRecurring();
  renderDashboard();
}

async function dismissRecurring(encodedKey) {
  await api("/api/recurring/dismiss", { method: "POST", body: JSON.stringify({ merchant_key: decodeURIComponent(encodedKey) }) });
  toast("Removed — it won't be detected again", "ok");
  await loadRecurring();
  renderDashboard();
}

async function restoreDismissed() {
  await api("/api/recurring/dismiss", { method: "POST", body: JSON.stringify({ restore_all: true }) });
  toast("Restored all removed charges", "ok");
  await loadRecurring();
  renderDashboard();
}

async function deleteSubscription(id) {
  await api(`/api/subscriptions/${id}`, { method: "DELETE" });
  toast("Subscription deleted", "ok");
  await loadRecurring();
  renderDashboard();
}

$("form-subscription").addEventListener("submit", async e => {
  e.preventDefault();
  const fd = new FormData(e.target);
  await api("/api/subscriptions", {
    method: "POST",
    body: JSON.stringify({
      name: fd.get("name"),
      amount: parseFloat(fd.get("amount")),
      cadence: fd.get("cadence"),
      next_date: fd.get("next_date") || null,
    }),
  });
  e.target.reset();
  toast("Subscription added", "ok");
  await loadRecurring();
  renderDashboard();
});

/* ── Net Worth page ─────────────────────────────────────────────────────── */

function renderNetWorth() {
  const { totalAssets, totalLiabilities, netWorth } = computeTotals();
  $("nw-total").textContent = fmt(netWorth);
  $("nw-assets").textContent = fmt(totalAssets);
  $("nw-liabilities").textContent = fmt(totalLiabilities);

  // 30-day change from snapshot history
  const h = state.history;
  const cutoff = new Date(); cutoff.setDate(cutoff.getDate() - 30);
  const past = h.filter(s => new Date(s.snapped_at + "T00:00:00") <= cutoff).pop() || h[0];
  if (past && h.length > 1) {
    const diff = netWorth - past.net_worth;
    $("nw-change").textContent = (diff >= 0 ? "+" : "") + fmt(diff);
    $("nw-change").className = "stat-value " + (diff >= 0 ? "green" : "red");
  } else {
    $("nw-change").textContent = "—";
  }

  buildNetWorthChart(h);

  const assetsList = $("manual-assets-list");
  const assets = state.manual.filter(m => m.entry_type === "asset");
  assetsList.innerHTML = assets.length ? assets.map(m => `
    <div class="entry-row">
      <div>
        <div class="entry-label">${esc(m.label)}</div>
        <div style="font-size:.72rem;color:var(--muted)">${esc(m.category || "")}</div>
      </div>
      <div style="display:flex;align-items:center;gap:.6rem">
        <span class="entry-amount green">${fmt(m.amount)}</span>
        <button class="btn-danger" onclick="deleteManual(${m.id})">✕</button>
      </div>
    </div>`).join("") : `<p class="empty">None added.</p>`;

  const liabList = $("manual-liabilities-list");
  const liabs = state.manual.filter(m => m.entry_type === "liability");
  liabList.innerHTML = liabs.length ? liabs.map(m => `
    <div class="entry-row">
      <div>
        <div class="entry-label">${esc(m.label)}</div>
        <div style="font-size:.72rem;color:var(--muted)">${esc(m.category || "")}</div>
      </div>
      <div style="display:flex;align-items:center;gap:.6rem">
        <span class="entry-amount red">-${fmt(m.amount)}</span>
        <button class="btn-danger" onclick="deleteManual(${m.id})">✕</button>
      </div>
    </div>`).join("") : `<p class="empty">None added.</p>`;
}

async function deleteManual(id) {
  await api(`/api/manual/${id}`, { method: "DELETE" });
  await loadAll();
  renderNetWorth();
}

/* ── Alerts page ────────────────────────────────────────────────────────── */

const NOTIF_RULES = [
  ["payday", "Payday reminders (day before + day of)"],
  ["paycheck", "Paycheck landed (with commission callout)"],
  ["budget", "Budget warnings (80% and over-limit)"],
  ["large_txn", "Large charges"],
  ["price_hike", "Subscription price increases"],
  ["bill_due", "Bills due today/tomorrow"],
  ["goal", "Savings goal reached"],
  ["milestone", "Net worth milestones ($5k steps)"],
  ["low_balance", "Low checking balance"],
  ["sync", "Bank sync failures"],
];

async function loadAlerts() {
  const [settings, feed] = await Promise.all([
    api("/api/notifications/settings"),
    api("/api/notifications"),
  ]);

  $("notif-settings").innerHTML = NOTIF_RULES.map(([key, label]) => `
    <label class="toggle notif-toggle">
      <input type="checkbox" data-rule="${key}" ${settings[key] ? "checked" : ""} /> ${label}
    </label>`).join("");
  $("nt-large").value = settings.large_txn_threshold;
  $("nt-lowbal").value = settings.low_balance_threshold;

  const list = $("alerts-feed");
  list.innerHTML = feed.length ? feed.map(n => `
    <div class="alert-row ${n.sent ? "" : "alert-muted"}">
      <div>
        <div class="entry-label">${esc(n.title)}</div>
        <div class="alert-body">${esc(n.body)}</div>
      </div>
      <div class="alert-meta">
        ${n.sent ? "" : `<span class="badge badge-other" title="Logged during initial seeding or while capped — not shown as a popup">silent</span>`}
        <span class="mini-sub">${new Date(n.created_at + "Z").toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span>
      </div>
    </div>`).join("") : `<p class="empty">No alerts yet — they'll show up here as syncs detect things worth telling you about.</p>`;
}

$("btn-save-notif").addEventListener("click", async () => {
  const body = { large_txn_threshold: parseFloat($("nt-large").value) || 250,
                 low_balance_threshold: parseFloat($("nt-lowbal").value) || 500 };
  document.querySelectorAll("#notif-settings input[data-rule]").forEach(cb => body[cb.dataset.rule] = cb.checked);
  await api("/api/notifications/settings", { method: "PUT", body: JSON.stringify(body) });
  toast("Notification settings saved", "ok");
});

$("btn-test-notif").addEventListener("click", async () => {
  const res = await api("/api/notifications/test", { method: "POST" });
  if (res.ok) toast("Test notification sent — check your screen", "ok");
  else toast("Notification failed: " + (res.error || "unknown"), "error");
});

/* ── Load all data ──────────────────────────────────────────────────────── */

async function loadCategories() {
  state.categories = await api("/api/categories");
  $("category-list").innerHTML = state.categories.map(c => `<option value="${esc(c)}">`).join("");
  const sel = $("txn-category-filter");
  const cur = sel.value;
  sel.innerHTML = `<option value="">All categories</option><option value="uncategorized">uncategorized</option>` +
    state.categories.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
  sel.value = cur;
}

async function loadAll() {
  const [accounts, transactions, manual, history, summary, monthly, merchants, recurring, income] = await Promise.all([
    api("/api/accounts"),
    api("/api/transactions?limit=500"),
    api("/api/manual"),
    api("/api/networth/history"),
    api("/api/insights/summary"),
    api("/api/insights/monthly?months=12"),
    api("/api/insights/merchants"),
    api("/api/recurring"),
    api("/api/income/summary"),
  ]);

  state = { ...state, accounts, transactions, manual, history, summary, monthly, merchants, income,
            recurring: recurring.items, recurringDismissed: recurring.dismissed_count };

  // Populate account filter dropdown
  const sel = $("txn-account-filter");
  const cur = sel.value;
  sel.innerHTML = `<option value="">All accounts</option>` +
    accounts.map(a => `<option value="${esc(a.id)}">${esc(a.institution_name)} – ${esc(a.name)}${a.hidden ? " (hidden)" : ""}</option>`).join("");
  sel.value = cur;

  await loadCategories();

  renderDashboard();
  renderAccounts();
  renderIncome();
  await loadTransactions();
}

/* ── Refresh button ─────────────────────────────────────────────────────── */

function showLastRefresh(iso) {
  $("last-refresh").textContent = iso ? "Synced " + new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
}

$("btn-refresh").addEventListener("click", async () => {
  $("btn-refresh").textContent = "Refreshing…";
  $("btn-refresh").disabled = true;
  try {
    const res = await api("/api/refresh", { method: "POST" });
    if (res.errors?.length) toast("Refreshed with errors: " + res.errors[0], "error");
    else toast("Data refreshed", "ok");
    await loadAll();
    const status = await api("/api/status");
    showLastRefresh(status.last_refresh);
  } catch (e) {
    toast("Refresh failed: " + e.message, "error");
  } finally {
    $("btn-refresh").textContent = "↺ Refresh Data";
    $("btn-refresh").disabled = false;
  }
});

/* ── Manual entry forms ─────────────────────────────────────────────────── */

function handleManualForm(formId, entryType) {
  $(formId).addEventListener("submit", async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    await api("/api/manual", {
      method: "POST",
      body: JSON.stringify({
        label: fd.get("label"),
        amount: parseFloat(fd.get("amount")),
        entry_type: entryType,
        category: fd.get("category"),
      }),
    });
    e.target.reset();
    await loadAll();
    renderNetWorth();
    toast(`${entryType === "asset" ? "Asset" : "Liability"} added`, "ok");
  });
}
handleManualForm("form-asset", "asset");
handleManualForm("form-liability", "liability");

/* ── Search / filter ────────────────────────────────────────────────────── */

$("txn-search").addEventListener("input", debounce(loadTransactions, 250));
["txn-account-filter", "txn-category-filter", "txn-start", "txn-end", "txn-hide-transfers"].forEach(id =>
  $(id).addEventListener("change", loadTransactions)
);

/* ── Init ───────────────────────────────────────────────────────────────── */

(async () => {
  const status = await api("/api/status");
  showLastRefresh(status.last_refresh);
  showSimplefin(status.simplefin);
  await loadAll();
})();
