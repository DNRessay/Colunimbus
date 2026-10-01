// Chart.js helpers in the red / blue / white theme, and a small Markdown renderer for AI answers.
import { esc, money } from "/js/ui.js";

export const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
export const PALETTE = ["#17244e", "#8c0b10", "#1f3f8f", "#e0a23c", "#6b8ad1", "#c4474d", "#94a3b8", "#0071c5"];
export const randTick = (v) => "R " + Number(v).toLocaleString("en-ZA", { notation: "compact", maximumFractionDigits: 1 });
export const monthLabel = (m) => new Date(m + "-01").toLocaleDateString("en-ZA", { month: "short", year: "2-digit" });
const charts = {};

export function chart(id, config) {
  if (!window.Chart) return;
  charts[id]?.destroy();
  Chart.defaults.color = css("--muted");
  Chart.defaults.borderColor = css("--border");
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  config.options = { maintainAspectRatio: false, ...config.options };
  config.options.plugins = { legend: { display: false }, ...config.options.plugins,
    tooltip: { backgroundColor: "#17244e", titleColor: "#fff", bodyColor: "#fff", padding: 10,
      callbacks: { label: (c) => ` ${c.dataset.label || c.label}: ${money(Math.abs(c.parsed.y ?? c.parsed.x ?? c.parsed))}` },
      ...(config.options.plugins?.tooltip || {}) } };
  charts[id] = new Chart(document.querySelector(id), config);
}

export function inOutChart(id, months, ins, outs) {
  chart(id, { type: "bar", data: { labels: months.map(monthLabel), datasets: [
    { label: "Money in", data: ins, backgroundColor: css("--accent"), borderRadius: 4, maxBarThickness: 22 },
    { label: "Money out", data: outs, backgroundColor: css("--red"), borderRadius: 4, maxBarThickness: 22 }] },
    options: { interaction: { mode: "index", intersect: false },
      scales: { x: { grid: { display: false } }, y: { ticks: { callback: randTick, maxTicksLimit: 5 } } } } });
}

export function donut(id, legendId, items) {
  const top = items.slice(0, 6), rest = items.slice(6).reduce((a, x) => a + x.amount, 0);
  const data = rest > 0 ? [...top, { name: "Everything else", amount: rest }] : top;
  const total = data.reduce((a, x) => a + x.amount, 0) || 1;
  chart(id, { type: "doughnut", data: { labels: data.map(x => x.name), datasets: [{ data: data.map(x => x.amount),
    backgroundColor: PALETTE, borderColor: css("--panel"), borderWidth: 2 }] },
    options: { cutout: "62%", plugins: { tooltip: { callbacks: { label: (c) => ` ${c.label}: ${money(c.parsed)} (${(c.parsed / total * 100).toFixed(0)}%)` } } } } });
  document.querySelector(legendId).innerHTML = data.map((x, i) => `<div class="leg-row"><i style="background:${PALETTE[i % PALETTE.length]}"></i>
    <span>${esc(x.name)}</span><b>${money(x.amount)}</b><small class="muted">${(x.amount / total * 100).toFixed(0)}%</small></div>`).join("");
}

// Markdown → HTML (escaped first): headings, tables, lists, bold/italic, code, links.
export function md(src) {
  const inline = (t) => esc(t).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const lines = String(src || "").replace(/\r/g, "").split("\n");
  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l), cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
  let html = "", i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (isRow(l) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      const head = cells(l); i += 2; let body = "";
      while (i < lines.length && isRow(lines[i])) body += `<tr>${cells(lines[i++]).map(c => `<td>${inline(c)}</td>`).join("")}</tr>`;
      html += `<div class="table-wrap"><table><thead><tr>${head.map(h => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`;
      continue;
    }
    const h = l.match(/^\s*(#{1,4})\s+(.*)$/);
    if (h) { html += `<h4>${inline(h[2])}</h4>`; i++; continue; }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(l)) {
      const tag = /^\s*\d/.test(l) ? "ol" : "ul"; let items = "";
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) items += `<li>${inline(lines[i++].replace(/^\s*([-*•]|\d+[.)])\s+/, ""))}</li>`;
      html += `<${tag}>${items}</${tag}>`; continue;
    }
    if (!l.trim()) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !isRow(lines[i]) && !/^\s*(#{1,4}\s|[-*•]\s|\d+[.)]\s)/.test(lines[i])) para.push(lines[i++]);
    html += `<p>${para.map(inline).join("<br>")}</p>`;
  }
  return html;
}

// "AI review" box: Groq looks at the numbers on this page (cached for the same numbers).
export function aiReview(boxSel, payload, api) {
  const box = document.querySelector(boxSel);
  const LV = { high: "risk", medium: "books", low: "grow" };
  box.innerHTML = `<div class="ai-head"><h3><span class="cl-icon" aria-hidden="true">i</span> AI review</h3>
    <button type="button" class="small secondary ai-run">Review with AI</button></div><p class="muted">Groq checks these numbers for risks and what to do next.</p>`;
  box.querySelector(".ai-run").addEventListener("click", async (e) => {
    e.target.disabled = true;
    box.querySelector("p").textContent = "Reviewing…";
    try {
      const r = await api.review(payload());
      box.querySelector("p").outerHTML = `<ol class="ai-list">${r.items.map(i => `<li><span class="kind ${LV[i.level]}">${i.level}</span>
        <strong>${esc(i.title)}</strong><br>${esc(i.detail)}</li>`).join("")}</ol><p class="muted"><small>Groq${r.cached ? " (saved answer)" : ""}. Not accounting or tax advice.</small></p>`;
    } catch (err) { box.querySelector("p").textContent = err.message; e.target.disabled = false; }
  });
}
