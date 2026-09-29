import { API_BASE } from "./config.js";

const ACCESS = "lsuite_access";
const REFRESH = "lsuite_refresh";

const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch {} },
};

export const tokens = {
  get access() { return store.get(ACCESS); },
  get refresh() { return store.get(REFRESH); },
  set(access, refresh) { if (access) store.set(ACCESS, access); if (refresh) store.set(REFRESH, refresh); },
  clear() { store.set(ACCESS, null); store.set(REFRESH, null); },
  isLoggedIn() { return !!store.get(ACCESS); },
};

// Social login lands with #access=...&refresh=... in the URL fragment.
(() => {
  const p = new URLSearchParams(location.hash.slice(1));
  if (p.get("access")) {
    tokens.set(p.get("access"), p.get("refresh"));
    history.replaceState(null, "", location.pathname + location.search);
  }
})();

function errorText(data, fallback) {
  const d = data?.detail ?? data?.error ?? data?.message ?? data;
  if (!d) return fallback;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.join(" ");
  return Object.entries(d).map(([k, v]) => `${k === "non_field_errors" ? "" : k + ": "}${[].concat(v).join(" ")}`).join(" · ");
}

async function refreshAccess() {
  if (!tokens.refresh) return false;
  const res = await fetch(`${API_BASE}/api/token/refresh`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh: tokens.refresh }),
  }).catch(() => null);
  if (!res?.ok) return false;
  tokens.set((await res.json()).access);
  return true;
}

export async function request(path, { method = "GET", body, raw = false } = {}, retried = false) {
  const headers = {};
  if (body !== undefined && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (tokens.access) headers.Authorization = `Bearer ${tokens.access}`;
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method, headers, body: body === undefined || body instanceof FormData ? body : JSON.stringify(body),
    });
  } catch {
    throw new Error("Can't reach the server. Check your connection.");
  }
  if (res.status === 401 && !retried && tokens.refresh) {
    if (await refreshAccess()) return request(path, { method, body, raw }, true);
    tokens.clear();
    location.href = "/login.html";
    throw new Error("Session expired.");
  }
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    throw new Error(errorText(data, `${res.status} ${res.statusText}`));
  }
  if (raw) return res;
  if (res.status === 204) return null;
  return (res.headers.get("content-type") || "").includes("json") ? res.json() : res.text();
}

const get = (p) => request(p);
const post = (p, body = {}) => request(p, { method: "POST", body });
const patch = (p, body) => request(p, { method: "PATCH", body });
const del = (p) => request(p, { method: "DELETE" });
const qs = (params = {}) => {
  const s = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null)).toString();
  return s ? `?${s}` : "";
};

