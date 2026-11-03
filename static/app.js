const reportMatch = location.pathname.match(/^\/report\/(\d+)\/?$/);
const REPORT_MODE = Boolean(reportMatch);
const desktopToken = document.querySelector('meta[name="geo-desktop-token"]')?.content || "";
const NAV = [
  ["overview", "优化概览", "01"],
  ["brand", "品牌分析", "02"],
  ["brand-monitor", "品牌监测", "03"],
  ["citations", "引用分析", "04"],
  ["monitor", "监测详情", "05"],
  ["input", "录入监测", "+"],
  ["settings", "项目设置", "⚙"],
];
const state = {
  projects: [], project: null, questions: [], observations: [], analytics: null,
  providers: [], browserPlatforms: {ready:false,platforms:[]}, browserAccounts: {ready:false,accounts:[],loading:false},
  page: new URLSearchParams(location.search).get("page") === "input" ? "input" : "overview", chartMetric: "mention_rate", overlay: null,
  editId: null, filters: {platform: "all", question_id: "", start: "", end: ""},
  reportPage: REPORT_MODE,
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}
function n(value) { return Number(value || 0); }
function pct(value) { return value === null ? "待复核" : `${n(value).toFixed(2)}%`; }
function dateCN(value) { return value ? value.replaceAll("-", ".") : "—"; }
function sourceKind(value) { return ({manual:"手工录入（待核验）",csv:"CSV 导入（待核验）",provider:"模型接口采集（待复核）",browser:"网页聊天采集（待复核）",demo:"虚构演示"})[value] || value; }
function sentimentText(value) { return ({positive:"正面",neutral:"中性",negative:"负面",unknown:"未标注"})[value] || value; }
function statusText(value) { return ({complete:"已采集",offline:"离线",error:"失败"})[value] || value; }
function safeUrl(value) { try { const u = new URL(value); return ["http:", "https:"].includes(u.protocol) ? u.href : ""; } catch { return ""; } }
function options(items, selected, field = "value", label = "label") {
  return items.map(item => `<option value="${esc(item[field])}" ${String(item[field]) === String(selected) ? "selected" : ""}>${esc(item[label])}</option>`).join("");
}
function toast(message, error = false) {
  const node = document.getElementById("toast");
  node.innerHTML = `<div class="toast ${error ? "error" : ""}">${esc(message)}</div>`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.innerHTML = ""; }, 4300);
}
async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {...options, headers});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
  return payload;
}
function queryString() {
  const params = new URLSearchParams();
  Object.entries(state.filters).forEach(([key, value]) => { if (value && value !== "all") params.set(key, value); });
  return params.toString();
}
async function loadProject(id) {
  const [project, observations, providers, browserPlatforms] = await Promise.all([
    api(`/api/projects/${id}`), api(`/api/projects/${id}/observations`),
    api("/api/providers"), api("/api/browser-platforms"),
  ]);
  state.project = project;
  state.questions = project.questions || [];
  state.observations = observations;
  state.providers = providers;
  state.browserPlatforms = browserPlatforms;
  state.analytics = await api(`/api/projects/${id}/analytics?${queryString()}`);
  localStorage.setItem("geo-project-id", String(id));
  render();
}
async function refresh() {
  if (!state.project) return;
  await loadProject(state.project.id);
}
async function refreshAnalytics() {
  if (!state.project) return;
  state.analytics = await api(`/api/projects/${state.project.id}/analytics?${queryString()}`);
  render();
}
async function init() {
  try {
    state.projects = await api("/api/projects");
    const selected = REPORT_MODE ? Number(reportMatch[1]) : Number(localStorage.getItem("geo-project-id"));
    const id = state.projects.some(p => p.id === selected) ? selected : state.projects[0]?.id;
    if (id) await loadProject(id); else render();
  } catch (error) {
    document.getElementById("app").innerHTML = `<div class="boot"><h2>暂时无法打开诊断台</h2><p>${esc(error.message)}</p><button class="button" onclick="location.reload()">重试</button></div>`;
  }
}

