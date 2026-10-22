"use strict";

const $ = (id) => document.getElementById(id);
const api = (path) => path; // same-origin (served by FastAPI)

let lastResult = null;   // reconciliation result object
let lastVendor = "";
let lastVendorName = "";
let activeTab = "matched";

let authToken = localStorage.getItem("vr_token") || null;
let currentUser = null;
let lastSapItems = null; // last SAP open-items response (for Export)

const PAGES = ["home", "vendorData", "reconciliation", "txnRecon", "engine", "history", "reconDetail", "template", "settings"];
let engResult = null;   // last engine result
let histDetail = null;      // currently opened run payload
let histDetailTab = "matched";
let lastVendorData = null;  // last multi-vendor Data Upload response

// Highlight the active navbar tab (maps every page to its nearest tab).
function syncNavTabs(page) {
  const tabFor = { home: "home", vendorData: "vendorData", history: "history", reconDetail: "reconDetail", settings: "settings" };
  const active = tabFor[page] || "";
  document.querySelectorAll(".nav-tab").forEach(t =>
    t.classList.toggle("active", t.dataset.nav === active));
}

// Landing page with the cards.
function showHome() {
  PAGES.forEach(p => {
    const sec = $("page-" + p);
    if (sec) sec.classList.toggle("hidden", p !== "home");
  });
  syncNavTabs("home");
}

function showPage(page) {
  PAGES.forEach(p => {
    const sec = $("page-" + p);
    if (sec) sec.classList.toggle("hidden", p !== page);
  });
  syncNavTabs(page);

  // When opening the Reconciliation Summary tab, if no full reconcile has run
  // yet but SAP items were previewed, show a SAP-items summary instead of the
  // empty prompt.
  if (page === "reconciliation" && !lastResult) {
    renderSapOnlySummary();
  }
  if (page === "vendorData") {
    initVendorRows();
    populateFinYears();
  }
  if (page === "history") {
    loadHistory();
  }
  if (page === "reconDetail") {
    // If no run has been opened yet, show the prompt instead of a blank card.
    const hasDetail = !!histDetail;
    $("histDetailSection").classList.toggle("hidden", !hasDetail);
    const empty = $("reconDetailEmpty");
    if (empty) empty.classList.toggle("hidden", hasDetail);
  }
  if (page === "settings") {
    loadSettings();
  }
}

// Show a summary of the previewed SAP open items (before a statement is uploaded).
function renderSapOnlySummary() {
  const empty = $("reconEmpty");
  if (!lastSapItems || !(lastSapItems.invoices || []).length) {
    // keep the default "no reconciliation yet" prompt
    return;
  }
  const d = lastSapItems;
  const totals = {};
  (d.invoices || []).forEach(inv => {
    const c = inv.currency || "";
    totals[c] = (totals[c] || 0) + Number(inv.amount || 0);
  });
  const totalLabel = Object.entries(totals)
    .map(([c, v]) => `${c ? c + " " : ""}${fmtMoney(v)}`).join(" · ");

  empty.innerHTML = `
    <div class="summary-head"><h2>SAP open items summary</h2></div>
    <p class="vendor-line">Vendor ${escapeHtml(d.vendor)}${d.vendor_name ? " — " + escapeHtml(d.vendor_name) : ""} · Company: ${escapeHtml(d.company_code || "")}</p>
    <div class="stat-grid">
      <div class="stat blue"><div class="label">Open Items</div><div class="value">${d.count}</div></div>
      <div class="stat blue"><div class="label">Total Amount</div><div class="value">${escapeHtml(totalLabel)}</div></div>
    </div>
    <p class="tpl-note">This is the SAP side only. Upload the vendor statement on the
      <strong>Vendor Data</strong> tab and click <strong>Reconcile</strong> to compare the two sides.</p>`;
  empty.classList.remove("hidden");
}

function authHeaders() {
  return authToken ? { Authorization: "Bearer " + authToken } : {};
}

// ---- login / logout --------------------------------------------------------
function toggleLoginPwd() {
  const inp = $("login-password");
  inp.type = inp.type === "password" ? "text" : "password";
}