export const api = {
  // auth
  register: (b) => post("/api/authusers/register", b),
  login: (username, password) => post("/api/authusers/login", { username, password }),
  logout: () => post("/api/authusers/logout"),
  me: () => get("/api/authusers/me"),
  profile: () => get("/api/authusers/profile"),
  updateProfile: (b) => patch("/api/authusers/profile", b),
  changePassword: (b) => post("/api/authusers/change-password", b),
  requestReset: (email) => post("/api/authusers/password-reset", { email }),
  confirmReset: (b) => post("/api/authusers/password-reset/confirm", b),
  links: () => get("/api/authusers/links"),
  addLink: (b) => post("/api/authusers/links", b),
  deleteLink: (id) => del(`/api/authusers/links/${id}`),
  socialProviders: () => get("/auth/social/providers"),
  socialLoginUrl: (p) => `${API_BASE}/auth/social/${p}/login`,

  dashboard: () => get("/api/dashboard"),

  // resources
  accounts: () => get("/api/accounts"),
  createAccount: (b) => post("/api/accounts", b),
  updateAccount: (id, b) => patch(`/api/accounts/${id}`, b),
  deleteAccount: (id) => del(`/api/accounts/${id}`),
  categories: () => get("/api/categories"),
  createCategory: (b) => post("/api/categories", b),
  updateCategory: (id, b) => patch(`/api/categories/${id}`, b),
  deleteCategory: (id) => del(`/api/categories/${id}`),
  statements: () => get("/api/statements"),
  deleteStatement: (id) => del(`/api/statements/${id}`),
  invoices: () => get("/api/invoices"),
  createInvoice: (b) => post("/api/invoices", b),
  updateInvoice: (id, b) => patch(`/api/invoices/${id}`, b),
  deleteInvoice: (id) => del(`/api/invoices/${id}`),
  transactions: (params) => get(`/api/transactions${qs(params)}`),
  updateTransaction: (id, b) => patch(`/api/transactions/${id}`, b),
  erpnextConfigs: () => get("/api/erpnext-configs"),
  createErpnextConfig: (b) => post("/api/erpnext-configs", b),
  updateErpnextConfig: (id, b) => patch(`/api/erpnext-configs/${id}`, b),
  deleteErpnextConfig: (id) => del(`/api/erpnext-configs/${id}`),
  syncLogs: () => get("/api/erpnext-sync-logs?limit=50"),
  pdfJobs: () => get("/api/pdf-jobs"),
  job: (id) => get(`/api/jobs/${id}`),

  // categorization
  categoryStats: () => get("/api/bridge/categories"),
  categoryTransactions: (id) => get(`/api/bridge/categories/${id}/transactions`),
  bulkStats: () => get("/api/bridge/bulk-operations"),
  autoCategorize: () => post("/api/bridge/bulk-operations/auto-categorize"),
  autoCategorizeAI: () => post("/api/bridge/bulk-operations/auto-categorize-ai"),
  previewCategorization: () => post("/api/bridge/bulk-operations/preview-categorization"),
  bulkSync: () => post("/api/bridge/bulk-operations/sync-to-erpnext"),
  classify: (transaction) => post("/api/bridge/classify", { transaction }),
  categorize: (id, category_id) => post(`/api/bridge/transactions/${id}/categorize`, { category_id }),
  uncategorize: (id) => post(`/api/bridge/transactions/${id}/uncategorize`),

  // erpnext
  testConfig: (id) => post(`/api/erpnext/configs/${id}/test`),
  activateConfig: (id) => post(`/api/erpnext/configs/${id}/activate`),
  syncTransaction: (id) => post(`/api/erpnext/transactions/${id}/sync`),
  erpAccounts: () => get("/api/erpnext/fetch-accounts"),
  erpCostCenters: () => get("/api/erpnext/fetch-cost-centers"),
  erpCompanies: () => get("/api/erpnext/fetch-companies"),
  updateErpDefaults: (b) => post("/api/erpnext/update-config-defaults", b),
  preflight: () => get("/api/erpnext/sync-preflight"),
  submitPreflight: (b) => post("/api/erpnext/sync-preflight", b),
  syncNow: () => post("/api/erpnext/sync-now"),
  syncJobStatus: () => get("/api/erpnext/sync-job-status"),

  // gmail + uploads
  gmailStatus: () => get("/api/gmail/status"),
  gmailConnect: () => get("/api/gmail/connect"),
  gmailDisconnect: () => post("/api/gmail/disconnect"),
  importStatements: () => post("/api/gmail/statements/import"),
  parseStatement: (id, b) => post(`/api/gmail/statements/${id}/parse`, b),
  parseCsvStatement: (id) => post(`/api/gmail/statements/${id}/parse-csv`),
  uploadCsv: (fd) => post("/api/gmail/upload-csv", fd),
  bulkCsv: (fd) => post("/api/gmail/bulk-csv-import", fd),
  uploadPdf: (fd) => post("/api/gmail/upload-pdf", fd),
  pdfStatus: (id) => get(`/api/gmail/pdf-jobs/${id}/status`),
  csvTemplate: () => request("/api/gmail/download-csv-template", { raw: true }),

  // erpnext invoices
  erpInvoices: (params) => get(`/api/erp-invoices${qs(params)}`),
  erpInvoiceCounts: () => get("/api/erp-invoices/counts"),
  syncErpInvoices: (year, month) => post("/api/erp-invoices/sync", { year, month }),

  // reconciliation
  reconMonths: () => get("/api/reconciliation/months"),
  reconPeriods: () => get("/api/reconciliation/periods"),
  reconMonth: (y, m, status = "") => get(`/api/reconciliation/month/${y}/${m}${qs({ status })}`),
  reconFetch: (y, m) => post(`/api/reconciliation/month/${y}/${m}/fetch`),
  reconMatch: (y, m) => post(`/api/reconciliation/month/${y}/${m}/match`),
  reconClose: (y, m) => post(`/api/reconciliation/month/${y}/${m}/close`),
  reconReopen: (y, m) => post(`/api/reconciliation/month/${y}/${m}/reopen`),
  reconExport: (y, m) => request(`/api/reconciliation/month/${y}/${m}/export`, { raw: true }),
  manualMatch: (txnId, journal_entry_id) => post(`/api/reconciliation/transactions/${txnId}/match`, { journal_entry_id }),
  unmatch: (txnId) => post(`/api/reconciliation/transactions/${txnId}/unmatch`),
};