function heading(title, subtitle, actions = "") {
  return `<div class="page-heading"><div><div class="eyebrow">GEO DIAGNOSTIC / ${esc(title)}</div><h1>${esc(title)}</h1><p>${esc(subtitle)}</p></div><div class="heading-actions">${actions}</div></div>`;
}
function demoBanner() {
  return state.project?.is_demo
    ? `<div class="demo-alert" role="status"><div><strong>虚构演示数据，不能用于品牌判断</strong><p>当前品牌、${n(state.analytics?.metrics?.answers)} 条有效回答、百分比和趋势图都是模拟的。要做真实诊断，请创建自己的品牌项目，再采集或导入真实模型回答。</p></div><button class="button" data-action="new-project">创建真实品牌项目</button></div>`
    : "";
}
function realStartBanner() {
  return !state.project?.is_demo && !state.analytics?.metrics?.answers
    ? `<div class="real-start"><strong>这个项目还没有真实回答</strong><span>先核对问题句，再从目标模型取得完整回答并录入。收到回答前，页面不会把“无数据”显示为 0%。</span><button class="button-secondary" data-nav="input">录入第一条回答</button></div>`
    : "";
}
function empty(title, help = "") {
  return `<div class="center-empty"><strong>${esc(title)}</strong>${esc(help)}</div>`;
}
function panel(title, subtitle, body, extra = "", klass = "") {
  return `<section class="panel ${klass}"><div class="panel-head"><div><h2>${esc(title)}</h2><p>${esc(subtitle)}</p></div>${extra}</div><div class="panel-strap"></div><div class="panel-body">${body}</div></section>`;
}
function metric(title, value, count, denominator, color = "#1768e8", foot = "条有效回答") {
  return `<div class="metric-card ${state.project?.is_demo ? "metric-demo" : ""}" style="--metric-color:${color}"><div class="metric-title">${esc(title)}</div>${state.project?.is_demo ? `<span class="metric-demo-label">虚构演示</span>` : ""}<div class="metric-value">${n(denominator) ? pct(value) : "—"}</div><div class="metric-foot"><span class="metric-number">${n(count)} / ${n(denominator)}</span> ${esc(foot)}</div></div>`;
}
function metricsHtml(data = state.analytics?.metrics) {
  if (!data) return "";
  return `<p class="formula">目标品牌名次已核对 ${n(data.rank_sample)} / ${n(data.answers)} 条。未核对完时，TOP3 和首位占比显示「待复核」。情绪、引用按人工标注汇总。</p><div class="metric-grid">
    ${metric("TOP3 占比", data.top3_rate, data.top3_count, data.answers, "#326ce3")}
    ${metric("品牌提及率", data.mention_rate, data.mentioned, data.answers, "#1782d5")}
    ${metric("首位占比", data.first_rate, data.first_count, data.answers, "#7959d6")}
    ${metric("正面情绪", data.positive_rate, data.positive_count, data.sentiment_sample, "#1aa786", "条已标注提及")}
    ${metric("负面情绪", data.negative_rate, data.negative_count, data.sentiment_sample, "#df6570", "条已标注提及")}
  </div>`;
}
function filterBar(report = false) {
  const platforms = [...new Set(state.observations.map(row => row.platform))].sort();
  const platformOptions = [{value:"all",label:"全部平台"}, ...platforms.map(value => ({value,label:value}))];
  const questionOptions = [{value:"",label:"全部问题句"}, ...state.questions.map(q => ({value:q.id,label:q.text}))];
  if (report) return `<div class="report-filter-bar"><span>筛选</span><select class="field" data-filter="platform">${options(platformOptions, state.filters.platform)}</select><select class="field" data-filter="question_id">${options(questionOptions, state.filters.question_id)}</select><span>已采集 ${n(state.analytics?.metrics?.answers)} 条有效回答</span></div>`;
  return `<div class="filters">
    <div class="filter-field"><label>平台</label><select class="field" data-filter="platform">${options(platformOptions, state.filters.platform)}</select></div>
    <div class="filter-field wide"><label>问题句</label><select class="field" data-filter="question_id">${options(questionOptions, state.filters.question_id)}</select></div>
    <div class="filter-field"><label>开始日期</label><input class="field" type="date" data-filter="start" value="${esc(state.filters.start)}"></div>
    <div class="filter-field"><label>结束日期</label><input class="field" type="date" data-filter="end" value="${esc(state.filters.end)}"></div>
    <button class="button-ghost" data-action="clear-filters">重置</button>
    <span class="filter-count">${n(state.analytics?.metrics?.answers)} 条有效回答 · ${n(state.analytics?.metrics?.offline)} 条离线或失败</span>
  </div>`;
}
function lineChart(rows, key = state.chartMetric) {
  if (rows?.some(row => row[key] === null)) return empty("名次待复核", "核对当前样本的名次后再比较排名趋势。");
  if (!rows?.length || !rows.some(row => row.answers)) return empty("还没有趋势数据", "导入不同日期的模型回答后，这里会显示变化。");
  const width = 760, height = 220, left = 38, right = 735, top = 20, bottom = 180;
  const points = rows.map((row, index) => {
    const x = rows.length === 1 ? (left + right) / 2 : left + index * (right - left) / (rows.length - 1);
    const y = bottom - n(row[key]) * (bottom - top) / 100;
    return {x, y, label: row.date, value: row[key]};
  });
  const line = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
  const area = `${line} L${points.at(-1).x.toFixed(1)},${bottom} L${points[0].x.toFixed(1)},${bottom} Z`;
  return `<div class="chart-wrap"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(key)}趋势图">
    <defs><linearGradient id="chartArea" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#3b86ed" stop-opacity=".2"/><stop offset="100%" stop-color="#3b86ed" stop-opacity="0"/></linearGradient></defs>
    ${[0,25,50,75,100].map(value => { const y = bottom - value * (bottom-top)/100; return `<line x1="${left}" x2="${right}" y1="${y}" y2="${y}" stroke="#e9eef6"/><text x="6" y="${y+4}" fill="#a0aec0" font-size="10">${value}</text>`; }).join("")}
    <path d="${area}" fill="url(#chartArea)"/><path d="${line}" fill="none" stroke="#2670dc" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
    ${points.map(point => `<circle cx="${point.x}" cy="${point.y}" r="4" fill="#fff" stroke="#2670dc" stroke-width="2"><title>${esc(point.label)} · ${pct(point.value)}</title></circle>`).join("")}
    ${points.filter((_, index) => index === 0 || index === points.length-1 || index % Math.max(1,Math.ceil(points.length/6))===0).map(point => `<text x="${point.x}" y="209" text-anchor="middle" fill="#98a7ba" font-size="10">${esc(point.label.slice(5))}</text>`).join("")}
  </svg></div>`;
}
function trendPanel() {
  const tabs = [["mention_rate","提及率"],["top3_rate","TOP3"],["first_rate","首位"]]
    .map(([key,label]) => `<button class="chart-tab ${state.chartMetric === key ? "active" : ""}" data-action="chart" data-value="${key}">${label}</button>`).join("");
  return panel(state.project?.is_demo ? "指标趋势（虚构演示）" : "指标趋势", "按采集日期查看品牌表现", lineChart(state.analytics.trend), `<div class="chart-tabs">${tabs}</div>`);
}
function keywordTable(limit = 10) {
  if (!state.analytics.metrics.answers) return empty("尚无回答数据", "采集行业问题句的真实回答后，这里会显示结果。");
  const rows = state.analytics.by_question.filter(row => row.kind === "keyword").slice(0, limit);
  if (!rows.length) return empty("还没有关键词监测数据", "先录入品牌以外的行业问题句与模型回答。");
  return `<div class="table-wrap"><table class="data-table"><thead><tr><th>问题句</th><th>有效回答</th><th>TOP3</th><th>提及率</th><th>首位</th><th></th></tr></thead><tbody>
    ${rows.map(row => `<tr><td><strong>${esc(row.question)}</strong></td><td>${row.answers}</td><td class="num">${pct(row.top3_rate)}</td><td class="num">${pct(row.mention_rate)}</td><td class="num">${pct(row.first_rate)}</td><td><button class="link-button" data-action="question-filter" data-id="${row.question_id}">查看</button></td></tr>`).join("")}
  </tbody></table></div>`;
}
function competitorTable(limit = 10) {
  if (!state.analytics.metrics.answers) return empty("尚无回答数据", "采集真实回答并补充竞品名次后，这里会显示比较结果。");
  const rows = state.analytics.competitors.slice(0, limit);
  if (!rows.length) return empty("还没有竞品", "在项目设置中添加竞品，录入其提及与排名后即可比较。");
  return `<div class="table-wrap"><table class="data-table"><thead><tr><th>竞品</th><th>TOP3 占比</th><th>提及率</th><th>首位占比</th></tr></thead><tbody>
    ${rows.map((row,index) => `<tr><td><span class="pill-count">${String(index+1).padStart(2,"0")}</span> <strong>${esc(row.name)}</strong></td><td class="num">${pct(row.top3_rate)}<div class="rank-bar"><i style="width:${Math.min(100,n(row.top3_rate))}%"></i></div></td><td class="num">${pct(row.mention_rate)}</td><td class="num">${pct(row.first_rate)}</td></tr>`).join("")}
  </tbody></table></div>`;
}
function sourcesHtml(limit = 10) {
  const rows = state.analytics.sources.slice(0, limit);
  if (!rows.length) return empty("还没有信源记录", "录入模型回答引用的文章或页面后，这里会显示分布。");
  return rows.map((row,index) => `<div class="source-row"><span class="index">${index+1}</span><span class="name">${esc(row.source)}</span><span class="value">${pct(row.share)}</span><div class="rank-bar"><i style="width:${Math.min(100,n(row.share)*2.2)}%"></i></div></div>`).join("");
}
function articleHtml(limit = 30) {
  const rows = state.analytics.articles.slice(0, limit);
  if (!rows.length) return empty("还没有文章引用", "为监测记录添加信源标题、来源与网址。");
  return `<div class="article-list">${rows.map(row => `<div class="article-row"><div><div class="article-title">${safeUrl(row.url) ? `<a href="${esc(safeUrl(row.url))}" target="_blank" rel="noopener noreferrer">${esc(row.title)}</a>` : esc(row.title)}</div><span class="article-meta">${esc(row.source)} · ${row.count} 次引用</span></div><span class="article-count">${pct(row.share)}</span></div>`).join("")}</div>`;
}
function summarySentence() {
  const m = state.analytics.metrics;
  if (!m.answers) return "还没有有效回答。先录入模型原文，报告会自动汇总品牌曝光、情绪和引用来源。";
  if (m.top3_rate === null) return `本期共 ${m.answers} 条有效回答，目标品牌被提及 ${m.mentioned} 次。名次已核对 ${n(m.rank_sample)} / ${m.answers} 条，TOP3 与首位占比待复核；情绪和引用也需检查原回答后补充。品牌词提问与不带品牌名的品类提问应分别解读。`;
  const gap = m.top3_benchmark === null ? null : m.top3_benchmark - m.top3_rate;
  return `${state.project?.is_demo ? "【虚构演示】" : ""}本期共 ${m.answers} 条有效回答，目标品牌被提及 ${m.mentioned} 次，其中 ${m.top3_count} 次进入前三位。${state.analytics.competitors.length >= 3 && gap !== null ? `与竞品第三位的 TOP3 基准相比，${gap > 0 ? `低 ${pct(gap)}` : `高 ${pct(-gap)}`}。` : ""}结果仅描述本轮样本，可回查具体问题句和回答原文。`;
}

