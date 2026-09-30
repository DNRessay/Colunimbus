import { API_BASE } from "./config.js";

const ACCESS = "colunimbus_access";
const REFRESH = "colunimbus_refresh";
const COMPANY = "colunimbus_company";

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

// The company (client) every company-scoped call is about; sent as X-Client-Id.
export const company = {
  get id() { return store.get(COMPANY); },
  set(id) { store.set(COMPANY, id ? String(id) : null); },
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
  const res = await fetch(`${API_BASE}/api/auth/token/refresh`, {
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
  if (company.id) headers["X-Client-Id"] = company.id;
  let res;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method, headers, body: body === undefined || body instanceof FormData ? body : JSON.stringify(body),
    });
  } catch {
    throw new Error("Can't reach the server. Check your connection.");
  }
  if (res.status === 404 && company.id && (await res.clone().json().catch(() => ({}))).detail === "Client not found.") {
    company.set(null);  // stale selection, e.g. company deleted
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
  // auth + organisation
  register: (b) => post("/api/auth/register", b),
  login: (username, password) => post("/api/auth/login", { username, password }),
  logout: () => post("/api/auth/logout"),
  me: () => get("/api/auth/me"),
  updateMe: (b) => patch("/api/auth/me", b),
  changePassword: (b) => post("/api/auth/change-password", b),
  requestReset: (email) => post("/api/auth/password-reset", { email }),
  confirmReset: (b) => post("/api/auth/password-reset/confirm", b),
  socialProviders: () => get("/auth/social/providers"),
  googleLoginUrl: () => `${API_BASE}/auth/social/google/login`,
  practice: () => get("/api/practice"),
  updatePractice: (b) => patch("/api/practice", b),
  team: () => get("/api/practice/users"),
  addMember: (b) => post("/api/practice/users", b),
  removeMember: (id) => del(`/api/practice/users/${id}`),

  // companies
  companies: () => get("/api/clients"),
  createCompany: (b) => post("/api/clients", b),
  updateCompany: (id, b) => patch(`/api/clients/${id}`, b),
  deleteCompany: (id) => del(`/api/clients/${id}`),
  dashboard: () => get("/api/dashboard"),
  intercompany: () => get("/api/intercompany"),
  confirmIntercompany: (out_id, in_id) => post("/api/intercompany/confirm", { out_id, in_id }),

  // bank data (selected company)
  accounts: () => get("/api/accounts"),
  createAccount: (b) => post("/api/accounts", b),
  updateAccount: (id, b) => patch(`/api/accounts/${id}`, b),
  deleteAccount: (id) => del(`/api/accounts/${id}`),
  categories: () => get("/api/categories"),
  createCategory: (b) => post("/api/categories", b),
  updateCategory: (id, b) => patch(`/api/categories/${id}`, b),
  deleteCategory: (id) => del(`/api/categories/${id}`),
  statements: () => get("/api/statements"),
  inbox: () => get("/api/statements?unassigned=1"),
  assignStatement: (id, b) => patch(`/api/statements/${id}`, b),
  deleteStatement: (id) => del(`/api/statements/${id}`),
  transactions: (params) => get(`/api/transactions${qs(params)}`),
  createTransaction: (b) => post("/api/transactions", b),
  updateTransaction: (id, b) => patch(`/api/transactions/${id}`, b),
  deleteTransaction: (id) => del(`/api/transactions/${id}`),
  pdfJobs: () => get("/api/pdf-jobs"),
  job: (id) => get(`/api/jobs/${id}`),

  // categorization
  categoryStats: () => get("/api/bridge/categories"),
  categoryTransactions: (id) => get(`/api/bridge/categories/${id}/transactions`),
  bulkStats: () => get("/api/bridge/bulk-operations"),
  autoCategorize: () => post("/api/bridge/bulk-operations/auto-categorize"),
  autoCategorizeAI: () => post("/api/bridge/bulk-operations/auto-categorize-ai"),
  previewCategorization: () => post("/api/bridge/bulk-operations/preview-categorization"),
  classify: (transaction) => post("/api/bridge/classify", { transaction }),
  categorize: (id, category_id) => post(`/api/bridge/transactions/${id}/categorize`, { category_id }),
  uncategorize: (id) => post(`/api/bridge/transactions/${id}/uncategorize`),

  // erpnext
  erpnextConfigs: () => get("/api/erpnext-configs"),
  createErpnextConfig: (b) => post("/api/erpnext-configs", b),
  updateErpnextConfig: (id, b) => patch(`/api/erpnext-configs/${id}`, b),
  deleteErpnextConfig: (id) => del(`/api/erpnext-configs/${id}`),
  testConfig: (id) => post(`/api/erpnext/configs/${id}/test`),
  erpCompanies: () => get("/api/erpnext/companies"),
  erpAccounts: () => get("/api/erpnext/accounts"),
  erpCostCenters: () => get("/api/erpnext/cost-centers"),
  syncTransaction: (id) => post(`/api/erpnext/transactions/${id}/sync`),
  preflight: () => get("/api/erpnext/sync-preflight"),
  submitPreflight: (b) => post("/api/erpnext/sync-preflight", b),
  syncJobStatus: () => get("/api/erpnext/sync-job-status"),
  syncLogs: () => get("/api/erpnext-sync-logs?limit=50"),

  // imports
  gmailStatus: () => get("/api/imports/gmail/status"),
  gmailConnect: () => get("/api/imports/gmail/connect"),
  gmailDisconnect: () => post("/api/imports/gmail/disconnect"),
  gmailFetch: () => post("/api/imports/gmail/fetch"),
  parseStatement: (id, b) => post(`/api/imports/statements/${id}/parse`, b),
  uploadCsv: (fd) => post("/api/imports/csv", fd),
  uploadPdf: (fd) => post("/api/imports/pdf", fd),
  pdfStatus: (id) => get(`/api/imports/pdf/${id}`),
  csvTemplate: () => request("/api/imports/csv-template", { raw: true }),

  // erpnext invoices
  erpInvoices: (params) => get(`/api/erp-invoices${qs(params)}`),
  erpInvoiceCounts: () => get("/api/erp-invoices/counts"),
  syncErpInvoices: (year, month) => post("/api/erp-invoices/sync", { year, month }),

  // reconciliation
  reconMonths: () => get("/api/reconciliation/months"),
  reconMonth: (y, m, status = "") => get(`/api/reconciliation/month/${y}/${m}${qs({ status })}`),
  reconFetch: (y, m) => post(`/api/reconciliation/month/${y}/${m}/fetch`),
  reconMatch: (y, m) => post(`/api/reconciliation/month/${y}/${m}/match`),
  reconClose: (y, m) => post(`/api/reconciliation/month/${y}/${m}/close`),
  reconReopen: (y, m) => post(`/api/reconciliation/month/${y}/${m}/reopen`),
  reconExport: (y, m) => request(`/api/reconciliation/month/${y}/${m}/export`, { raw: true }),
  manualMatch: (txnId, journal_entry_id) => post(`/api/reconciliation/transactions/${txnId}/match`, { journal_entry_id }),
  unmatch: (txnId) => post(`/api/reconciliation/transactions/${txnId}/unmatch`),
};