async function doLogin() {
  const username = $("login-username").value.trim();
  const password = $("login-password").value.trim();
  const err = $("login-error");
  err.style.display = "none";
  if (!username || !password) {
    err.textContent = "Enter your username and password.";
    err.style.display = "block";
    return;
  }
  try {
    const r = await fetch(api("/api/auth/login"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || "Invalid username or password. Please try again.");
    authToken = d.token;
    currentUser = d.user;
    localStorage.setItem("vr_token", authToken);
    showApp();
  } catch (e) {
    err.textContent = e.message || "Login failed";
    err.style.display = "block";
  }
}

function doLogout() {
  authToken = null;
  currentUser = null;
  localStorage.removeItem("vr_token");
  $("app-wrapper").classList.add("hidden");
  $("login-page").style.display = "flex";
  $("login-password").value = "";
}

function showApp() {
  $("login-page").style.display = "none";
  $("app-wrapper").classList.remove("hidden");
  const name = (currentUser && (currentUser.full_name || currentUser.username)) || "User";
  $("user-name").textContent = "Hi, " + name;
  $("user-avatar").textContent = (name[0] || "U").toUpperCase();
  loadHealth();
  showHome();   // land on the 4-card home page
}

// Try to resume a session from a stored token.
async function resumeSession() {
  if (!authToken) return;
  try {
    const r = await fetch(api("/api/auth/me"), { headers: authHeaders() });
    if (!r.ok) throw new Error("expired");
    currentUser = await r.json();
    showApp();
  } catch {
    doLogout();
  }
}

// ---- helpers ---------------------------------------------------------------
function fmtMoney(v) {
  const n = Number(v || 0);
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// "UNALLOCATED PAYMENT" -> "Unallocated Payment"
function toTitle(s) {
  return String(s || "").toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
}

// Write a status line to the message element inside the currently visible page
// (both the Data Upload and Reconciliation pages have a .message element).
function setMessage(text, kind) {
  document.querySelectorAll(".page:not(.hidden) .message").forEach(el => {
    el.textContent = text || "";
    el.className = "message" + (kind ? " " + kind : "");
  });
}

function busy(on) {
  const p = $("previewBtn");
  const r = $("reconcileBtn");
  if (p) p.disabled = on;
  if (r) r.disabled = on;
}

// ---- SAP status ------------------------------------------------------------
async function loadHealth() {
  try {
    const r = await fetch(api("/api/health"));
    const d = await r.json();
    const badge = $("sapStatus");
    if (d.sap_configured) {
      badge.textContent = "SAP connected (" + d.default_company_code + ")";
      badge.className = "badge badge-ok";
    } else {
      badge.textContent = "SAP not configured";
      badge.className = "badge badge-bad";
    }
  } catch {
    $("sapStatus").textContent = "Backend offline";
    $("sapStatus").className = "badge badge-bad";
  }
}

// ---- preview SAP open items ------------------------------------------------
async function previewSap() {
  const vendor = $("vendor").value.trim();
  if (!vendor) { setMessage("Enter a vendor code first.", "error"); return; }
  busy(true);
  setMessage("Fetching SAP open items…");
  try {
    const params = new URLSearchParams({ vendor, company_code: $("companyCode2").value.trim().toUpperCase() });
    const r = await fetch(api("/api/sap/open-items?" + params.toString()), { headers: authHeaders() });
    if (r.status === 401) return doLogout();
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "SAP request failed");
    setMessage(`SAP returned ${d.count} open item(s) for ${d.vendor_name || vendor}.`, "ok");
    renderSapItems(d);
  } catch (e) {
    setMessage(e.message, "error");
  } finally {
    busy(false);
  }
}

// ---- render SAP open items table -------------------------------------------
// Columns: Vendor Code | Vendor Name | Company Code | PO Number | Invoice No.
//          | Invoice Date | Due Date | Currency | Amount
function renderSapItems(d) {
  lastSapItems = d;
  const host = $("sapItemsHost");
  const section = $("sapItemsSection");
  const invoices = d.invoices || [];

  $("sapItemsTitle").textContent =
    `SAP open items — ${d.vendor}${d.vendor_name ? " · " + d.vendor_name : ""} (${d.count})`;

  if (!invoices.length) {
    host.innerHTML = `<div class="empty">No open items returned by SAP for this vendor.</div>`;
    section.classList.remove("hidden");
    return;
  }

  let totalsByCcy = {};
  const rows = invoices.map(inv => {
    const ccy = inv.currency || "";
    totalsByCcy[ccy] = (totalsByCcy[ccy] || 0) + Number(inv.amount || 0);
    return `<tr>
      <td>${escapeHtml(inv.vendor || d.vendor || "")}</td>
      <td>${escapeHtml(d.vendor_name || "")}</td>
      <td>${escapeHtml(inv.company_code || d.company_code || "")}</td>
      <td>${escapeHtml(inv.po_number || "")}</td>
      <td>${escapeHtml(inv.invoice_no || "")}</td>
      <td>${escapeHtml(inv.invoice_date || "")}</td>
      <td>${escapeHtml(inv.due_date || "")}</td>
      <td>${escapeHtml(ccy)}</td>
      <td class="num">${fmtMoney(inv.amount)}</td>
    </tr>`;
  }).join("");

  const totalLabel = Object.entries(totalsByCcy)
    .map(([c, v]) => `${c ? c + " " : ""}${fmtMoney(v)}`).join(" · ");

  host.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Vendor Code</th><th>Vendor Name</th><th>Company Code</th>
          <th>PO Number</th><th>Invoice No.</th><th>Invoice Date</th>
          <th>Due Date</th><th>Currency</th><th class="num">Amount</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
      <tfoot>
        <tr>
          <td colspan="8" class="num" style="font-weight:700">Total (${invoices.length} items)</td>
          <td class="num" style="font-weight:700">${escapeHtml(totalLabel)}</td>
        </tr>
      </tfoot>
    </table>`;
  section.classList.remove("hidden");
}

// ---- Data Upload: fetch open items for multiple vendor codes ---------------
// Map a SAP document type (BLART) to a readable transaction type.
const DOC_TYPE_LABELS = {
  KR: "Invoice", RE: "Invoice", MA: "Invoice", MI: "Invoice", KA: "Invoice",
  KG: "Credit Memo",
  KZ: "Payment", PE: "Payment", ZP: "Payment", DZ: "Payment",
  AB: "Clearing", SA: "Adjustment",
  OP: "Opening Balance", CL: "Closing Balance",
};
function txnTypeFor(docType) {
  const code = (docType || "").trim().toUpperCase();
  return DOC_TYPE_LABELS[code] || (code ? code : "—");
}

const DATA_COLUMNS = [
  ["document_type", "Document Type", false],
  ["transaction_type", "Transaction Type", false],
  ["vendor_code", "Supplier", false],
  ["document_number", "Document Number", false],
  ["assignment", "Assignment", false],
  ["item_text", "Text", false],
  ["clearing_document", "Clearing Document", false],
  ["reference", "Reference", false],
  ["business_area", "Business Area", false],
  ["document_date", "Document Date", false],
  ["posting_date", "Posting Date", false],
  ["amount_doc_curr", "Amount in Doc. Curr.", true],
  ["amount_local_curr", "Amount in Local Currency", true],
  ["local_currency", "Local Currency", false],
  ["payment_block", "Payment block", false],
  ["withholding_tax_amt", "Withholding Tax Amt", true],
];

function dataBusy(on) {
  const b = $("fetchDataBtn");
  if (b) b.disabled = on;
}

// ---- editable vendor-code table -------------------------------------------
const MULTI_ROWS = 12;          // rows shown in "Multiple" mode
let vendorMode = "single";      // "single" | "multiple"
let sapSource = "fetch";        // "fetch" (live SAP) | "upload" (manual Excel)

// Toggle between live SAP fetch and manual SAP Excel upload.
function setSapSource(mode) {
  sapSource = mode;
  $("sapFetchBox").classList.toggle("hidden", mode !== "fetch");
  $("sapUploadBox").classList.toggle("hidden", mode !== "upload");
  // Hide any previously shown data/vendor sections until new data is loaded.
  $("vendorSummarySection").classList.add("hidden");
  $("dataTableSection").classList.add("hidden");
  $("vendorStmtSection").classList.add("hidden");
  $("exportDataBtn").classList.add("hidden");
  lastVendorData = null;
}

// Upload a SAP Excel/CSV and show its rows in the 15-column table.
async function loadSapFile() {
  const f = $("sapUploadFile").files[0];
  const msg = $("sapUploadMsg");
  const companyCodeUp = $("companyCodeUp").value.trim().toUpperCase();
  const vendorNameUp2 = $("vendorNameUp2").value.trim();
  if (!f) { msg.textContent = "Choose a SAP data file first."; msg.className = "message error"; return; }
  if (!companyCodeUp) { msg.textContent = "Company code is required."; msg.className = "message error"; $("companyCodeUp").focus(); return; }
  if (!vendorNameUp2) { msg.textContent = "Vendor name is required."; msg.className = "message error"; $("vendorNameUp2").focus(); return; }
  $("loadSapBtn").disabled = true;
  msg.textContent = "Parsing SAP file…"; msg.className = "message";
  try {
    const fd = new FormData();
    fd.append("sap_file", f);
    fd.append("company_code", companyCodeUp);
    fd.append("vendor_name", vendorNameUp2);
    fd.append("fin_year", ($("finYear") && $("finYear").value) || "");
    const r = await fetch(api("/api/sap/upload-preview"), { method: "POST", headers: authHeaders(), body: fd });
    if (r.status === 401) return doLogout();
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Could not parse the SAP file");

    if (!d.total_count) {
      const cols = (d.columns_detected || []).join(", ");
      msg.innerHTML = `No data rows found in the file (read ${d.raw_row_count || 0} row(s)). ` +
        `Detected columns: <em>${escapeHtml(cols || "none")}</em>. ` +
        `Make sure the sheet has data rows and headers like Document Type, Supplier, Document Number, Amount in Doc. Curr.`;
      msg.className = "message error";
      $("dataTableSection").classList.add("hidden");
      $("vendorStmtSection").classList.add("hidden");
      $("exportDataBtn").classList.add("hidden");
      return;
    }

    (d.items || []).forEach(it => { it.transaction_type = txnTypeFor(it.document_type); });
    d.requested = [companyCodeUp || "manual"];
    d.company_code = companyCodeUp;
    d.entered_vendor_name = vendorNameUp2;
    lastVendorData = d;
    const reqId = d.request_id ? ` · Request ID ${d.request_id}` : "";
    msg.textContent = `Loaded ${d.total_count} SAP row(s) from file${reqId}.`; msg.className = "message ok";

    // Show the Reconciliation Request ID banner (same style as the live fetch).
    const sumHost = $("vendorSummaryHost");
    if (sumHost && d.request_id) {
      sumHost.innerHTML = `
        <div class="reqid-banner">
          <div>
            <div class="reqid-banner-label">Reconciliation Request ID</div>
            <div class="reqid-banner-id">${escapeHtml(d.request_id)}</div>
          </div>
          <div class="reqid-banner-meta">
            <div><span>Company</span> ${escapeHtml(d.company_code || "")}</div>
            <div><span>Vendor name</span> ${escapeHtml(d.entered_vendor_name || "")}</div>
            <div><span>Rows</span> ${d.total_count}</div>
          </div>
        </div>`;
      $("vendorSummarySection").classList.remove("hidden");
    } else {
      $("vendorSummarySection").classList.add("hidden");
    }

    // Populate the table (reuse the same renderer) and reveal vendor upload.
    renderVendorData(d);
    $("dataTableTitle").textContent = `SAP data — uploaded (${d.total_count})`;
    $("exportDataBtn").classList.toggle("hidden", d.total_count === 0);
    $("vendorStmtSection").classList.remove("hidden");
  } catch (e) {
    msg.textContent = e.message; msg.className = "message error";
  } finally {
    $("loadSapBtn").disabled = false;
  }
}

// Run the engine reconciliation (uploaded SAP file or live SAP) against the
// uploaded vendor statement, then route to Reconciliation History.
async function runReconciliation() {
  const venFile = $("vendorStmtFile").files[0];
  const msg = $("reconMsg");
  if (!venFile) { msg.textContent = "Upload the vendor statement first."; msg.className = "message error"; return; }
  $("reconcileNowBtn").disabled = true;
  msg.textContent = "Reconciling…"; msg.className = "message";
  try {
    if (!lastVendorData || !(lastVendorData.items || []).length) {
      throw new Error("Load or fetch SAP data first — nothing to reconcile.");
    }
    // Reconcile the EXACT SAP data shown on screen (All_items or open-items),
    // so the Assignment/Reference you see is what the engine matches on.
    const fd = new FormData();
    fd.append("vendor_file", venFile);
    fd.append("sap_items", JSON.stringify(lastVendorData.items));
    fd.append("vendor", (lastVendorData.requested || []).join(",") || collectVendorCodes().join(","));
    fd.append("company_code", (lastVendorData.company_code || $("companyCode").value || "").trim().toUpperCase());
    const names = Array.from(new Set((lastVendorData.per_vendor || [])
      .map(v => (v.vendor_name || "").trim()).filter(Boolean)));
    // Prefer the user-entered (compulsory) vendor name; fall back to SAP names.
    const enteredName = (lastVendorData.entered_vendor_name || $("vendorNameUp").value || "").trim();
    fd.append("vendor_name", enteredName || names.join(" / "));
    fd.append("request_id", lastVendorData.request_id || "");
    // Pass the reconciliation period so out-of-period open items can be flagged.
    fd.append("date_from", (lastVendorData.date_from || $("dateFrom").value || ""));
    fd.append("date_to", (lastVendorData.date_to || $("dateTo").value || ""));
    const r = await fetch(api("/api/engine/reconcile-items"), { method: "POST", headers: authHeaders(), body: fd });
    if (r.status === 401) return doLogout();
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Reconciliation failed");

    msg.textContent = `Reconciliation complete (run #${d.history_id ?? "—"}). Opening history…`;
    msg.className = "message ok";
    setTimeout(() => showPage("history"), 700);
  } catch (e) {
    msg.textContent = e.message; msg.className = "message error";
  } finally {
    $("reconcileNowBtn").disabled = false;
  }
}

// Render the vendor-code input for the current mode into #vendorInputHost.
// Single  -> one normal input.
// Multiple-> SAP-style "Single value" grid: a narrow column of 12 small cells.
function setVendorMode(mode, keepValues) {
  vendorMode = mode;
  document.querySelectorAll(".vmode-tab").forEach(t =>
    t.classList.toggle("active", t.dataset.vmode === mode));

  const host = $("vendorInputHost");
  const existing = keepValues || collectVendorCodes();
  const cell = (i, v) =>
    `<div class="svx-row">
       <div class="svx-num">${i + 1}</div>
       <input class="vrow-code" type="text" maxlength="10" inputmode="numeric"
              placeholder="e.g. 104741" value="${v ? escapeHtml(v) : ""}" autocomplete="off" />
     </div>`;

  if (mode === "multiple") {
    let cells = "";
    for (let i = 0; i < MULTI_ROWS; i++) cells += cell(i, existing[i] || "");
    host.innerHTML = `
      <div class="svx-grid">
        <div class="svx-head">Single value</div>
        <div class="svx-body">${cells}</div>
      </div>`;
    $("vmodeHint").innerHTML =
      "Enter up to <strong>12</strong> vendor codes. Paste a whole column from Excel into the first cell.";
  } else {
    host.innerHTML = `
      <div class="svx-single">
        <input class="vrow-code" type="text" maxlength="10" inputmode="numeric"
               placeholder="e.g. 104741" value="${existing[0] ? escapeHtml(existing[0]) : ""}" autocomplete="off" />
      </div>`;
    $("vmodeHint").innerHTML =
      "Single vendor code. Switch to <strong>Multiple</strong> to enter up to 12 codes (paste a column from Excel).";
  }

  // Wire paste-to-split on every cell.
  host.querySelectorAll(".vrow-code").forEach(inp =>
    inp.addEventListener("paste", onVendorPaste));
  const first = host.querySelector(".vrow-code");
  if (first) first.focus();
}

// Distribute a pasted multi-line / delimited list across consecutive cells.
function onVendorPaste(e) {
  const text = (e.clipboardData || window.clipboardData).getData("text") || "";
  const parts = text.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean);
  if (parts.length <= 1) return;   // single value -> let the browser handle it
  e.preventDefault();

  const inputs = Array.from($("vendorInputHost").querySelectorAll(".vrow-code"));
  let start = inputs.indexOf(e.target);
  if (start < 0) start = 0;

  // Single mode can only hold one value; a multi-paste switches to Multiple.
  if (vendorMode === "single" && parts.length > 1) {
    setVendorMode("multiple", parts.slice(0, MULTI_ROWS));
    return;
  }
  parts.forEach((code, i) => {
    const inp = inputs[start + i];
    if (inp) inp.value = code.slice(0, 10);   // extra codes beyond 12 are ignored
  });
}

function collectVendorCodes() {
  return Array.from($("vendorInputHost").querySelectorAll(".vrow-code"))
    .map(i => i.value.trim())
    .filter(Boolean);
}

// Render the default (single) input when the page first opens.
function initVendorRows() {
  const host = $("vendorInputHost");
  if (host && !host.querySelector(".vrow-code")) setVendorMode("single", []);
}

// ---- Financial year (Indian FY: 1 Apr – 31 Mar) ---------------------------
// Populate the FY dropdown with the current FY and a few prior years.
function populateFinYears() {
  const sel = $("finYear");
  if (!sel || sel.options.length > 1) return;   // already populated
  const now = new Date();
  // FY starting year: if we're in Jan–Mar, the current FY started last year.
  const startYear = now.getMonth() >= 3 ? now.getFullYear() : now.getFullYear() - 1;
  for (let y = startYear; y >= startYear - 6; y--) {
    const opt = document.createElement("option");
    opt.value = String(y);
    opt.textContent = `FY ${y}-${String((y + 1) % 100).padStart(2, "0")}`;  // e.g. FY 2025-26
    sel.appendChild(opt);
  }
}