function overviewPage() {
  const m = state.analytics.metrics;
  return `${heading("优化概览", "GEO 品牌曝光与竞品表现", `<a class="button-ghost" href="/report/${state.project.id}">查看报告</a>${state.project.is_demo ? `<button class="button" data-action="new-project">创建真实品牌项目</button>` : `<button class="button" data-nav="input">新增监测</button>`}`)}
    ${demoBanner()}${realStartBanner()}${filterBar()}
    <div class="note-banner"><span>↗</span><div>${esc(summarySentence())}</div></div>
    ${metricsHtml()}
    <div class="section-grid">${trendPanel()}${panel("样本说明", "让每个百分比都能复核", `<div class="insight-box"><b>统计口径</b><br>提及率、TOP3 和首位占比以当前筛选下的 <b>${m.answers}</b> 条有效回答为分母；离线与失败记录单独保留。情绪占比以 <b>${m.sentiment_sample}</b> 条已标注且提及目标品牌的回答为分母。</div><div class="formula">名称出现在回答里只计为提及；进入前三位或首位需要记录明确排名。样本越少，百分比波动越大。</div>`)}
    </div>
    <div class="section-grid">${panel("关键词详情", "所有行业问题句的核心表现", keywordTable())}${panel("竞对排名", "各竞品的模型推荐位置", competitorTable())}</div>`;
}

function sentimentDonut() {
  const m = state.analytics.metrics;
  if (!m.sentiment_sample) return `<div class="center-empty"><strong>暂无情绪数据</strong>录入并标注提及品牌的回答后，这里会显示占比。</div>`;
  const positive = Math.max(0, Math.min(100, n(m.positive_rate)));
  const negative = Math.max(0, Math.min(100-positive, n(m.negative_rate)));
  return `<div class="donut" style="background:conic-gradient(#1dbb96 0 ${positive}%, #ef8490 ${positive}% ${positive+negative}%, #e6edf6 ${positive+negative}% 100%)"><div class="donut-label"><strong>${m.sentiment_sample}</strong><small>条已标注提及</small></div></div><div class="legend" style="justify-content:center"><span style="--dot:#1dbb96">正面 ${pct(positive)}</span><span style="--dot:#ef8490">负面 ${pct(negative)}</span><span style="--dot:#e6edf6">中性</span></div>`;
}
function termsHtml(terms, negative = false) {
  if (!terms.length) return `<span style="color:#9cabbc;font-size:11px">暂无人工标注的高频词</span>`;
  return `<div class="term-list">${terms.slice(0,24).map(row => `<span class="term ${negative ? "negative" : ""}" title="出现 ${row.count} 次">${esc(row.term)} <small>×${row.count}</small></span>`).join("")}</div>`;
}
function brandPage() {
  const relevant = state.analytics.observations.filter(row => row.question_type === "brand" && row.brand_mentioned).slice(0,8);
  return `${heading("品牌分析", "品牌情绪、高频词与回答证据", `<a class="button-ghost" href="/report/${state.project.id}#sentiment">查看报告</a>`)}${demoBanner()}${filterBar()}
    <div class="section-grid">${panel("品牌情绪占比", "仅统计已标注且提及目标品牌的回答", sentimentDonut())}${panel("本期解读", "从样本回到原始回答", `<div class="insight-box">${esc(summarySentence())}</div><p class="formula">情绪由录入者标注。未标注的回答不会被自动判断为正面或负面。</p>`)}</div>
    ${panel("高频信息点", "词语来自录入时的正负面标注", `<div class="term-grid"><div class="term-box"><h3>正面高频词</h3>${termsHtml(state.analytics.positive_terms)}</div><div class="term-box"><h3>负面高频词</h3>${termsHtml(state.analytics.negative_terms,true)}</div></div>`)}
    <div style="height:17px"></div>${panel("模型回答摘录", "点击可查看完整回答及信源", relevant.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>问题句</th><th>平台</th><th>情绪</th><th>排名</th><th>时间</th></tr></thead><tbody>${relevant.map(row => `<tr><td><button class="link-button" data-action="detail" data-id="${row.id}">${esc(row.question)}</button></td><td>${esc(row.platform)}</td><td>${sentimentChip(row.sentiment)}</td><td>${row.brand_rank ? `第 ${row.brand_rank} 位` : "提及"}</td><td>${esc(row.observed_at)}</td></tr>`).join("")}</tbody></table></div>` : empty("暂无品牌回答", "使用品牌词问题句采集模型回复。"))}`;
}
function sentimentChip(value) {
  const klass = value === "positive" ? "green" : value === "negative" ? "red" : "gray";
  return `<span class="chip ${klass}">${esc(sentimentText(value))}</span>`;
}
function statusChip(value) {
  const klass = value === "complete" ? "green" : value === "error" ? "red" : "gray";
  return `<span class="chip ${klass}">${esc(statusText(value))}</span>`;
}
function observationTable(rows, brand = false) {
  if (!rows.length) return empty("当前筛选下没有监测记录", "调整时间与平台，或录入新的模型回答。");
  return `<div class="table-wrap"><table class="data-table"><thead><tr><th>核心问题句</th><th>${brand ? "品牌词" : "关键词"}</th><th>平台</th><th>目标品牌</th><th>排名</th><th>时间</th><th>状态</th></tr></thead><tbody>
    ${rows.map(row => `<tr><td><button class="link-button" data-action="detail" data-id="${row.id}">${esc(row.question)}</button></td><td>${esc(brand ? state.project.brand : state.project.category)}</td><td>${esc(row.platform)}</td><td>${row.brand_mentioned ? `<span class="chip green">已提及</span>` : `<span class="chip gray">未提及</span>`}</td><td class="num">${row.brand_rank ? `第 ${row.brand_rank} 位` : "—"}</td><td>${esc(row.observed_at)}</td><td>${statusChip(row.status)}</td></tr>`).join("")}
  </tbody></table></div>`;
}
function monitoringPage(brand) {
  const title = brand ? "品牌监测" : "监测详情";
  const subtitle = brand ? "品牌词问题句的模型回复记录" : "行业问题句的模型回复与推荐位置";
  const rows = state.analytics.observations.filter(row => row.question_type === (brand ? "brand" : "keyword"));
  return `${heading(title, subtitle, `<button class="button" data-nav="input">新增记录</button>`)}${demoBanner()}${filterBar()}
    ${panel("问题句监测", `当前筛选下共 ${rows.length} 条记录`, observationTable(rows,brand))}`;
}
function citationPage() {
  const total = state.analytics.sources.reduce((sum,row) => sum + row.count, 0);
  return `${heading("引用分析", "信源媒体分布与文章引用详情", `<a class="button-ghost" href="/report/${state.project.id}#sources">查看报告</a>`)}${demoBanner()}${filterBar()}
    <div class="section-grid">${panel("信源媒体分布", `共 ${total} 次引用 · ${state.analytics.sources.length} 个来源`, sourcesHtml(15))}${panel("信源提示", "引用数量与品牌提及分开统计", `<div class="insight-box">引用来源表反映模型回答中出现过哪些链接或媒体。来源出现次数并不代表内容可信度或品牌推荐强度；点击具体文章后应回看原文。</div><p class="formula">只统计当前筛选下、状态为“已采集”的回答所记录的引用。</p>`)}</div>
    ${panel("文章引用列表", `去重后共 ${state.analytics.articles.length} 篇`, articleHtml(50))}`;
}

function observationForm() {
  const edit = state.editId ? state.observations.find(row => row.id === state.editId) : null;
  const today = new Date().toLocaleDateString("sv-SE", {timeZone:"Asia/Shanghai"});
  const citationLines = edit?.citations.map(c => [c.title,c.source,c.url].join(" | ")).join("\n") || "";
  return `<form id="observation-form"><div class="form-grid">
    <div class="form-field full"><label>问题句 *</label><select class="field" name="question_id" required><option value="">请选择问题句</option>${options(state.questions.map(q => ({value:q.id,label:`${q.kind === "brand" ? "品牌词" : "品类词"} · ${q.text}`})), edit?.question_id)}</select></div>
    <div class="form-field"><label>平台 *</label><select class="field" name="platform">${options(["DeepSeek","DP","豆包","Kimi","千问","文心一言","元宝","其他"].map(value => ({value,label:value})),edit?.platform || "豆包")}</select></div>
    <div class="form-field"><label>采集日期 *</label><input class="field" type="date" name="observed_at" required value="${esc(edit?.observed_at || today)}"></div>
    <div class="form-field"><label>采集状态</label><select class="field" name="status">${options([{value:"complete",label:"已采集"},{value:"offline",label:"离线"},{value:"error",label:"失败"}],edit?.status || "complete")}</select></div>
    <div class="form-field"><label>是否提及目标品牌</label><select class="field" name="brand_mentioned">${options([{value:"auto",label:"根据品牌名称自动识别"},{value:"true",label:"已提及"},{value:"false",label:"未提及"}],edit ? String(edit.brand_mentioned) : "auto")}</select></div>
    <div class="form-field full"><label>模型回答原文 *</label><textarea class="field" name="answer" rows="6" placeholder="粘贴模型的完整回复，离线或失败记录可留空">${esc(edit?.answer || "")}</textarea><small>保留原文，后续可逐条核对统计结果。</small></div>
    <div class="form-field full"><label>模型会话分享链接</label><input class="field" type="url" name="evidence_url" value="${esc(edit?.evidence_url || "")}" placeholder="https://..."><small>有分享链接时填入；没有链接请另存当时的页面截图。链接本身不代表回答已核验。</small></div>
    <div class="form-field"><label>目标品牌推荐名次</label><input class="field" name="brand_rank" type="number" min="1" max="100" value="${esc(edit?.brand_rank || "")}" placeholder="没有明确名次则留空"></div>
    <div class="form-field"><label><input type="checkbox" name="rank_reviewed" ${edit?.rank_reviewed?'checked':''}> 已核对目标品牌名次</label><small>没有明确推荐名次也可留空并勾选。未核对不代表没有进入前三。</small></div>
    <div class="form-field"><label>情绪标注</label><select class="field" name="sentiment">${options([{value:"unknown",label:"未标注"},{value:"positive",label:"正面"},{value:"neutral",label:"中性"},{value:"negative",label:"负面"}],edit?.sentiment || "unknown")}</select></div>
    <div class="form-field full"><label>竞品推荐名次（每行：名称 | 名次）</label><textarea class="field" name="competitor_ranks" rows="2" placeholder="竞品甲 | 2">${esc(edit ? Object.entries(edit.competitor_ranks).map(([name,rank])=>`${name} | ${rank || "无明确名次"}`).join("\n") : "")}</textarea><small>名称出现在原文中会计入提及；TOP3 与首位需要明确名次。</small></div>
    <div class="form-field full"><label>引用文章（每行：标题 | 来源 | URL）</label><textarea class="field" name="citations" rows="3" placeholder="文章标题 | 媒体名称 | https://...">${esc(citationLines)}</textarea></div>
    <div class="form-field"><label>正面信息点</label><input class="field" name="positive_terms" value="${esc(edit?.positive_terms.join("，") || "")}" placeholder="用逗号隔开"></div>
    <div class="form-field"><label>负面信息点</label><input class="field" name="negative_terms" value="${esc(edit?.negative_terms.join("，") || "")}" placeholder="用逗号隔开"></div>
  </div><div class="form-actions">${edit ? `<button type="button" class="button-ghost" data-action="cancel-edit">取消编辑</button>` : ""}<button class="button" type="submit">${edit ? "保存修改" : "保存监测记录"}</button></div></form>`;
}
function inputPage() {
  if (state.project.is_demo) return `${heading("录入监测", "真实回答请放在新项目中")} ${demoBanner()}<div class="real-start"><strong>演示项目只读</strong><span>这里的记录全是虚构样本。创建自己的品牌项目后，再录入模型原文或导入 CSV，避免两类数据混算。</span><button class="button-secondary" data-action="new-project">创建真实品牌项目</button></div>`;
  const rows = state.observations.slice().sort((a,b) => b.observed_at.localeCompare(a.observed_at) || b.id-a.id).slice(0,20);
  const providerOptions = state.providers.filter(p => p.ready);
  const localBrowser = state.browserPlatforms.runtime === "local";
  const eligibleAccounts = state.browserAccounts.accounts.filter(a => a.can_collect && Object.values(a.platforms||{}).some(Boolean));
  const firstAccount = eligibleAccounts[0];
  const accountId = eligibleAccounts.some(a => a.id === state.browserAccounts.selected) ? state.browserAccounts.selected : firstAccount?.id;
  const account = eligibleAccounts.find(a => a.id === accountId);
  const selectedWebsites = account ? Object.entries(account.platforms).filter(([,ok]) => ok).map(([id]) => ({deepseek:"DeepSeek",doubao:"豆包",kimi:"Kimi"}[id])).filter(Boolean) : [];
  let browserPanel = panel("网页聊天采集", "在采集机的模型网页上逐题提问，并保存原文、时间和截图",
    state.browserPlatforms.ready ? `<form id="browser-run-form"><div class="form-grid">
      <div class="form-field full"><label>账号归属</label><div style="display:flex;gap:8px;align-items:center"><select class="field" name="participant_id" required ${eligibleAccounts.length?'':'disabled'}>${options(eligibleAccounts.map(a=>({value:a.id,label:`${a.name} · 授权有效`})),accountId,'value','label')}</select><button class="button-ghost" type="button" data-action="refresh-browser-accounts" ${state.browserAccounts.loading?'disabled':''}>${state.browserAccounts.loading?'正在加载':'刷新已授权账号'}</button></div><small>只会显示本人已授权 GEO、并在登录中心确认过的平台账号。</small></div>
      <div class="form-field"><label>网页平台</label><select class="field" name="platform" required ${selectedWebsites.length?'':'disabled'}>${options(selectedWebsites.map(name => ({value:name,label:name})),selectedWebsites[0])}</select></div>
      <div class="form-field full"><label>选择问题句（每次最多 3 条）</label><div style="max-height:180px;overflow:auto;border:1px solid var(--line);border-radius:9px;padding:8px">${state.questions.map(q => `<label style="display:flex;gap:7px;padding:5px;font-size:11px"><input type="checkbox" name="question_ids" value="${q.id}">${esc(q.text)}</label>`).join("")}</div></div>
    </div><div class="form-actions"><button class="button-secondary" type="submit" ${eligibleAccounts.length&&selectedWebsites.length?'':'disabled'}>开始网页采集</button></div></form><p class="formula">每批最多 3 条。提交前会再次显示账号并请求确认；授权用户可随时在登录中心撤销。网页可能要求重新登录或出现限流；失败会留在明细，不计入有效回答。</p>`
      : `<div class="center-empty"><strong>采集机尚未连接</strong>配置采集机后，才能从模型网页取得真实回答。当前仍可手工录入。</div>`);
  if (localBrowser) browserPanel = panel("本机浏览器采集", "在这台电脑的 Chrome 或 Edge 中提问，回答直接进入当前项目",
    `<p class="formula">先<a href="/login-center" target="_blank" rel="noopener">打开平台并登录</a>，再选择问题开始。无需关闭浏览器。采集时请暂时不要操作聊天页。</p>` +
    (state.browserPlatforms.ready ? `<form id="browser-run-form"><div class="form-grid"><div class="form-field"><label>网页平台</label><select class="field" name="platform" required>${options(state.browserPlatforms.platforms.map(name=>({value:name,label:name})),"DeepSeek")}</select></div><div class="form-field full"><label>选择问题句（每次最多 3 条）</label><div style="max-height:230px;overflow:auto;border:1px solid var(--line);border-radius:9px;padding:8px">${state.questions.map((q,index)=>`<label style="display:flex;gap:7px;padding:5px;font-size:12px"><input type="checkbox" name="question_ids" value="${q.id}" ${index===0?"checked":""}>${esc(q.text)}</label>`).join("")}</div></div></div><div class="form-actions"><button class="button-secondary" type="submit">开始提问并保存回答</button></div></form><p class="formula">每题打开新的聊天页，保留原文、时间、会话地址和截图。登录或验证中断时会停止本批次，失败不计入有效样本；名次、情绪及信源仍需复核。</p>` : `<div class="center-empty">本机浏览器依赖尚未就绪。</div>`));
  return `${heading("录入监测", "把模型回答与出处留在同一条可核对的记录里", `<a class="button-ghost" href="/api/projects/${state.project.id}/export.csv">导出 CSV</a>`)}${demoBanner()}
    ${browserPanel}<div style="height:17px"></div><div class="split-panel">${panel(state.editId ? "编辑记录" : "新增记录", "手工录入一条模型回答", observationForm())}
      <div style="display:grid;gap:17px">${panel("批量导入", "使用 CSV 快速加入历史回答", `<form id="import-form" class="upload-zone"><div>文件最多 5 MB、2000 行，按 UTF-8 保存。导入前可下载模板核对列名。</div><input type="file" name="file" accept=".csv,text/csv" required><div><a href="/api/template.csv">下载 CSV 模板</a></div><div class="form-actions"><button class="button-secondary" type="submit">导入数据</button></div></form>`)}
        ${panel("模型接口采集", "可选：连接你已获授权的 OpenAI 兼容接口", providerOptions.length ? `<form id="run-form"><div class="form-field"><label>已配置模型</label><select class="field" name="provider">${options(providerOptions.map(p => ({value:p.name,label:`${p.name} · ${p.model}`})),providerOptions[0].name)}</select></div><div class="form-field" style="margin-top:12px"><label>选择问题句（每次最多 5 条）</label><div style="max-height:230px;overflow:auto;border:1px solid var(--line);border-radius:9px;padding:8px">${state.questions.map(q => `<label style="display:flex;gap:7px;padding:5px;font-size:11px"><input type="checkbox" name="question_ids" value="${q.id}">${esc(q.text)}</label>`).join("")}</div></div><div class="form-actions"><button class="button-secondary" type="submit">开始采集</button></div></form><p class="formula">自动采集保留模型原文；名次、情绪和信源需复核后补标。</p>` : `<div class="center-empty"><strong>尚未连接模型接口</strong>当前可手工录入或导入 CSV。若要自动采集，在本机配置已获授权的模型接口与密钥。</div>`)}</div>
    </div><div style="height:17px"></div>
    ${panel("最近录入", `当前项目共 ${state.observations.length} 条记录`, rows.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>日期</th><th>平台</th><th>状态</th><th>来源</th><th>摘要</th><th>操作</th></tr></thead><tbody>${rows.map(row => `<tr><td>${esc(row.observed_at)}</td><td>${esc(row.platform)}</td><td>${statusChip(row.status)}</td><td>${esc(sourceKind(row.source_kind))}</td><td>${esc(row.answer.slice(0,55) || "无回答")}${row.answer.length > 55 ? "…" : ""}</td><td><button class="tiny-button" data-action="detail" data-id="${row.id}">原文与截图</button> <button class="tiny-button" data-action="edit-observation" data-id="${row.id}">编辑</button> <button class="tiny-button" data-action="delete-observation" data-id="${row.id}">删除</button></td></tr>`).join("")}</tbody></table></div>` : empty("暂无记录"))}`;
}
function settingsPage() {
  if (state.project.is_demo) return `${heading("项目设置", "演示项目只供查看")} ${demoBanner()}<div class="real-start"><strong>请为真实品牌单独建项目</strong><span>新项目会有自己的问题句、回答和报告，不会混入这里的虚构样本。</span><button class="button-secondary" data-action="new-project">创建真实品牌项目</button></div>`;
  return `${heading("项目设置", "维护目标品牌、竞品和监测问题句", `<button class="button-ghost" data-action="new-project">新建品牌项目</button>`)}${demoBanner()}
    <div class="split-panel">${panel("品牌资料", "新录入的数据会按这些名称识别提及", `<form id="project-edit-form"><div class="form-grid"><div class="form-field full"><label>目标品牌 *</label><input class="field" name="brand" required value="${esc(state.project.brand)}"></div><div class="form-field"><label>品类（选填）</label><input class="field" name="category" value="${esc(state.project.category)}"></div><div class="form-field"><label>地区</label><input class="field" name="region" value="${esc(state.project.region)}"></div><div class="form-field full"><label>品牌别名</label><textarea class="field" name="aliases" rows="2">${esc(state.project.aliases.join("，"))}</textarea><small>使用逗号分隔。仅用于识别提及，不自动推断排名。</small></div><div class="form-field full"><label>竞品名称</label><textarea class="field" name="competitors" rows="4">${esc(state.project.competitors.join("，"))}</textarea></div></div><div class="form-actions"><button class="button" type="submit">保存资料</button></div></form>`)}
      ${panel("统计规则", "当前报告使用明确、可复核的口径", `<div class="detail-list"><div><b>提及率</b><p class="formula">提及目标品牌的有效回答 ÷ 有效回答数。</p></div><div><b>TOP3 / 首位</b><p class="formula">目标品牌被标为第 1–3 位 / 第 1 位的有效回答 ÷ 有效回答数。</p></div><div><b>情绪占比</b><p class="formula">已标注情绪且提及目标品牌的回答中，正面或负面的比例。</p></div><div><b>信源占比</b><p class="formula">某来源的引用次数 ÷ 当前筛选下所有引用次数。</p></div></div>`)}</div>
    <div style="height:17px"></div>${panel("问题句", `${state.questions.length} 条 · 分为品类词和品牌词`, `<form id="question-create-form" class="filters" style="box-shadow:none;margin:0 0 8px"><div class="filter-field" style="min-width:100px"><label>类型</label><select class="field" name="kind"><option value="keyword">品类词</option><option value="brand">品牌词</option></select></div><div class="filter-field wide"><label>新问题句</label><input class="field" name="text" required maxlength="500" placeholder="例如：北京工程咨询公司怎么选？"></div><button class="button-secondary" type="submit">添加问题</button></form><div>${state.questions.map(q => `<div class="question-item"><span class="chip ${q.kind === "brand" ? "green" : ""} question-kind">${q.kind === "brand" ? "品牌词" : "品类词"}</span><span class="question-text">${esc(q.text)}</span><button class="tiny-button" data-action="delete-question" data-id="${q.id}">删除</button></div>`).join("")}</div>`)}`;
}

function detailOverlay() {
  if (state.overlay?.type !== "detail") return "";
  const row = state.observations.find(item => item.id === state.overlay.id);
  if (!row) return "";
  const question = state.questions.find(q => q.id === row.question_id)?.text || "已删除问题";
  const citations = row.citations.length ? row.citations.map(c => `<div class="article-row"><div><div class="article-title">${safeUrl(c.url) ? `<a href="${esc(safeUrl(c.url))}" target="_blank" rel="noopener noreferrer">${esc(c.title || c.url)}</a>` : esc(c.title || "未命名文章")}</div><span class="article-meta">${esc(c.source || "未注明来源")}</span></div></div>`).join("") : "暂无引用标注";
  const rivals = Object.entries(row.competitor_ranks).map(([name, rank]) => `${esc(name)}：${rank ? `第 ${rank} 位` : "提及"}`).join("<br>") || "无";
  const imageName = /^[a-zA-Z0-9_-]+\.png$/.test(row.evidence_image || "") ? row.evidence_image : "";
  const imageUrl = imageName ? `/api/evidence/${imageName}` : "";
  return `<div class="overlay" data-action="close-overlay"><aside class="drawer" onclick="event.stopPropagation()">
    <div class="drawer-head"><div><div class="eyebrow">MONITORING RECORD</div><h2>模型回答详情</h2>
      <div style="color:#92a0b3;font-size:11px">记录 #${row.id} · ${esc(sourceKind(row.source_kind))}</div></div>
      <button class="close" data-action="close-overlay" aria-label="关闭">×</button></div>
    <div class="drawer-body"><dl class="detail-list">
      <div class="detail-item"><dt>问题句</dt><dd>${esc(question)}</dd></div>
      <div class="detail-item"><dt>平台与日期</dt><dd>${esc(row.platform)} · ${esc(row.observed_at)}</dd></div>
      <div class="detail-item"><dt>实际提问时间</dt><dd>${esc(row.asked_at || "未记录")}</dd></div>
      <div class="detail-item"><dt>模型会话</dt><dd>${safeUrl(row.evidence_url) ? `<a href="${esc(safeUrl(row.evidence_url))}" target="_blank" rel="noopener noreferrer">打开分享链接 ↗</a>` : "未提供分享链接"}</dd></div>
      <div class="detail-item"><dt>采集页面</dt><dd>${safeUrl(row.page_url) ? `<a href="${esc(safeUrl(row.page_url))}" target="_blank" rel="noopener noreferrer">打开页面 ↗</a>（可能需要原账号）` : "未记录"}</dd></div>
      <div class="detail-item"><dt>页面截图</dt><dd>${imageUrl ? `<a href="${imageUrl}" target="_blank" rel="noopener noreferrer">查看原始截图 ↗</a>` : "未保存"}</dd></div>
      ${row.error_message ? `<div class="detail-item"><dt>失败原因</dt><dd>${esc(row.error_message)}</dd></div>` : ""}
      <div class="detail-item"><dt>品牌表现</dt><dd>${row.brand_mentioned ? "已提及" : "未提及"} · ${row.brand_rank ? `第 ${row.brand_rank} 位` : "名次未标注"} · ${esc(sentimentText(row.sentiment))}</dd></div>
      <div class="detail-item"><dt>竞品</dt><dd>${rivals}</dd></div></dl>
      <h3 style="margin-top:24px;font-size:13px">回答原文</h3>
      <div class="detail-answer">${esc(row.answer || "离线或失败，暂无回答原文。")}</div>
      ${imageUrl ? `<h3 style="margin-top:24px;font-size:13px">采集截图</h3><a href="${imageUrl}" target="_blank" rel="noopener noreferrer"><img src="${imageUrl}" alt="网页回答截图" style="max-width:100%;border:1px solid #d6deea;border-radius:8px"></a>` : ""}
      <h3 style="margin-top:24px;font-size:13px">引用文章</h3><div>${citations}</div>
      <div class="form-actions"><button class="button-ghost" data-action="edit-observation" data-id="${row.id}">编辑标注</button></div>
    </div></aside></div>`;
}
function projectModal() {
  if (state.overlay?.type !== "new-project") return "";
  return `<div class="overlay modal-overlay" data-action="close-overlay"><div class="modal" onclick="event.stopPropagation()"><div class="modal-head"><div><div class="eyebrow">NEW BRAND</div><h2>创建品牌诊断项目</h2></div><button class="close" data-action="close-overlay" aria-label="关闭">×</button></div><form id="project-create-form"><div class="form-grid"><div class="form-field full"><label>目标品牌 *</label><input class="field" name="brand" maxlength="120" required placeholder="例如：某工程咨询公司"></div><div class="form-field"><label>所属品类（选填）</label><input class="field" name="category" maxlength="120" placeholder="填写后可生成行业推荐问题"></div><div class="form-field"><label>地区</label><input class="field" name="region" maxlength="120" placeholder="例如：北京"></div><div class="form-field full"><label>品牌别名</label><input class="field" name="aliases" placeholder="用逗号分隔"></div><div class="form-field full"><label>主要竞品</label><textarea class="field" name="competitors" rows="3" placeholder="用逗号分隔"></textarea><small>只填品牌名即可开始；补充品类可增加行业推荐问题。</small></div></div><div class="form-actions"><button class="button-ghost" type="button" data-action="close-overlay">取消</button><button class="button" type="submit">创建项目</button></div></form></div></div>`;
}
function renderShell() {
  const title = NAV.find(item => item[0] === state.page)?.[1] || "优化概览";
  const page = ({overview:overviewPage,brand:brandPage,"brand-monitor":() => monitoringPage(true),citations:citationPage,monitor:() => monitoringPage(false),input:inputPage,settings:settingsPage})[state.page]();
  return `<div class="app-layout"><aside class="sidebar"><div class="brandmark"><div class="logo">G<span style="font-size:12px">°</span></div><div><strong>GEO 诊断台</strong><small>BRAND INTELLIGENCE</small></div></div>
    <div class="project-picker"><label>当前品牌项目</label><select id="project-select">${options(state.projects.map(p => ({value:p.id,label:p.is_demo ? `【虚构演示】${p.brand}` : p.brand})),state.project.id)}</select><button class="link-button" data-action="new-project">＋ 新建品牌项目</button></div>
    <div class="nav-list"><div class="nav-caption">诊断与监测</div>${NAV.map(([key,label,icon],index) => `${index === 5 ? `<div class="nav-caption">数据与配置</div>` : ""}<button class="nav-button ${state.page === key ? "active" : ""}" data-nav="${key}"><span class="nav-icon">${esc(icon)}</span>${esc(label)}</button>`).join("")}</div>
    <div class="sidebar-foot"><a href="/login-center" style="color:inherit;font-weight:700">${state.browserPlatforms.runtime === "local" ? "本机浏览器登录" : "平台登录中心"} →</a><br><b>本机诊断工作区</b><br>数据保存在本机，报告可打印为 PDF。</div></aside>
    <div class="main-area"><header class="topbar"><div class="breadcrumb">GEO DIAGNOSTIC　·　<b>${esc(title)}</b></div><div class="top-actions"><span class="status-pill ${state.project.is_demo ? "demo" : ""}">${state.project.is_demo ? "虚构演示 · 只读" : `${state.observations.length} 条记录`}</span><a class="button-ghost" href="/api/projects/${state.project.id}/export.zip">导出证据包</a><a class="button-ghost" href="/report/${state.project.id}">报告预览 ↗</a></div></header><main class="content">${page}</main></div></div>${detailOverlay()}${projectModal()}`;
}

function desktopControls() {
  return desktopToken ? `<div class="desktop-controls" style="position:fixed;right:24px;bottom:18px;z-index:30"><button class="button-ghost" data-action="quit-app">退出 GEO</button></div>` : "";
}
function reportFoot() { return `<div class="page-foot">GEO 品牌诊断报告 · ${state.project.is_demo ? "虚构演示数据，不能用于品牌判断" : "本机项目"}</div>`; }
function reportTitle(cn, en) { return `<h2 class="report-section-title">${esc(cn)}</h2><div class="report-section-en">${esc(en)}</div><div class="report-rule"></div>`; }
function reportQuestionMonitor() {
  const rows = state.analytics.observations.slice(0,30);
  if (!rows.length) return `<div class="report-empty">暂无问题句监测记录</div>`;
  return `<div class="table-wrap"><table class="data-table"><thead><tr><th>问题句</th><th>平台</th><th>目标品牌</th><th>名次</th><th>日期</th></tr></thead><tbody>${rows.map(row => `<tr><td>${esc(row.question)}</td><td>${esc(row.platform)}</td><td>${row.brand_mentioned ? "提及" : "未提及"}</td><td>${row.brand_rank ? `第 ${row.brand_rank} 位` : "—"}</td><td>${esc(row.observed_at)}</td></tr>`).join("")}</tbody></table></div>${state.analytics.observations.length > 30 ? `<p class="report-note">仅显示前 30 条，共 ${state.analytics.observations.length} 条。完整记录见诊断台。</p>` : ""}`;
}
function renderReport() {
  const p = state.project, m = state.analytics.metrics;
  const dates = state.analytics.observations.map(row => row.observed_at).sort();
  const dateRange = dates.length ? `${dateCN(dates[0])} — ${dateCN(dates.at(-1))}` : "待采集";
  return `<div class="report-layout"><aside class="report-sidebar"><div class="toc-title">目录<small>CONTENTS</small></div><a href="#overview">品牌总览</a><a href="#sentiment">品牌情绪占比</a><a href="#deep-dive">深度解析</a><a class="toc-sub" href="#deep-dive">数据总览</a><a class="toc-sub" href="#competitors">竞品分析</a><a class="toc-sub" href="#sources">信源分析</a><a class="toc-sub" href="#questions">问题句监测</a><div class="toc-footer"><button class="button-ghost" data-action="report-back">← 返回诊断台</button><button class="button" data-action="print-report">打印 / 保存 PDF</button></div></aside>
    <main class="report-main"><div class="report-controls"><button class="button-ghost" data-action="copy-report">复制本机链接</button><button class="button" data-action="print-report">打印 / 保存 PDF</button></div>
      <section class="report-page report-cover"><h1>${esc(p.brand)}<br>GEO 品牌诊断报告</h1>${p.is_demo ? `<div class="report-demo-warning">虚构演示数据，不能用于品牌判断</div>` : ""}<div class="cover-rule"></div><div class="cover-brand">GEO 诊断台</div><div class="cover-foot"><span>${esc(dateRange)}</span><span>${p.is_demo ? "DEMO DATA" : "LOCAL REPORT"}</span></div></section>
      <section class="report-page" id="overview">${reportTitle("品牌总览","Brand Overview")}${p.is_demo ? `<div class="note-banner demo"><b>演示数据</b>　以下指标由虚构回答计算，仅用于体验报告结构。</div>` : ""}<p style="font-size:13px;color:#61728e">${esc(summarySentence())}</p>${metricsHtml(m)}<div class="report-stat-box"><div><small>有效回答</small><strong>${m.answers}</strong></div><div><small>目标品牌被提及</small><strong>${m.mentioned}</strong></div><div><small>离线或失败</small><strong>${m.offline}</strong></div></div><p class="report-note">统计说明：提及率、TOP3 占比与首位占比均以有效回答为分母；情绪占比以已标注且提及目标品牌的回答为分母。回答原文和标注可在诊断台逐条查看。</p>${reportFoot()}</section>
      <section class="report-page" id="sentiment">${reportTitle("品牌情绪占比","Brand Sentiment Distribution")}<div class="sentiment-layout"><div>${sentimentDonut()}</div><div class="term-grid"><div class="term-box"><h3>正面高频词</h3>${termsHtml(state.analytics.positive_terms)}</div><div class="term-box"><h3>负面高频词</h3>${termsHtml(state.analytics.negative_terms,true)}</div></div></div><p class="report-note">情绪和信息点来自记录中的标注。未标注回答不纳入情绪比例。</p>${reportFoot()}</section>
      <section class="report-page" id="deep-dive">${reportTitle("深度解析","Deep Dive Analysis")}${filterBar(true)}<h3>数据总览</h3><div class="report-stat-box"><div><small>TOP3 基准值（竞品第三位）</small><strong>${m.answers ? pct(m.top3_benchmark) : "—"}</strong></div><div><small>目标品牌 TOP3</small><strong>${m.answers ? pct(m.top3_rate) : "—"}</strong></div><div><small>目标品牌提及率</small><strong>${m.answers ? pct(m.mention_rate) : "—"}</strong></div></div>${lineChart(state.analytics.trend,"mention_rate")}<p class="report-note">趋势按采集日期汇总。改变平台或问题句筛选，会同步更新本报告各页数字。</p>${reportFoot()}</section>
      <section class="report-page" id="competitors">${reportTitle("竞品分析","Competitor Comparison")}${competitorTable(30)}<p class="report-note">竞品名称出现在回答中记为提及；前三位与首位只采用明确录入的名次。</p>${reportFoot()}</section>
      <section class="report-page" id="sources">${reportTitle("信源分析","Source Analysis")}<div class="section-grid">${panel("媒体来源分布", "按引用次数占比", sourcesHtml(15))}${panel("文章引用列表", `共 ${state.analytics.articles.length} 篇`, articleHtml(12))}</div><p class="report-note">信源占比按引用次数计算；来源出现不等于内容已核实。</p>${reportFoot()}</section>
      <section class="report-page" id="questions">${reportTitle("问题句监测","Question Monitoring")}${reportQuestionMonitor()}${reportFoot()}</section>
    </main></div>${detailOverlay()}`;
}
function render() {
  if (!state.project || !state.analytics) {
    document.getElementById("app").innerHTML = `<div class="boot"><h2>还没有品牌项目</h2><p>输入品牌名即可开始 GEO 诊断。首次使用时，在本机浏览器登录 DeepSeek、豆包和 Kimi，再逐个平台采集。无需安装 AI 工具；采集后请核对排名、评价和引用。</p><button class="button" data-action="new-project">创建项目</button></div>${projectModal()}${desktopControls()}`;
    return;
  }
  document.title = REPORT_MODE ? `${state.project.brand} · GEO 品牌诊断报告` : "GEO 诊断台";
  document.getElementById("app").innerHTML = (REPORT_MODE ? renderReport() : renderShell()) + desktopControls();
}

function splitNames(value) { return String(value || "").split(/[，,\n]/).map(x => x.trim()).filter(Boolean); }
function jsonObject(value) { try { const obj = JSON.parse(value || "{}"); if (!obj || Array.isArray(obj) || typeof obj !== "object") throw Error(); return obj; } catch { throw Error("竞品排名需填写 JSON 对象，例如：{\"竞品名称\": 2}"); } }
function competitorRanks(value) {
  const text = String(value || "").trim();
  if (!text) return {};
  if (text.startsWith("{")) return jsonObject(text);
  const result = {};
  for (const line of text.split(/\n/).filter(x=>x.trim())) {
    const [name, rank, extra] = line.split("|").map(x=>x.trim());
    if (!name || rank === undefined || extra !== undefined) throw Error("竞品名次请每行填写：名称 | 名次；无明确名次可填写「无明确名次」");
    if (rank && rank !== "无明确名次" && (!/^\d+$/.test(rank) || Number(rank) < 1 || Number(rank) > 100)) throw Error("竞品名次请填写 1–100，或「无明确名次」");
    result[name] = (!rank || rank === "无明确名次") ? null : Number(rank);
  }
  return result;
}
function citationsFromLines(value) {
  return String(value || "").split("\n").map(x => x.trim()).filter(Boolean).map((line,index) => {
    const parts = line.split("|").map(x => x.trim());
    if (parts.length !== 3) throw Error(`第 ${index+1} 条引用需要“标题 | 来源 | URL”三项`);
    return {title:parts[0],source:parts[1],url:parts[2]};
  });
}
async function onSubmit(event) {
  const form = event.target;
  if (!["project-create-form","project-edit-form","question-create-form","observation-form","import-form","run-form","browser-run-form"].includes(form.id)) return;
  event.preventDefault();
  const data = new FormData(form);
  const submit = form.querySelector("button[type=submit]");
  if (submit) submit.disabled = true;
  try {
    if (form.id === "project-create-form") {
      const project = await api("/api/projects", {method:"POST",body:JSON.stringify({
        brand:data.get("brand"),category:data.get("category"),region:data.get("region"),
        aliases:splitNames(data.get("aliases")),competitors:splitNames(data.get("competitors")),
      })});
      state.projects = await api("/api/projects");
      state.overlay = null; state.page = "input"; state.filters = {platform:"all",question_id:"",start:"",end:""};
      await loadProject(project.id); toast("项目已创建，已生成初始问题句");
    } else if (form.id === "project-edit-form") {
      await api(`/api/projects/${state.project.id}`, {method:"PUT",body:JSON.stringify({
        brand:data.get("brand"),category:data.get("category"),region:data.get("region"),
        aliases:splitNames(data.get("aliases")),competitors:splitNames(data.get("competitors")),
      })});
      state.projects = await api("/api/projects"); await refresh(); toast("品牌资料已保存");
    } else if (form.id === "question-create-form") {
      await api(`/api/projects/${state.project.id}/questions`, {method:"POST",body:JSON.stringify({kind:data.get("kind"),text:data.get("text")})});
      await refresh(); toast("问题句已添加");
    } else if (form.id === "observation-form") {
      const payload = {
        question_id:Number(data.get("question_id")),platform:data.get("platform"),observed_at:data.get("observed_at"),
        status:data.get("status"),answer:data.get("answer"),evidence_url:data.get("evidence_url"),brand_rank:data.get("brand_rank"),rank_reviewed:data.has("rank_reviewed"),
        sentiment:data.get("sentiment"),competitor_ranks:competitorRanks(data.get("competitor_ranks")),
        citations:citationsFromLines(data.get("citations")),positive_terms:splitNames(data.get("positive_terms")),
        negative_terms:splitNames(data.get("negative_terms")),
      };
      if (data.get("brand_mentioned") !== "auto") payload.brand_mentioned = data.get("brand_mentioned") === "true";
      const path = state.editId ? `/api/projects/${state.project.id}/observations/${state.editId}` : `/api/projects/${state.project.id}/observations`;
      await api(path, {method:state.editId ? "PUT" : "POST",body:JSON.stringify(payload)});
      state.editId = null; await refresh(); toast("监测记录已保存，指标已更新");
    } else if (form.id === "import-form") {
      const file = data.get("file");
      if (!file || !file.name) throw Error("请选择 CSV 文件");
      const result = await api(`/api/projects/${state.project.id}/import`, {method:"POST",body:data});
      await refresh(); toast(`已导入 ${result.imported} 条记录`);
    } else if (form.id === "run-form") {
      const ids = data.getAll("question_ids").map(Number);
      const result = await api(`/api/projects/${state.project.id}/run`, {method:"POST",body:JSON.stringify({provider:data.get("provider"),question_ids:ids})});
      await refresh(); toast(`已采集 ${result.results.filter(r => r.status === "complete").length} 条；失败 ${result.results.filter(r => r.status !== "complete").length} 条`);
    } else if (form.id === "browser-run-form") {
      const ids = data.getAll("question_ids").map(Number);
      if (ids.length < 1 || ids.length > 3) throw Error("每次请选择 1 到 3 条问题句");
      const participantId = data.get("participant_id");
      const localBrowser = state.browserPlatforms.runtime === "local";
      const account = state.browserAccounts.accounts.find(a => a.id === participantId && a.can_collect);
      if (!localBrowser && !account) throw Error("请刷新账号授权并选择有效账号");
      const platform = data.get("platform");
      if (!localBrowser && !window.confirm(`即将通过 ${account.name} 的 ${platform} 账号发送 ${ids.length} 条问题，并保存答案到当前 GEO 项目。继续吗？`)) { if (submit) submit.disabled=false; return; }
      if (submit) submit.textContent = "正在浏览器中提问，请稍等…";
      toast(`正在通过${localBrowser ? "本机" : account.name + " 的"} ${platform} 提问`);
      const result = await api(`/api/projects/${state.project.id}/run-browser`, {method:"POST",body:JSON.stringify({platform,...(localBrowser?{}:{participant_id:participantId}),question_ids:ids})});
      await refresh();
      const good = result.results.filter(r => r.status === "complete").length;
      toast(`网页采集完成 ${good} 条；失败 ${result.results.length - good} 条，请在最近录入中查看`);
    }
  } catch (error) { toast(error.message, true); if (submit) submit.disabled = false; }
}
async function onClick(event) {
  const nav = event.target.closest("[data-nav]");
  if (nav) { state.page = nav.dataset.nav; state.overlay = null; state.editId = null; render(); window.scrollTo(0,0); return; }
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  try {
    if (action === "quit-app") {
      await api("/api/desktop/quit", {method:"POST",headers:{"X-GEO-App-Token":desktopToken}});
      document.getElementById("app").innerHTML = '<div class="boot"><h2>GEO 已退出</h2><p>项目和登录资料已保存在这台电脑，下次双击程序即可继续。</p></div>';
    }
    else if (action === "refresh-browser-accounts") {
      state.browserAccounts.loading=true;render();
      try { const result=await api("/api/browser-accounts");state.browserAccounts={ready:result.ready,accounts:result.accounts||[],loading:false}; }
      catch(error) { state.browserAccounts={ready:false,accounts:[],loading:false};render();throw error; }
      render();toast(state.browserAccounts.accounts.some(a=>a.can_collect)?"已刷新个人账号授权状态":"还没有可供 GEO 使用的账号授权");
    }
    else if (action === "new-project") { state.overlay = {type:"new-project"}; render(); }
    else if (action === "close-overlay") { state.overlay = null; render(); }
    else if (action === "detail") { state.overlay = {type:"detail",id:Number(button.dataset.id)}; render(); }
    else if (action === "edit-observation") { state.editId = Number(button.dataset.id); state.overlay = null; state.page = "input"; render(); window.scrollTo(0,0); }
    else if (action === "cancel-edit") { state.editId = null; render(); }
    else if (action === "delete-observation") {
      if (!confirm("确定删除这条本机监测记录吗？报告会立即重新计算。")) return;
      await api(`/api/projects/${state.project.id}/observations/${button.dataset.id}`, {method:"DELETE"});
      await refresh(); toast("记录已删除");
    } else if (action === "delete-question") {
      if (!confirm("确定删除这条问题句吗？已有监测记录的问题不能删除。")) return;
      await api(`/api/projects/${state.project.id}/questions/${button.dataset.id}`, {method:"DELETE"});
      await refresh(); toast("问题句已删除");
    } else if (action === "question-filter") { state.filters.question_id = button.dataset.id; await refreshAnalytics(); }
    else if (action === "clear-filters") { state.filters = {platform:"all",question_id:"",start:"",end:""}; await refreshAnalytics(); }
    else if (action === "chart") { state.chartMetric = button.dataset.value; render(); }
    else if (action === "print-report") window.print();
    else if (action === "report-back") location.href = "/";
    else if (action === "copy-report") { await navigator.clipboard.writeText(location.href); toast("本机报告链接已复制"); }
  } catch (error) { toast(error.message, true); }
}
async function onChange(event) {
  if (event.target.id === "project-select") {
    state.filters = {platform:"all",question_id:"",start:"",end:""};
    try { await loadProject(Number(event.target.value)); } catch (error) { toast(error.message,true); }
    return;
  }
  if (event.target.name === "participant_id") {
    state.browserAccounts.selected=event.target.value;
    render();
    return;
  }
  const key = event.target.dataset.filter;
  if (!key) return;
  state.filters[key] = event.target.value;
  try { await refreshAnalytics(); } catch (error) { toast(error.message,true); }
}
document.addEventListener("submit", onSubmit);
document.addEventListener("click", onClick);
document.addEventListener("change", onChange);
init();