// Selecting a financial year fills Date from/to with 1 Apr – 31 Mar.
function applyFinYear() {
  const y = $("finYear").value;
  if (!y) return;
  const start = Number(y);
  $("dateFrom").value = `${start}-04-01`;
  $("dateTo").value = `${start + 1}-03-31`;
}

// If the user edits the dates manually, clear the FY selection (custom range).
["dateFrom", "dateTo"].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener("input", () => { const s = $("finYear"); if (s) s.value = ""; });
});

async function fetchVendorData() {
  // Company code and Vendor name are required.
  const companyCode = $("companyCode").value.trim().toUpperCase();
  const vendorName = $("vendorNameUp").value.trim();
  if (!companyCode) {
    setMessage("Company code is required.", "error");
    $("companyCode").focus();
    return;
  }
  if (!vendorName) {
    setMessage("Vendor name is required.", "error");
    $("vendorNameUp").focus();
    return;
  }
  const codes = collectVendorCodes();
  if (!codes.length) { setMessage("Enter at least one vendor code.", "error"); return; }
  dataBusy(true);
  setMessage("Fetching SAP open items…");
  try {
    const from = $("dateFrom").value;
    const to = $("dateTo").value;
    const qp = {
      vendors: codes.join(","),
      company_code: companyCode,
    };
    // When a date range is given, SAP returns ALL transactions for the period
    // (date filtering happens server-side via IM_FROMDATE / IM_TODATE).
    if (from) qp.date_from = from;
    if (to) qp.date_to = to;
    const params = new URLSearchParams(qp);
    const r = await fetch(api("/api/sap/open-items-multi?" + params.toString()), { headers: authHeaders() });
    if (r.status === 401) return doLogout();
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "SAP request failed");

    // Derive a readable transaction type from the SAP document type.
    (d.items || []).forEach(it => { it.transaction_type = txnTypeFor(it.document_type); });

    // Carry the user-entered vendor name through to reconciliation.
    d.entered_vendor_name = vendorName;
    lastVendorData = d;
    const kind = d.source === "all_items"
      ? `all transactions ${d.date_from} to ${d.date_to}`
      : "open item(s)";
    setMessage(`Request ${d.request_id} — SAP returned ${d.total_count} ${kind} across ${d.requested.length} vendor code(s).`, "ok");
    renderVendorSummary(d);
    renderVendorData(d);
    $("exportDataBtn").classList.toggle("hidden", d.total_count === 0);
    // Reveal the vendor statement upload + Reconcile step.
    $("vendorStmtSection").classList.remove("hidden");
  } catch (e) {
    setMessage(e.message, "error");
  } finally {
    dataBusy(false);
  }
}

function renderVendorSummary(d) {
  const host = $("vendorSummaryHost");
  const section = $("vendorSummarySection");
  // Vendor name(s) across the batch (one party may span several codes).
  const names = Array.from(new Set((d.per_vendor || [])
    .map(v => (v.vendor_name || "").trim()).filter(Boolean)));
  const banner = `
    <div class="reqid-banner">
      <div>
        <div class="reqid-banner-label">Reconciliation Request ID</div>
        <div class="reqid-banner-id">${escapeHtml(d.request_id || "")}</div>
      </div>
      <div class="reqid-banner-meta">
        <div><span>Company</span> ${escapeHtml(d.company_code || "")}</div>
        <div><span>Vendor code(s)</span> ${escapeHtml((d.requested || []).join(", "))}</div>
        ${names.length ? `<div><span>Vendor name</span> ${escapeHtml(names.join(" / "))}</div>` : ""}
      </div>
    </div>`;
  const rows = (d.per_vendor || []).map(v => `
    <tr>
      <td>${escapeHtml(v.vendor)}</td>
      <td>${escapeHtml(v.vendor_name || "")}</td>
      <td class="num">${v.count}</td>
      <td>${v.error ? `<span style="color:var(--red)">${escapeHtml(v.error)}</span>` : (escapeHtml(v.message || "OK"))}</td>
    </tr>`).join("");
  host.innerHTML = banner + `
    <table>
      <thead><tr><th>Vendor Code</th><th>Vendor Name</th><th class="num">Open Items</th><th>Status</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  section.classList.remove("hidden");
}

function renderVendorData(d) {
  const host = $("dataTableHost");
  const section = $("dataTableSection");
  const items = d.items || [];

  $("dataTableTitle").textContent =
    `Open items — ${d.requested.join(", ")} (${d.total_count})`;

  if (!items.length) {
    host.innerHTML = `<div class="empty">No open items returned by SAP for the entered vendor code(s).</div>`;
    section.classList.remove("hidden");
    return;
  }

  const head = DATA_COLUMNS.map(([, label, num]) =>
    `<th class="${num ? "num" : ""}">${label}</th>`).join("");

  const body = items.map(row => {
    const tds = DATA_COLUMNS.map(([key, , num]) => {
      let val = row[key];
      if (num && typeof val === "number") val = fmtMoney(val);
      return `<td class="${num ? "num" : ""}">${escapeHtml(val == null ? "" : String(val))}</td>`;
    }).join("");
    return `<tr>${tds}</tr>`;
  }).join("");

  host.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  section.classList.remove("hidden");
}

// Export the Data Upload table to a CSV (client-side; no server round-trip).
function exportVendorData() {
  if (!lastVendorData || !(lastVendorData.items || []).length) return;
  const esc = (v) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const header = DATA_COLUMNS.map(([, label]) => esc(label)).join(",");
  const lines = lastVendorData.items.map(row =>
    DATA_COLUMNS.map(([key]) => esc(row[key])).join(","));
  const csv = [header, ...lines].join("\r\n");
  const blob = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "sap_open_items.csv";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ---- reconcile -------------------------------------------------------------
async function reconcile() {
  const vendor = $("vendor").value.trim();
  const file = $("statement").files[0];
  if (!vendor) { setMessage("Enter a vendor code.", "error"); return; }
  if (!file) { setMessage("Choose a vendor statement file.", "error"); return; }

  busy(true);
  setMessage("Reconciling against SAP…");
  try {
    const fd = new FormData();
    fd.append("vendor", vendor);
    fd.append("company_code", $("companyCode2").value.trim().toUpperCase());
    fd.append("statement", file);

    const r = await fetch(api("/api/reconcile"), { method: "POST", headers: authHeaders(), body: fd });
    if (r.status === 401) return doLogout();
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Reconciliation failed");

    lastResult = d.result;
    lastVendor = d.vendor;
    lastVendorName = d.vendor_name || "";
    setMessage("Reconciliation complete. Showing results.", "ok");
    renderSummary(d);
    renderWarnings(d.warnings);
    activeTab = "matched";
    syncTabs();
    renderTable();
    $("reconEmpty").classList.add("hidden");
    $("summarySection").classList.remove("hidden");
    $("tablesSection").classList.remove("hidden");
    showPage("reconciliation");   // jump to page 2
  } catch (e) {
    setMessage(e.message, "error");
  } finally {
    busy(false);
  }
}

function renderSummary(d) {
  const s = d.result.summary;
  $("vendorLine").textContent =
    `Vendor ${d.vendor}${lastVendorName ? " — " + lastVendorName : ""} · ` +
    `SAP items: ${d.sap_count} · Statement rows: ${d.statement_row_count} · ` +
    `Company: ${d.company_code}`;

  const stats = [
    { label: "SAP Total", value: fmtMoney(s.sap_total), cls: "blue" },
    { label: "Vendor Total", value: fmtMoney(s.vendor_total), cls: "blue" },
    { label: "Net Difference", value: fmtMoney(s.net_difference), cls: Math.abs(s.net_difference) < 0.01 ? "green" : "red" },
    { label: "Matched", value: s.matched_count, cls: "green" },
    { label: "Amount Mismatch", value: s.amount_mismatch_count, cls: "amber" },
    { label: "Invoice Missing in SAP", value: s.only_in_sap_count, cls: "red" },
    { label: "Missing in Vendor", value: s.only_in_vendor_count, cls: "red" },
  ];
  $("statGrid").innerHTML = stats.map(st =>
    `<div class="stat ${st.cls}"><div class="label">${st.label}</div><div class="value">${st.value}</div></div>`
  ).join("");
}

function renderWarnings(warnings) {
  const host = $("warnings");
  if (!warnings || !warnings.length) { host.innerHTML = ""; return; }
  host.innerHTML = warnings.map(w => `<div class="warn">${escapeHtml(w)}</div>`).join("");
}

// ---- tables ----------------------------------------------------------------
const TAB_COLUMNS = {
  matched: [
    ["invoice_no", "Invoice No.", false],
    ["sap_amount", "SAP Amount", true],
    ["vendor_amount", "Vendor Amount", true],
    ["difference", "Difference", true],
    ["currency", "Curr.", false],
    ["due_date", "Due Date", false],
  ],
  amount_mismatch: [
    ["invoice_no", "Invoice No.", false],
    ["sap_amount", "SAP Amount", true],
    ["vendor_amount", "Vendor Amount", true],
    ["difference", "Difference", true],
    ["currency", "Curr.", false],
  ],
  only_in_sap: [
    ["invoice_no", "Invoice No.", false],
    ["amount", "Amount", true],
    ["currency", "Curr.", false],
    ["invoice_date", "Invoice Date", false],
    ["document_number", "Doc No.", false],
    ["due_date", "Due Date", false],
  ],
  only_in_vendor: [
    ["invoice_no", "Invoice No.", false],
    ["amount", "Amount", true],
    ["currency", "Curr.", false],
    ["invoice_date", "Invoice Date", false],
  ],
};

function renderTable() {
  const host = $("tableHost");
  if (!lastResult) { host.innerHTML = ""; return; }
  const rows = lastResult[activeTab] || [];
  const cols = TAB_COLUMNS[activeTab];

  if (!rows.length) {
    host.innerHTML = `<div class="empty">No rows in this category.</div>`;
    return;
  }

  const head = cols.map(([, label, num]) =>
    `<th class="${num ? "num" : ""}">${label}</th>`).join("");

  const body = rows.map(row => {
    const tds = cols.map(([key, , num]) => {
      let val = row[key];
      let cls = num ? "num" : "";
      if (num && typeof val === "number") {
        if (key === "difference") cls += val > 0 ? " diff-pos" : (val < 0 ? " diff-neg" : "");
        val = fmtMoney(val);
      }
      return `<td class="${cls}">${escapeHtml(val == null ? "" : String(val))}</td>`;
    }).join("");
    return `<tr>${tds}</tr>`;
  }).join("");

  host.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function syncTabs() {
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.tab === activeTab));
}

// ---- export ----------------------------------------------------------------
async function exportExcel() {
  if (!lastResult) return;
  const fd = new FormData();
  fd.append("vendor", lastVendor);
  fd.append("vendor_name", lastVendorName);
  fd.append("result", JSON.stringify(lastResult));
  try {
    const r = await fetch(api("/api/reconcile/export"), { method: "POST", headers: authHeaders(), body: fd });
    if (r.status === 401) return doLogout();
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || "Export failed"); }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `reconciliation_${lastVendor || "vendor"}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    setMessage(e.message, "error");
  }
}

// ---- SAP open-items export (client-side CSV, the exact preview columns) -----
function exportSapItems() {
  if (!lastSapItems || !(lastSapItems.invoices || []).length) return;
  const d = lastSapItems;
  const cols = ["Vendor Code", "Vendor Name", "Company Code", "PO Number",
    "Invoice No.", "Invoice Date", "Due Date", "Currency", "Amount"];
  const esc = (v) => {
    const s = String(v == null ? "" : v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [cols.join(",")];
  (d.invoices || []).forEach(inv => {
    lines.push([
      inv.vendor || d.vendor || "", d.vendor_name || "", inv.company_code || d.company_code || "",
      inv.po_number || "", inv.invoice_no || "", inv.invoice_date || "",
      inv.due_date || "", inv.currency || "", inv.amount == null ? "" : inv.amount,
    ].map(esc).join(","));
  });
  const blob = new Blob([lines.join("\n")], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `sap_open_items_${d.vendor || "vendor"}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ---- template download -----------------------------------------------------
function downloadTemplate(fmt) {
  const a = document.createElement("a");
  a.href = api("/api/template/download?fmt=" + encodeURIComponent(fmt));
  a.download = "vendor_statement_template." + (fmt === "csv" ? "csv" : "xlsx");
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function downloadTxnTemplate(fmt) {
  const a = document.createElement("a");
  a.href = api("/api/txn-template/download?fmt=" + encodeURIComponent(fmt));
  a.download = "vendor_ledger_template." + (fmt === "csv" ? "csv" : "xlsx");
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ---- ENGINE reconciliation (SAP file vs vendor file) -----------------------
function statusPillClass(status) {
  const s = (status || "").toUpperCase();
  if (s.includes("AUTO MATCHED") || s.includes("PAYMENT MATCHED") || s === "FULLY MATCHED") return "st-matched";
  if (s.includes("POSSIBLE")) return "st-possible";
  if (s.includes("DUPLICATE")) return "st-dup";
  if (s.includes("PARTIAL")) return "st-partial";
  if (s.includes("MISSING") || s.includes("NOT REFLECTED") || s.includes("UNALLOCATED") || s.includes("EXCEPTION")) return "st-missing";
  return "st-other";
}

function engSapSource() {
  const el = document.querySelector('input[name="engSapSrc"]:checked');
  return el ? el.value : "live";
}

function engToggleSource() {
  const live = engSapSource() === "live";
  $("engVendorCodeWrap").classList.toggle("hidden", !live);
  $("engCompanyWrap").classList.toggle("hidden", !live);
  $("engSapFileWrap").classList.toggle("hidden", live);
}

async function runEngine() {
  const venFile = $("engVendorFile").files[0];
  if (!venFile) { setEngMsg("Choose a vendor statement file.", "error"); return; }

  const live = engSapSource() === "live";
  let url, fd = new FormData();
  fd.append("vendor_file", venFile);

  if (live) {
    const vendor = $("engVendorCode").value.trim();
    if (!vendor) { setEngMsg("Enter the SAP vendor code.", "error"); return; }
    fd.append("vendor", vendor);
    fd.append("company_code", $("engCompanyCode").value.trim().toUpperCase());
    url = "/api/engine/reconcile-live";
  } else {
    const sapFile = $("engSapFile").files[0];
    if (!sapFile) { setEngMsg("Choose a SAP ledger file (or switch to Fetch from SAP).", "error"); return; }
    fd.append("sap_file", sapFile);
    url = "/api/engine/reconcile";
  }

  $("engRunBtn").disabled = true;
  setEngMsg(live ? "Fetching SAP open items and reconciling…" : "Running reconciliation engine…");
  try {
    const r = await fetch(api(url), { method: "POST", headers: authHeaders(), body: fd });
    if (r.status === 401) return doLogout();
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Engine run failed");
    engResult = d;
    let msg = `Done. ${d.sap_row_count} SAP rows and ${d.vendor_row_count} vendor rows.`;
    setEngMsg(msg, "ok");
    renderEngineResult(d);
  } catch (e) {
    setEngMsg(e.message, "error");
  } finally {
    $("engRunBtn").disabled = false;
  }
}

function setEngMsg(text, kind) {
  const el = $("engMessage");
  el.textContent = text || "";
  el.className = "message" + (kind ? " " + kind : "");
}

function renderEngineResult(d) {
  const badge = $("engFinalStatus");
  badge.textContent = d.final_status || "";
  const fs = (d.final_status || "").toUpperCase();
  badge.className = "badge " + (fs.includes("FULLY") ? "badge-ok"
    : fs.includes("TOLERANCE") ? "badge-ok"
    : fs.includes("PARTIAL") ? "badge-txn" : "badge-bad");

  $("engVendorLine").textContent =
    `Vendor ${d.vendor_code || "—"}${d.vendor_name ? " · " + d.vendor_name : ""} · ` +
    `SAP rows: ${d.sap_row_count} · Vendor rows: ${d.vendor_row_count}` +
    (d.sap_source === "live_open_items" ? " · SAP: live open items" : "");

  const c = d.counts || {};
  const b = d.balance || {};
  const stats = [
    { label: "Total", value: c.total || 0, cls: "blue" },
    { label: "Matched", value: (c["AUTO MATCHED"] || 0) + (c["PAYMENT MATCHED"] || 0) + (c["FULLY MATCHED"] || 0), cls: "green" },
    { label: "Possible", value: (c["POSSIBLE MATCH"] || 0) + (c["POSSIBLE DUPLICATE"] || 0), cls: "amber" },
    { label: "Missing in SAP", value: c["MISSING IN SAP"] || 0, cls: "red" },
    { label: "Missing in Vendor", value: c["MISSING IN VENDOR STATEMENT"] || 0, cls: "red" },
    { label: "SAP Balance", value: fmtMoney(b.sap_closing_balance), cls: "blue" },
    { label: "Vendor Balance", value: fmtMoney(b.vendor_closing_balance), cls: "blue" },
    { label: "Balance Diff", value: fmtMoney(b.balance_difference), cls: Math.abs(b.balance_difference || 0) < 0.01 ? "green" : "red" },
  ];
  $("engStatGrid").innerHTML = stats.map(st =>
    `<div class="stat ${st.cls}"><div class="label">${st.label}</div><div class="value">${st.value}</div></div>`
  ).join("");
  $("engSummarySection").classList.remove("hidden");

  // status filter options
  const statuses = Array.from(new Set((d.results || []).map(x => x.match_status)));
  const sel = $("engStatusFilter");
  sel.innerHTML = `<option value="">All statuses (${(d.results || []).length})</option>` +
    statuses.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)} (${(d.results || []).filter(x => x.match_status === s).length})</option>`).join("");
  sel.onchange = () => renderEngineTable(sel.value);

  renderEngineTable("");
  $("engResultsSection").classList.remove("hidden");
}

function renderEngineTable(statusFilter) {
  if (!engResult) return;
  let rows = engResult.results || [];
  if (statusFilter) rows = rows.filter(r => r.match_status === statusFilter);
  $("engResultCount").textContent = `${rows.length} transaction(s)`;
  if (!rows.length) { $("engResultsHost").innerHTML = `<div class="empty">No transactions.</div>`; return; }
  const body = rows.map(r => `<tr>
    <td><span class="st-pill ${statusPillClass(r.match_status)}">${escapeHtml(r.match_status)}</span></td>
    <td>${escapeHtml(r.match_rule || "")}</td>
    <td class="num">${r.confidence_score != null ? r.confidence_score : ""}</td>
    <td>${escapeHtml(r.sap_doc_number || "")}</td>
    <td>${escapeHtml(r.vendor_statement_reference || "")}</td>
    <td class="num">${r.sap_amount != null ? fmtMoney(r.sap_amount) : ""}</td>
    <td class="num">${r.vendor_amount != null ? fmtMoney(r.vendor_amount) : ""}</td>
    <td class="num">${r.difference != null ? fmtMoney(r.difference) : ""}</td>
    <td>${escapeHtml(r.currency || "")}</td>
    <td>${escapeHtml(r.comments || "")}</td>
  </tr>`).join("");
  $("engResultsHost").innerHTML = `<table><thead><tr>
    <th>Status</th><th>Rule</th><th class="num">Conf.</th><th>SAP Doc</th><th>Vendor Ref</th>
    <th class="num">SAP Amt</th><th class="num">Vendor Amt</th><th class="num">Diff</th><th>Curr.</th><th>Comments</th>
  </tr></thead><tbody>${body}</tbody></table>`;
}

function downloadEngineTemplate(which, fmt) {
  const base = which === "sap" ? "/api/engine/sap-template/download" : "/api/engine/vendor-template/download";
  const fname = which === "sap"
    ? "sap_ledger_template." + (fmt === "csv" ? "csv" : "xlsx")
    : "vendor_statement_full_template." + (fmt === "csv" ? "csv" : "xlsx");
  const a = document.createElement("a");
  a.href = api(base + "?fmt=" + encodeURIComponent(fmt));
  a.download = fname;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ---- SETTINGS (engine config) ----------------------------------------------
let cfgData = null;

const CFG_TOLERANCE_FIELDS = [
  ["date_tolerance_days", "Date tolerance (days)"],
  ["amount_tolerance_abs", "Amount tolerance (abs)"],
  ["amount_tolerance_pct", "Amount tolerance (%)"],
  ["rounding_tolerance_abs", "Rounding tolerance (abs)"],
  ["bank_charge_tolerance_abs", "Bank charge tolerance (abs)"],
  ["bank_charge_tolerance_pct", "Bank charge tolerance (%)"],
  ["forex_tolerance_pct", "Forex tolerance (%)"],
  ["gst_tolerance_pct", "GST tolerance (%)"],
  ["tds_tolerance_pct", "TDS tolerance (%)"],
  ["closing_balance_tolerance_abs", "Closing balance tolerance (abs)"],
];
const CFG_THRESHOLD_FIELDS = [
  ["auto_match_min_confidence", "Auto-match min confidence"],
  ["review_min_confidence", "Review min confidence"],
  ["max_combination_size", "Max combination size"],
  ["max_combination_candidates", "Max combination candidates"],
];
const CFG_TOGGLE_FIELDS = [
  ["enable_rule_1_exact", "Rule 1 — exact invoice + amount"],
  ["enable_rule_2_invoice_amount", "Rule 2 — invoice + amount"],
  ["enable_rule_3_invoice_date_amount", "Rule 3 — invoice + date + amount"],
  ["enable_rule_4_vendor_amount_date", "Rule 4 — vendor + amount + date"],
  ["enable_rule_5_payment_reference", "Rule 5 — payment reference"],
  ["enable_one_to_many_payments", "Rule 7 — one invoice / many payments"],
  ["enable_many_to_one_invoices", "Rule 8 — one payment / many invoices"],
  ["enable_partial_payment", "Rule 6 — partial payment"],
  ["enable_tds_adjustment", "Rule 11 — TDS adjustment"],
  ["enable_bank_charges", "Rule 12 — bank charges"],
  ["enable_rounding", "Rule 13 — rounding difference"],
  ["enable_duplicate_detection", "Rule 14 — duplicate detection"],
  ["auto_approve_rule_2", "Auto-approve Rule 2"],
  ["auto_approve_rule_3", "Auto-approve Rule 3"],
  ["auto_approve_rule_4", "Auto-approve Rule 4"],
];

async function loadSettings() {
  setCfgMsg("Loading settings…");
  try {
    const r = await fetch(api("/api/engine/config"), { headers: authHeaders() });
    if (r.status === 401) return doLogout();
    cfgData = await r.json();
    renderSettings();
    setCfgMsg("");
  } catch (e) {
    setCfgMsg(e.message, "error");
  }
}

function setCfgMsg(text, kind) {
  const el = $("cfgMessage");
  el.textContent = text || "";
  el.className = "message" + (kind ? " " + kind : "");
}

const RULES_REFERENCE = [
  ["1", "Exact Invoice + Amount", "Vendor + normalised invoice + currency + exact amount + same date", "AUTO MATCHED", "100%"],
  ["2", "Invoice + Amount", "Vendor + invoice + amount; date may differ within tolerance", "AUTO MATCHED", "95%"],
  ["3", "Invoice + Date + Amount", "Invoice matches; date and amount within tolerance", "POSSIBLE MATCH", "90%"],
  ["4", "Vendor + Amount + Date", "No reliable invoice; matched on amount + date only", "POSSIBLE MATCH", "75%"],
  ["5", "Payment Reference", "Vendor + payment reference + currency + amount", "PAYMENT MATCHED", "100%"],
  ["6", "Partial Payment", "Payment less than the open invoice; outstanding computed", "PARTIAL PAYMENT", "85%"],
  ["7", "One Invoice / Many Payments", "Several vendor payments sum to one SAP invoice", "FULLY MATCHED", "95%"],
  ["8", "One Payment / Many Invoices", "One vendor payment covers several SAP invoices", "FULLY MATCHED", "95%"],
  ["9", "Credit Note", "Credit note on the statement, not in SAP", "CREDIT NOTE MISSING IN SAP", "—"],
  ["10", "Debit Note", "Debit note in SAP, not on the statement", "DEBIT NOTE MISSING IN VENDOR STATEMENT", "—"],
  ["11", "TDS Difference", "Net payment after TDS reconciles; TDS not treated as unpaid", "RECONCILED - TDS", "—"],
  ["12", "Bank Charges", "Payment/receipt gap within bank-charge tolerance", "BANK CHARGES", "—"],
  ["13", "Rounding Difference", "Amount gap within rounding tolerance", "ROUNDING DIFFERENCE", "—"],
  ["14", "Duplicate Detection", "More than one SAP row with same invoice + amount + currency", "POSSIBLE DUPLICATE", "60%"],
  ["15", "Invoice Missing in SAP", "Statement invoice with no SAP counterpart", "MISSING IN SAP", "—"],
  ["16", "Invoice Missing in Vendor Statement", "SAP open invoice not on the statement", "MISSING IN VENDOR STATEMENT", "—"],
  ["17", "Payment in SAP, not Vendor", "SAP payment not reflected on the statement (timing)", "PAYMENT NOT REFLECTED BY VENDOR", "—"],
  ["18", "Unallocated Payment", "Statement payment not allocated / matched to any SAP document", "UNALLOCATED PAYMENT", "—"],
];

function renderRulesReference() {
  const host = $("rulesRefHost");
  if (!host) return;
  const body = RULES_REFERENCE.map(([n, name, desc, status, conf]) => `<tr>
    <td class="num">${n}</td>
    <td><strong>${escapeHtml(name)}</strong></td>
    <td>${escapeHtml(desc)}</td>
    <td><span class="st-pill ${statusPillClass(status)}">${escapeHtml(status)}</span></td>
    <td class="num">${escapeHtml(conf)}</td>
  </tr>`).join("");
  host.innerHTML = `<table><thead><tr>
    <th class="num">#</th><th>Rule</th><th>What it matches</th><th>Result status</th><th class="num">Confidence</th>
  </tr></thead><tbody>${body}</tbody></table>`;
}

function renderSettings() {
  if (!cfgData) return;
  const numField = ([key, label]) =>
    `<label>${label}<input type="number" step="any" data-cfg="${key}" value="${cfgData[key] != null ? cfgData[key] : ""}" /></label>`;
  $("cfgTolerances").innerHTML = CFG_TOLERANCE_FIELDS.map(numField).join("");
  $("cfgThresholds").innerHTML = CFG_THRESHOLD_FIELDS.map(numField).join("");
  $("cfgToggles").innerHTML = CFG_TOGGLE_FIELDS.map(([key, label]) =>
    `<label class="cfg-toggle"><input type="checkbox" data-cfg="${key}" ${cfgData[key] ? "checked" : ""} /> ${label}</label>`
  ).join("");
  renderRulesReference();
}

async function saveSettings() {
  const payload = {};
  document.querySelectorAll('[data-cfg]').forEach(el => {
    const key = el.dataset.cfg;
    if (el.type === "checkbox") payload[key] = el.checked;
    else payload[key] = el.value === "" ? 0 : Number(el.value);
  });
  setCfgMsg("Saving…");
  try {
    const r = await fetch(api("/api/engine/config"), {
      method: "PUT",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (r.status === 401) return doLogout();
    cfgData = await r.json();
    if (!r.ok) throw new Error(cfgData.detail || "Save failed");
    renderSettings();
    setCfgMsg("Settings saved. They apply to the next engine run.", "ok");
  } catch (e) {
    setCfgMsg(e.message, "error");
  }
}

// ---- transaction reconciliation (full ledger) ------------------------------
const TXN_TYPE_LABELS = {
  opening_balance: "Opening Balance",
  closing_balance: "Closing Balance",
  invoice: "Invoice",
  payment: "Payment",
  credit_note: "Credit Note",
  debit_note: "Debit Note",
  other: "Other",
};

async function analyzeLedger() {
  const file = $("txnLedger").files[0];
  if (!file) { setTxnMsg("Choose a vendor ledger file.", "error"); return; }
  $("txnAnalyzeBtn").disabled = true;
  setTxnMsg("Analyzing ledger…");
  try {
    const fd = new FormData();
    fd.append("vendor", $("txnVendor").value.trim());
    fd.append("ledger", file);
    const r = await fetch(api("/api/txn-reconcile"), { method: "POST", headers: authHeaders(), body: fd });
    if (r.status === 401) return doLogout();
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Ledger analysis failed");
    setTxnMsg(`Ledger parsed: ${d.row_count} transactions.`, "ok");
    renderTxnResult(d);
  } catch (e) {
    setTxnMsg(e.message, "error");
  } finally {
    $("txnAnalyzeBtn").disabled = false;
  }
}

function setTxnMsg(text, kind) {
  const el = $("txnMessage");
  el.textContent = text || "";
  el.className = "message" + (kind ? " " + kind : "");
}

function renderTxnResult(d) {
  // SAP-side note (placeholder until SAP all-transactions API is wired)
  const note = $("txnSapNote");
  if (d.sap_side === "not_configured") {
    note.textContent = "Note: SAP all-transactions matching is not wired yet. This validates and " +
      "summarizes the vendor ledger only. Once the SAP API details are confirmed, SAP-side matching " +
      "will appear here.";
    note.classList.remove("hidden");
  } else {
    note.classList.add("hidden");
  }

  const b = d.bridge || {};
  const badge = $("txnBalancedBadge");
  if (b.balanced) { badge.textContent = "Balanced"; badge.className = "badge badge-ok"; }
  else { badge.textContent = "Difference " + fmtMoney(b.difference); badge.className = "badge badge-bad"; }

  const stats = [
    { label: "Opening Balance", value: fmtMoney(b.opening_balance), cls: "blue" },
    { label: "Total Debits", value: fmtMoney(b.total_debits), cls: "blue" },
    { label: "Total Credits", value: fmtMoney(b.total_credits), cls: "blue" },
    { label: "Computed Closing", value: fmtMoney(b.computed_closing), cls: "blue" },
    { label: "Stated Closing", value: fmtMoney(b.stated_closing), cls: "blue" },
    { label: "Difference", value: fmtMoney(b.difference), cls: b.balanced ? "green" : "red" },
  ];
  $("txnBridgeStats").innerHTML = stats.map(st =>
    `<div class="stat ${st.cls}"><div class="label">${st.label}</div><div class="value">${st.value}</div></div>`
  ).join("");
  renderTxnWarnings(d.warnings);
  $("txnBridgeSection").classList.remove("hidden");

  // by type
  const bt = d.by_type || {};
  const order = ["invoice", "debit_note", "payment", "credit_note", "other"];
  const keys = Object.keys(bt).sort((a, c) => order.indexOf(a) - order.indexOf(c));
  if (keys.length) {
    const rows = keys.map(k => {
      const g = bt[k];
      return `<tr><td>${escapeHtml(TXN_TYPE_LABELS[k] || k)}</td>
        <td class="num">${g.count}</td>
        <td class="num">${fmtMoney(g.debit)}</td>
        <td class="num">${fmtMoney(g.credit)}</td></tr>`;
    }).join("");
    $("txnByTypeHost").innerHTML = `<table><thead><tr>
      <th>Transaction Type</th><th class="num">Count</th>
      <th class="num">Total Debit</th><th class="num">Total Credit</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
    $("txnByTypeSection").classList.remove("hidden");
  } else {
    $("txnByTypeSection").classList.add("hidden");
  }

  // all transactions
  const txns = d.transactions || [];
  if (txns.length) {
    const rows = txns.map(t => `<tr>
      <td>${escapeHtml(t.date || "")}</td>
      <td>${escapeHtml(t.type_label || TXN_TYPE_LABELS[t.type] || "")}</td>
      <td>${escapeHtml(t.reference_no || "")}</td>
      <td class="num">${t.debit ? fmtMoney(t.debit) : ""}</td>
      <td class="num">${t.credit ? fmtMoney(t.credit) : ""}</td>
      <td>${escapeHtml(t.currency || "")}</td>
      <td>${escapeHtml(t.narration || "")}</td>
    </tr>`).join("");
    $("txnLinesHost").innerHTML = `<table><thead><tr>
      <th>Date</th><th>Type</th><th>Reference No</th>
      <th class="num">Debit</th><th class="num">Credit</th><th>Curr.</th><th>Narration</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
    $("txnLinesSection").classList.remove("hidden");
  } else {
    $("txnLinesSection").classList.add("hidden");
  }
}

function renderTxnWarnings(warnings) {
  const host = $("txnWarnings");
  if (!warnings || !warnings.length) { host.innerHTML = ""; return; }
  host.innerHTML = warnings.map(w => `<div class="warn">${escapeHtml(w)}</div>`).join("");
}

// ---- reconciliation history ------------------------------------------------
async function loadHistory() {
  const host = $("histHost");
  host.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const params = new URLSearchParams();
    const from = $("histFrom").value;
    const to = $("histTo").value;
    if (from) params.set("date_from", from);
    if (to) params.set("date_to", to);
    const r = await fetch(api("/api/history?" + params.toString()), { headers: authHeaders() });
    if (r.status === 401) return doLogout();
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Could not load history");
    renderHistoryList(d.runs || []);
  } catch (e) {
    host.innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

// Clear all reconciliation history (with confirmation).
async function clearHistory() {
  if (!confirm("Clear ALL reconciliation history? This cannot be undone.")) return;
  const btn = $("histClearAllBtn");
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(api("/api/history"), { method: "DELETE", headers: authHeaders() });
    if (r.status === 401) return doLogout();
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Could not clear history");
    // Clear the detail page (show its empty prompt) and reload the list.
    histDetail = null;
    $("histDetailSection").classList.add("hidden");
    const empty = $("reconDetailEmpty");
    if (empty) empty.classList.remove("hidden");
    loadHistory();
  } catch (e) {
    alert(e.message || "Failed to clear history");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function fmtDateTime(iso) {
  if (!iso) return "";
  const dt = new Date(iso);
  if (isNaN(dt)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(dt.getDate())}-${p(dt.getMonth() + 1)}-${dt.getFullYear()} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
}

function renderHistoryList(runs) {
  const host = $("histHost");
  if (!runs.length) {
    host.innerHTML = `<div class="empty">No reconciliation runs found for this filter.</div>`;
    return;
  }
  const body = runs.map(r => {
    const net = Number(r.net_difference || 0);
    const netCls = Math.abs(net) < 0.01 ? "net-zero" : "net-diff";
    const type = (r.recon_type || "");
    const isTxn = type === "Transaction";
    const isEngine = type === "Engine";
    const typeBadge = isTxn
      ? `<span class="badge badge-txn">Transaction</span>`
      : isEngine
        ? `<span class="badge badge-engine">Engine</span>`
        : `<span class="badge badge-cb">Closing Balance</span>`;
    // Status badge from final_status (Engine/Closing runs).
    const fs = (r.final_status || "").toUpperCase();
    let statusHtml = "—";
    if (r.final_status) {
      const cls = fs.includes("FULLY") || fs.includes("TOLERANCE") ? "badge-ok"
        : fs.includes("PARTIAL") ? "badge-txn"
        : fs.includes("EXCEPTION") || fs.includes("UNRECON") ? "badge-bad" : "badge-cb";
      statusHtml = `<span class="badge ${cls}">${escapeHtml(r.final_status)}</span>`;
    }
    const dash = (v) => isTxn ? "—" : (v ?? "");
    // Per-side record counts come from the engine's record_tally.
    const rt = r.record_tally || {};
    const sapRecords = isEngine ? (rt.sap_transactions ?? "") : (isTxn ? "—" : (r.sap_count ?? ""));
    const venRecords = isEngine ? (rt.vendor_transactions ?? "") : (isTxn ? r.statement_rows : "");
    // RECORD-LEVEL counts: each category counts the RECORDS it consumes (a
    // match consumes 1 SAP + 1 vendor), so Match + Mismatch + Miss SAP +
    // Miss Vendor + Possibly Match == SAP records + Vendor records.
    const matchCnt = isEngine ? (r.match_count ?? 0) : dash(r.matched_count);
    const missSapCnt = isEngine ? (r.miss_sap_count ?? 0) : dash(r.only_in_sap_count);
    const missVendorCnt = isEngine ? (r.miss_vendor_count ?? 0) : dash(r.only_in_vendor_count);
    const possiblyCnt = isEngine ? (r.possibly_match_count ?? 0) : 0;
    // Mismatch = everything that is NOT a clean match = Total − Match, which
    // equals Miss SAP + Miss Vendor + Possibly Match.
    const mismatchCnt = isEngine
      ? ((r.miss_sap_count ?? 0) + (r.miss_vendor_count ?? 0) + (r.possibly_match_count ?? 0))
      : dash(r.amount_mismatch_count);
    const reqIdCell = r.request_id
      ? `<div class="req-id">${escapeHtml(r.request_id)}</div>`
      : `<span class="req-id-date">—</span>`;
    return `<tr>
      <td>${reqIdCell}</td>
      <td class="hist-datetime">${escapeHtml(fmtDateTime(r.created_at))}</td>
      <td>${escapeHtml(r.vendor)}</td>
      <td>${escapeHtml(r.vendor_name || "")}</td>
      <td class="num">${sapRecords}</td>
      <td class="num">${venRecords}</td>
      <td class="num">${matchCnt}</td>
      <td class="num">${mismatchCnt}</td>
      <td class="num">${missSapCnt}</td>
      <td class="num">${missVendorCnt}</td>
      <td class="num">${possiblyCnt}</td>
      <td class="num ${netCls}">${fmtMoney(net)}</td>
      <td><button class="hist-view-btn" onclick="viewHistoryRun(${r.id})">View</button></td>
    </tr>`;
  }).join("");
  host.innerHTML = `
    <table>
      <thead><tr>
        <th>Request ID</th><th>Date &amp; Time</th><th>Vendor</th><th>Vendor Name</th>
        <th class="num">SAP Records</th><th class="num">Vendor Records</th>
        <th class="num">Match</th><th class="num">Mismatch</th>
        <th class="num">Miss SAP</th><th class="num">Miss Vendor</th>
        <th class="num">Possibly Match</th>
        <th class="num">Net Diff</th><th></th>
      </tr></thead>
      <tbody>${body}</tbody>
    </table>`;
}

async function viewHistoryRun(id) {
  try {
    const r = await fetch(api("/api/history/" + id), { headers: authHeaders() });
    if (r.status === 401) return doLogout();
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Could not load run");
    histDetail = d;
    histDetail._id = id;
    histDetailTab = "matched";
    if ((d.recon_type || "") === "Engine" || d.engine_result) {
      renderHistEngineDetail();
    } else if ((d.recon_type || "") === "Transaction" || d.bridge) {
      renderHistTxnDetail();
    } else {
      renderHistDetail();
    }
    // Open the detail on its own full-width Reconciliation page.
    const empty = $("reconDetailEmpty");
    if (empty) empty.classList.add("hidden");
    $("histDetailSection").classList.remove("hidden");
    showPage("reconDetail");
    window.scrollTo({ top: 0, behavior: "auto" });
  } catch (e) {
    setMessage(e.message, "error");
  }
}

// Engine run detail (18-rule engine results) in the History panel.
function renderHistEngineDetail() {
  const d = histDetail;
  const er = d.engine_result || {};
  const c = er.counts || d.counts || {};
  const b = er.balance || d.balance || {};
  const results = er.results || [];

  $("histDetailTitle").textContent =
    `Engine Reconciliation — ${d.vendor || er.vendor_code || ""}${(d.vendor_name || er.vendor_name) ? " · " + (d.vendor_name || er.vendor_name) : ""}`;
  const fs = (d.final_status || er.final_status || "").toUpperCase();
  $("histDetailLine").innerHTML =
    `Status: <strong>${escapeHtml(d.final_status || er.final_status || "—")}</strong> · ` +
    `${results.length} transactions`;

  // Which "side" is each status missing from? Group the cards accordingly.
  // not-in-SAP  = vendor has it, SAP doesn't.
  // not-in-vendor = SAP has it, vendor doesn't.
  const NOT_IN_SAP = ["MISSING IN SAP", "CREDIT NOTE MISSING IN SAP", "UNALLOCATED PAYMENT"];
  const NOT_IN_VENDOR = ["MISSING IN VENDOR STATEMENT", "PAYMENT NOT REFLECTED BY VENDOR",
                         "DEBIT NOTE MISSING IN VENDOR STATEMENT"];
  const RESOLVED = ["AUTO MATCHED", "PAYMENT MATCHED", "FULLY MATCHED", "POSSIBLE MATCH",
                    "POSSIBLE DUPLICATE", "PARTIAL PAYMENT", "RECONCILED - TDS",
                    "BANK CHARGES", "ROUNDING DIFFERENCE"];
  const statusClass = (st) => {
    const s = (st || "").toUpperCase();
    if (s.includes("MATCHED")) return "green";
    if (s.includes("POSSIBLE") || s.includes("PARTIAL") || s.includes("ROUNDING")
        || s.includes("TDS") || s.includes("BANK")) return "amber";
    return "red";
  };

  // Total absolute amount involved per status (SAP amount if present, else vendor).
  const amtByStatus = {};
  (results || []).forEach(r => {
    const a = Math.abs(Number(r.sap_amount != null ? r.sap_amount : (r.vendor_amount || 0)) || 0);
    amtByStatus[r.match_status] = (amtByStatus[r.match_status] || 0) + a;
  });
  const amtFor = (list) => list.reduce((n, st) => n + (amtByStatus[st] || 0), 0);

  // Collect filter lists per card (index-aligned) for click handling.
  const filterMap = [];
  let idx = 0;
  // A card shows the count big, with the amount involved underneath.
  const card = (label, value, cls, filter, amount) => {
    const clickable = filter !== undefined;
    const i = clickable ? idx++ : -1;
    if (clickable) filterMap[i] = filter;
    const amtLine = amount != null ? `<div class="stat-amt">₹ ${fmtMoney(amount)}</div>` : "";
    return `<div class="stat ${cls}${clickable ? " stat-clickable" : ""}" ${clickable ? `data-stat="${i}" onclick="filterEngineByStat(${i})"` : ""}>
      <div class="label">${label}</div><div class="value">${value}</div>${amtLine}</div>`;
  };
  const groupCards = (list) => list
    .filter(st => (c[st] || 0) > 0)
    .sort((a, bb) => (c[bb] - c[a]))
    .map(st => card(toTitle(st), c[st], statusClass(st), [st], amtByStatus[st] || 0)).join("");

  const matched = (c["AUTO MATCHED"] || 0) + (c["PAYMENT MATCHED"] || 0) + (c["FULLY MATCHED"] || 0);
  const matchedAmt = amtFor(["AUTO MATCHED", "PAYMENT MATCHED", "FULLY MATCHED"]);
  const possibleAmt = amtFor(["POSSIBLE MATCH", "POSSIBLE DUPLICATE"]);
  const sumFor = (list) => list.reduce((n, st) => n + (c[st] || 0), 0);

  // Any statuses not covered by the three groups above (safety net).
  const covered = new Set([...NOT_IN_SAP, ...NOT_IN_VENDOR, ...RESOLVED]);
  const otherKeys = Object.keys(c).filter(k => k !== "total" && !covered.has(k));

  // Build a reconciliation worksheet table. Each line that has a status filter
  // is clickable and opens those rows in the list below.
  const diff = Number(b.balance_difference || 0);
  const vendorName = d.vendor_name || er.vendor_name || "";
  const vendorCode = d.vendor || er.vendor_code || "";

  const wsFilters = [];   // index-aligned filter lists for clickable worksheet rows
  let wi = 0;
  const money = (v) => fmtMoney(v);
  // A clickable line: label (indented), amount, optional group subtotal.
  const line = (label, statuses, { bold = false, indent = false, subtotal = null } = {}) => {
    const amt = amtFor(statuses);
    const cnt = sumFor(statuses);
    const clickable = statuses && statuses.length;
    const i = clickable ? wi++ : -1;
    if (clickable) wsFilters[i] = statuses;
    return `<tr class="${clickable ? "ws-click" : ""}${bold ? " ws-bold" : ""}" ${clickable ? `data-ws="${i}" onclick="filterEngineByWorksheet(${i})"` : ""}>
      <td class="${indent ? "ws-indent" : ""}">${escapeHtml(label)}${clickable ? ` <span class="ws-cnt">(${cnt})</span>` : ""}</td>
      <td class="num">${clickable ? "₹ " + money(amt) : ""}</td>
      <td class="num ws-subtotal">${subtotal != null ? "₹ " + money(subtotal) : ""}</td>
    </tr>`;
  };
  const blank = `<tr class="ws-blank"><td></td><td></td><td></td></tr>`;

  // Signed bridge from the backend (each item carries its proper sign so the
  // final residual nets to zero when the reconciliation balances).
  const bridge = b.bridge || {};
  const signed = bridge.by_status_signed || {};
  const sgn = (st) => signed[st] || 0;          // signed amount for one status
  const vendorOnlyTotal = bridge.vendor_only_total || 0;   // Missing-in-SAP group
  const sapOnlyTotal = bridge.sap_only_total || 0;         // Missing-in-Vendor group
  // A worksheet line shows the amount with its reconciling direction:
  //   dir = +1  Missing-in-SAP group (amount to ADD to SAP)  -> positive
  //   dir = -1  Missing-in-Vendor group (amount to SUBTRACT) -> negative
  const lineS = (label, status, dir, { subtotal = null } = {}) => {
    const cnt = c[status] || 0;
    const i = wi++;
    wsFilters[i] = [status];
    const amt = dir * Math.abs(sgn(status));
    const sub = subtotal != null ? dir * Math.abs(subtotal) : null;
    const amtCls = amt < 0 ? "net-diff" : "";
    return `<tr class="ws-click" data-ws="${i}" onclick="filterEngineByWorksheet(${i})">
      <td class="ws-indent">${escapeHtml(label)} <span class="ws-cnt">(${cnt})</span></td>
      <td class="num ${amtCls}">₹ ${money(amt)}</td>
      <td class="num ws-subtotal ${sub != null && sub < 0 ? "net-diff" : ""}">${sub != null ? "₹ " + money(sub) : ""}</td>
    </tr>`;
  };

  // Reconciliation header: Vendor name first, then Request ID + Date below it.
  const reqId = d.request_id || (er && er.request_id) || "";
  const runDate = d.created_at ? fmtDateTime(d.created_at) : "";
  let ws = `<table class="recon-ws"><tbody>`;
  // Vendor name shown prominently above the figures column.
  ws += `<tr class="ws-bold"><td>Vendor Name</td><td></td><td class="ws-vendor-name">${escapeHtml(vendorName || vendorCode)}</td></tr>`;
  if (reqId || runDate) {
    ws += `<tr class="ws-meta"><td>Request ID / Date</td><td></td><td class="ws-vendor-meta">`
        + (reqId ? `<div class="ws-reqid">${escapeHtml(reqId)}</div>` : "")
        + (runDate ? `<div class="ws-rundate">${escapeHtml(runDate)}</div>` : "")
        + `</td></tr>`;
  }
  ws += `<tr class="ws-bold"><td>SAP Closing Balance</td><td></td><td class="num">₹ ${money(b.sap_closing_balance)}</td></tr>`;
  ws += `<tr class="ws-bold"><td>Vendor Closing Balance</td><td></td><td class="num">₹ ${money(b.vendor_closing_balance)}</td></tr>`;
  ws += `<tr class="ws-bold"><td>Difference (|SAP| − |Vendor|)</td><td></td><td class="num ${Math.abs(diff) < 0.01 ? "net-zero" : "net-diff"}">₹ ${money(diff)}</td></tr>`;
  ws += blank;

  // Opening balance difference — shown as a net line inside "Missing in SAP".
  const openDiff = Number(bridge.opening_difference != null ? bridge.opening_difference : (b.opening_difference || 0));
  // The Missing-in-SAP subtotal must include the opening difference too.
  const missingSapSubtotal = Math.abs(vendorOnlyTotal) + Math.abs(openDiff);

  ws += `<tr class="ws-head"><td colspan="3">Missing in SAP (in vendor statement, not in SAP)</td></tr>`;
  ws += lineS("A. Unallocated Payment", "UNALLOCATED PAYMENT", 1);
  ws += lineS("B. Missing In Sap", "MISSING IN SAP", 1);
  ws += lineS("C. Credit Note Missing In Sap", "CREDIT NOTE MISSING IN SAP", 1);
  // D. Opening Balance Difference (net), only when there is one.
  ws += `<tr><td class="ws-indent">D. Opening Balance Difference</td>
      <td class="num ${openDiff < 0 ? "net-diff" : ""}">₹ ${money(openDiff)}</td>
      <td class="num ws-subtotal">₹ ${money(missingSapSubtotal)}</td></tr>`;
  ws += blank;

  ws += `<tr class="ws-head"><td colspan="3">Missing in Vendor Statement (in SAP, not in vendor statement)</td></tr>`;
  ws += lineS("A. Payment Not Reflected By Vendor", "PAYMENT NOT REFLECTED BY VENDOR", -1);
  ws += lineS("B. Missing In Vendor Statement", "MISSING IN VENDOR STATEMENT", -1, { subtotal: sapOnlyTotal });
  if ((c["DEBIT NOTE MISSING IN VENDOR STATEMENT"] || 0) > 0) {
    ws += lineS("C. Debit Note Missing In Vendor Statement", "DEBIT NOTE MISSING IN VENDOR STATEMENT", -1);
  }
  ws += blank;

  // Net TDS / rounding adjustment across matched payments (the small remainder).
  // Clickable -> shows the matched pairs that carry the TDS/rounding gaps.
  const tdsAdj = Number(bridge.tds_adjustment || 0);
  if (Math.abs(tdsAdj) > 0.009) {
    const TDS_STATUSES = ["RECONCILED - TDS", "BANK CHARGES", "ROUNDING DIFFERENCE"];
    const ti = wi++;
    wsFilters[ti] = TDS_STATUSES;
    const tdsCount = results.filter(x => TDS_STATUSES.includes(x.match_status)).length;
    ws += `<tr class="ws-head"><td colspan="3">Adjustments</td></tr>`;
    ws += `<tr class="ws-click" data-ws="${ti}" onclick="filterEngineByWorksheet(${ti})">
      <td class="ws-indent">TDS / Rounding Adjustment <span class="ws-cnt">(${tdsCount})</span></td>
      <td class="num ${tdsAdj < 0 ? "net-diff" : ""}">₹ ${money(tdsAdj)}</td>
      <td class="num ws-subtotal ${tdsAdj < 0 ? "net-diff" : ""}">₹ ${money(tdsAdj)}</td></tr>`;
    ws += blank;
  }

  // After the opening difference, open items and TDS adjustment, the bridge ties.
  const residual = Number(bridge.residual || 0);
  const balanced = bridge.balanced;
  ws += `<tr class="ws-bold"><td>Diff (should be zero)</td><td></td><td class="num ${balanced ? "net-zero" : "net-diff"}">₹ ${money(residual)}</td></tr>`;
  if (!balanced) {
    ws += `<tr><td colspan="3" class="tpl-note" style="color:var(--amber)">Not zero yet — unmatched items don't fully explain the balance gap. Resolve the open items (or adjust tolerances) to close it.</td></tr>`;
  }
  ws += `</tbody></table>`;

  $("histDetailStats").innerHTML = ws;
  histDetail._wsFilters = wsFilters;
  histDetail._statFilters = filterMap;   // kept for compatibility

  // Open items = unreconciled rows (everything not cleanly matched). These are
  // what still needs action, so the list defaults to showing them.
  const OPEN_STATUSES = [...NOT_IN_SAP, ...NOT_IN_VENDOR, ...otherKeys,
                         "POSSIBLE MATCH", "POSSIBLE DUPLICATE", "EXCEPTION", "UNMATCHED"];
  histDetail._openStatuses = OPEN_STATUSES;
  const openCount = results.filter(x => OPEN_STATUSES.includes(x.match_status)).length;

  // Filter bar: Open items (default) / All / per-status.
  const tabs = $("histDetailTabs");
  if (tabs) {
    const statuses = Array.from(new Set(results.map(x => x.match_status)));
    tabs.innerHTML = `<label style="font-size:12px;color:var(--muted);font-weight:600">Show
      <select id="histEngFilter" class="input" style="margin-left:8px;max-width:300px">
        <option value="__open__">Open items — unreconciled (${openCount})</option>
        <option value="">All transactions (${results.length})</option>
        ${statuses.map(s => `<option value="${escapeHtml(s)}">${escapeHtml(s)} (${results.filter(x => x.match_status === s).length})</option>`).join("")}
      </select></label>`;
    const sel = $("histEngFilter");
    if (sel) sel.onchange = () => {
      if (sel.value === "__open__") renderHistEngineTable(histDetail._openStatuses, "Open Items (unreconciled)");
      else renderHistEngineTable(sel.value ? [sel.value] : [], sel.value || "All Transactions");
    };
  }
  // Default view: the open-item list.
  renderHistEngineTable(OPEN_STATUSES, "Open Items (unreconciled)");
}

// Click a worksheet line -> open that category's rows in the list below.
function filterEngineByWorksheet(i) {
  const statuses = (histDetail._wsFilters || [])[i] || [];
  const tdsSet = ["RECONCILED - TDS", "BANK CHARGES", "ROUNDING DIFFERENCE"];
  const isTds = statuses.length === tdsSet.length && statuses.every(s => tdsSet.includes(s));
  const label = isTds ? "TDS / Rounding Adjustment — matched pairs" : statuses.map(toTitle).join(", ");
  document.querySelectorAll(".recon-ws .ws-click").forEach(el =>
    el.classList.toggle("ws-active", el.dataset.ws === String(i)));
  // Sync the dropdown if present (single status).
  const sel = $("histEngFilter");
  if (sel) sel.value = statuses.length === 1 ? statuses[0] : "";
  renderHistEngineTable(statuses, label || "Items");
  const host = $("histDetailHost");
  if (host) host.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// Click a stat card -> filter the results table to that card's statuses and
// scroll the table into view.
function filterEngineByStat(i) {
  const filters = (histDetail._statFilters || [])[i] || [];
  // Reflect in the dropdown when it's a single status.
  const sel = $("histEngFilter");
  if (sel) sel.value = filters.length === 1 ? filters[0] : "";
  // Highlight the active card.
  document.querySelectorAll("#histDetailStats .stat-clickable").forEach(el =>
    el.classList.toggle("stat-active", el.dataset.stat === String(i)));
  renderHistEngineTable(filters);
  const host = $("histDetailHost");
  if (host) host.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderHistEngineTable(statusFilter, heading) {
  const er = histDetail.engine_result || {};
  let rows = er.results || [];
  const wanted = Array.isArray(statusFilter)
    ? statusFilter
    : (statusFilter ? [statusFilter] : []);
  if (wanted.length) rows = rows.filter(r => wanted.includes(r.match_status));
  const host = $("histDetailHost");
  const title = heading ? `<h3 class="tpl-h3" style="margin:4px 0 10px">${escapeHtml(heading)} — ${rows.length}</h3>` : "";
  if (!rows.length) { host.innerHTML = title + `<div class="empty">No rows for this category.</div>`; return; }

  const OPEN = new Set(["UNALLOCATED PAYMENT","MISSING IN SAP","CREDIT NOTE MISSING IN SAP",
    "MISSING IN VENDOR STATEMENT","PAYMENT NOT REFLECTED BY VENDOR","DEBIT NOTE MISSING IN VENDOR STATEMENT"]);
  const resolutions = histDetail.resolutions || {};
  const from = histDetail.date_from || "";
  const to = histDetail.date_to || "";
  const outOfPeriod = (d) => {
    if (!d || (!from && !to)) return false;
    if (from && d < from) return true;
    if (to && d > to) return true;
    return false;
  };

  const body = rows.map(r => {
    const tid = r.transaction_id || "";
    const res = resolutions[tid];
    const isOpen = OPEN.has(r.match_status);
    const sd = r.sap_date || "", vd = r.vendor_date || "";
    const oop = outOfPeriod(sd) || outOfPeriod(vd);
    const cls = ((res && res.resolved) ? "row-resolved" : "") + (oop ? " row-oop" : "");
    let action = "";
    if (isOpen) {
      action = (res && res.resolved)
        ? `<button class="btn btn-secondary btn-sm" onclick="toggleResolve('${tid}', false)">Reopen</button>`
        : `<button class="btn btn-primary btn-sm" onclick="toggleResolve('${tid}', true)">Resolve</button>`;
    }
    const dateCell = (val) => `<td class="${outOfPeriod(val) ? "oop-cell" : ""}">${escapeHtml(val)}${outOfPeriod(val) ? ' <span class="oop-tag">out of period</span>' : ""}</td>`;
    const note = (res && res.note) ? `<div class="res-note">✓ ${escapeHtml(res.note)}</div>` : "";
    return `<tr class="${cls.trim()}">
      <td>${escapeHtml(r.match_status || "")}${note}</td>
      <td>${escapeHtml(r.match_rule || "")}</td>
      <td>${escapeHtml(r.sap_doc_number || "")}</td>
      ${dateCell(sd)}
      <td>${escapeHtml(r.vendor_statement_reference || "")}</td>
      ${dateCell(vd)}
      <td class="num">${r.sap_amount == null ? "" : fmtMoney(r.sap_amount)}</td>
      <td class="num">${r.vendor_amount == null ? "" : fmtMoney(r.vendor_amount)}</td>
      <td class="num">${r.difference == null ? "" : fmtMoney(r.difference)}</td>
      <td>${escapeHtml(r.comments || "")}</td>
    </tr>`;
  }).join("");
  host.innerHTML = title + `<table><thead><tr>
    <th>Status</th><th>Rule</th><th>SAP Doc</th><th>SAP Date</th><th>Vendor Ref</th><th>Vendor Date</th>
    <th class="num">SAP Amt</th><th class="num">Vendor Amt</th><th class="num">Diff</th><th>Comments</th>
  </tr></thead><tbody>${body}</tbody></table>`;
}

// Resolve / reopen an open item. On resolve, prompt for an optional note.
async function toggleResolve(transactionId, resolved) {
  let note = "";
  if (resolved) {
    note = prompt("Add a note for resolving this item (optional):", "") || "";
  }
  const runId = histDetail._id;
  try {
    const r = await fetch(api(`/api/history/${runId}/resolve`), {
      method: "POST",
      headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
      body: JSON.stringify({ transaction_id: transactionId, resolved, note }),
    });
    if (r.status === 401) return doLogout();
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Could not update");
    histDetail = d;
    histDetail._id = runId;
    renderHistEngineDetail();
  } catch (e) {
    alert(e.message || "Failed to update item");
  }
}

function renderHistTxnDetail() {
  const d = histDetail;
  const b = d.bridge || {};
  $("histDetailTitle").textContent =
    `Transaction Reconciliation — ${d.vendor || ""}${d.vendor_name ? " · " + d.vendor_name : ""}`;
  $("histDetailLine").textContent =
    `Transactions: ${d.row_count || 0} · ${b.balanced ? "Balanced" : "Difference " + fmtMoney(b.difference)}`;
  const stats = [
    { label: "Opening Balance", value: fmtMoney(b.opening_balance), cls: "blue" },
    { label: "Total Debits", value: fmtMoney(b.total_debits), cls: "blue" },
    { label: "Total Credits", value: fmtMoney(b.total_credits), cls: "blue" },
    { label: "Computed Closing", value: fmtMoney(b.computed_closing), cls: "blue" },
    { label: "Stated Closing", value: fmtMoney(b.stated_closing), cls: "blue" },
    { label: "Difference", value: fmtMoney(b.difference), cls: b.balanced ? "green" : "red" },
  ];
  $("histDetailStats").innerHTML = `<div class="stat-grid">` + stats.map(st =>
    `<div class="stat ${st.cls}"><div class="label">${st.label}</div><div class="value">${st.value}</div></div>`
  ).join("") + `</div>`;
  // Hide the closing-balance tabs; show the transactions list instead.
  $("histDetailTabs").classList.add("hidden");
  const txns = d.transactions || [];
  if (!txns.length) { $("histDetailHost").innerHTML = `<div class="empty">No transactions.</div>`; return; }
  const rows = txns.map(t => `<tr>
    <td>${escapeHtml(t.date || "")}</td>
    <td>${escapeHtml(t.type_label || t.type || "")}</td>
    <td>${escapeHtml(t.reference_no || "")}</td>
    <td class="num">${t.debit ? fmtMoney(t.debit) : ""}</td>
    <td class="num">${t.credit ? fmtMoney(t.credit) : ""}</td>
    <td>${escapeHtml(t.currency || "")}</td>
    <td>${escapeHtml(t.narration || "")}</td>
  </tr>`).join("");
  $("histDetailHost").innerHTML = `<table><thead><tr>
    <th>Date</th><th>Type</th><th>Reference No</th>
    <th class="num">Debit</th><th class="num">Credit</th><th>Curr.</th><th>Narration</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

function renderHistDetail() {
  if (!histDetail) return;
  const d = histDetail;
  const s = (d.result && d.result.summary) || {};
  $("histDetailTabs").classList.remove("hidden");
  document.querySelectorAll("#histDetailTabs .tab").forEach(t =>
    t.classList.toggle("active", t.dataset.htab === "matched"));
  $("histDetailTitle").textContent = `Closing Balance Reconciliation — ${d.vendor}${d.vendor_name ? " · " + d.vendor_name : ""}`;
  $("histDetailLine").textContent =
    `SAP items: ${d.sap_count} · Statement rows: ${d.statement_row_count} · Company: ${d.company_code}`;

  const stats = [
    { label: "SAP Total", value: fmtMoney(s.sap_total), cls: "blue" },
    { label: "Vendor Total", value: fmtMoney(s.vendor_total), cls: "blue" },
    { label: "Net Difference", value: fmtMoney(s.net_difference), cls: Math.abs(s.net_difference || 0) < 0.01 ? "green" : "red" },
    { label: "Matched", value: s.matched_count, cls: "green" },
    { label: "Amount Mismatch", value: s.amount_mismatch_count, cls: "amber" },
    { label: "Invoice Missing in SAP", value: s.only_in_sap_count, cls: "red" },
    { label: "Missing in Vendor", value: s.only_in_vendor_count, cls: "red" },
  ];
  $("histDetailStats").innerHTML = `<div class="stat-grid">` + stats.map(st =>
    `<div class="stat ${st.cls}"><div class="label">${st.label}</div><div class="value">${st.value}</div></div>`
  ).join("") + `</div>`;
  renderHistDetailTable();
}

function renderHistDetailTable() {
  const host = $("histDetailHost");
  const rows = (histDetail.result && histDetail.result[histDetailTab]) || [];
  const cols = TAB_COLUMNS[histDetailTab];
  if (!rows.length) { host.innerHTML = `<div class="empty">No rows in this category.</div>`; return; }
  const head = cols.map(([, label, num]) => `<th class="${num ? "num" : ""}">${label}</th>`).join("");
  const body = rows.map(row => {
    const tds = cols.map(([key, , num]) => {
      let val = row[key];
      let cls = num ? "num" : "";
      if (num && typeof val === "number") {
        if (key === "difference") cls += val > 0 ? " diff-pos" : (val < 0 ? " diff-neg" : "");
        val = fmtMoney(val);
      }
      return `<td class="${cls}">${escapeHtml(val == null ? "" : String(val))}</td>`;
    }).join("");
    return `<tr>${tds}</tr>`;
  }).join("");
  host.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function exportHistoryRun() {
  if (!histDetail || !histDetail._id) return;
  const a = document.createElement("a");
  a.href = api("/api/history/" + histDetail._id + "/export");
  // include the token via a short-lived approach: open in a fetch->blob
  fetch(a.href, { headers: authHeaders() })
    .then(r => r.status === 401 ? (doLogout(), null) : r.blob())
    .then(blob => {
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `reconciliation_${histDetail.vendor || "vendor"}_${histDetail._id}.xlsx`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    })
    .catch(e => setMessage(e.message, "error"));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---- wire up ---------------------------------------------------------------
$("previewBtn").addEventListener("click", previewSap);
$("reconcileBtn").addEventListener("click", reconcile);
$("exportBtn").addEventListener("click", exportExcel);
$("exportSapBtn").addEventListener("click", exportSapItems);
$("tplXlsxBtn").addEventListener("click", () => downloadTemplate("xlsx"));
$("tplCsvBtn").addEventListener("click", () => downloadTemplate("csv"));

// Engine reconciliation controls
$("engRunBtn").addEventListener("click", runEngine);
$("engSapTplXlsxBtn").addEventListener("click", () => downloadEngineTemplate("sap", "xlsx"));
$("engSapTplCsvBtn").addEventListener("click", () => downloadEngineTemplate("sap", "csv"));
$("engVenTplXlsxBtn").addEventListener("click", () => downloadEngineTemplate("vendor", "xlsx"));
$("engVenTplCsvBtn").addEventListener("click", () => downloadEngineTemplate("vendor", "csv"));
engToggleSource();  // set initial field visibility (live is default)
// Settings controls
$("cfgSaveBtn").addEventListener("click", saveSettings);
$("cfgReloadBtn").addEventListener("click", loadSettings);

// Transaction reconciliation controls
$("txnAnalyzeBtn").addEventListener("click", analyzeLedger);
$("txnTplXlsxBtn").addEventListener("click", () => downloadTxnTemplate("xlsx"));
$("txnTplCsvBtn").addEventListener("click", () => downloadTxnTemplate("csv"));

// History page controls
$("histRefreshBtn").addEventListener("click", loadHistory);
$("histApplyBtn").addEventListener("click", loadHistory);
$("histClearBtn").addEventListener("click", () => {
  $("histFrom").value = ""; $("histTo").value = ""; loadHistory();
});
$("histDetailExportBtn").addEventListener("click", exportHistoryRun);
document.querySelectorAll("#histDetailTabs .tab").forEach(t =>
  t.addEventListener("click", () => {
    histDetailTab = t.dataset.htab;
    document.querySelectorAll("#histDetailTabs .tab").forEach(x =>
      x.classList.toggle("active", x === t));
    renderHistDetailTable();
  }));
document.querySelectorAll(".tab").forEach(t =>
  t.addEventListener("click", () => { activeTab = t.dataset.tab; syncTabs(); renderTable(); }));

// Enter to submit login
["login-username", "login-password"].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });
});

resumeSession();
