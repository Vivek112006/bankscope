'use strict';
/* BankScope front end. Every control calls a Flask JSON API; nothing is hard-coded or simulated. */

// ------------------------------------------------------------------ helpers
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = (v, d = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  if (typeof v !== 'number') return esc(v);
  if (v !== 0 && Math.abs(v) < 0.01) return v.toExponential(2);
  return v.toLocaleString(undefined, { maximumFractionDigits: Number.isInteger(v) ? 0 : d });
};
const pct = (v, d = 1) => (v === null || v === undefined ? '—' : (v * 100).toFixed(d) + '%');
const CSRF = $('meta[name=csrf]').content;
const COLORS = ['#2563eb', '#f59e0b', '#10b981', '#8b5cf6', '#ef4444', '#06b6d4', '#ec4899', '#84cc16', '#64748b', '#f97316'];
let CFG = { max_upload_mb: 200, max_rows: 500000 };
let DS = null;            // current dataset info from the server
let current = 'dashboard';
let seen = {};
let busy = 0;
const charts = {};

async function api(path, { method = 'GET', json, form } = {}) {
  busy++; $('#loader').classList.add('show');
  try {
    const opt = { method, headers: {}, credentials: 'same-origin' };
    if (method !== 'GET') opt.headers['X-CSRF-Token'] = CSRF;
    if (json !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(json); }
    if (form) opt.body = form;
    let res;
    try { res = await fetch(path, opt); }
    catch (e) { throw new Error('Cannot reach the server. Check that it is running (an upload larger than the limit can also cause this).'); }
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON response */ }
    if (res.status === 401 && !path.startsWith('/api/auth/')) { window.location.href = '/login'; throw new Error('Your session has ended. Please sign in again.'); }
    if (!res.ok || !data || !data.success) throw new Error((data && data.error) || `Request failed (${res.status}).`);
    return data.data;
  } finally {
    if (--busy === 0) $('#loader').classList.remove('show');
  }
}

function toast(msg, type = '') {
  const t = document.createElement('div');
  t.className = 'toast ' + type; t.textContent = msg;
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), type === 'err' ? 7000 : 3500);
}
async function run(fn, btn) {
  if (btn) btn.disabled = true;
  try { await fn(); return true; }
  catch (e) { toast(e.message || String(e), 'err'); return false; }
  finally { if (btn) btn.disabled = false; }
}
const notice = (type, text) => `<div class="notice ${type}">${esc(text)}</div>`;
const notices = (type, list) => (list || []).map(t => notice(type, t)).join('');
const stat = (lbl, val, note = '') => `<div class="stat"><div class="lbl">${esc(lbl)}</div><div class="val">${val}</div><div class="note">${esc(note)}</div></div>`;

function table(head, rows, o = {}) {
  const td = v => {
    if (v && typeof v === 'object' && 'html' in v) return `<td>${v.html}</td>`;
    if (typeof v === 'number') return `<td class="num">${o.raw ? v : num(v)}</td>`;
    return `<td>${v === null || v === undefined ? '<span class="muted">—</span>' : esc(v)}</td>`;
  };
  return `<div class="scroll"><table><thead><tr>${head.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(td).join('')}</tr>`).join('')}</tbody></table></div>`;
}
const recordsTable = (cols, recs) => table(cols, recs.map(r => cols.map(c => r[c])), { raw: true });

function chips(id, cols, checked = () => false) {
  $('#' + id).innerHTML = cols.length
    ? cols.map(c => `<label class="chip"><input type="checkbox" value="${esc(c)}" ${checked(c) ? 'checked' : ''}><span>${esc(c)}</span></label>`).join('')
    : '<span class="muted small">No suitable columns.</span>';
}
const picked = id => $$(`#${id} input:checked`).map(i => i.value);
function options(id, cols, blank) {
  $('#' + id).innerHTML = (blank !== undefined ? `<option value="">${esc(blank)}</option>` : '') +
    cols.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
}
function draw(id, cfg) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($('#' + id), cfg);
}
if (window.Chart) {
  Chart.defaults.font.family = 'Inter, "Segoe UI", system-ui, sans-serif';
  Chart.defaults.color = '#64748b';
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.maintainAspectRatio = false;
}
const axis = t => ({ title: { display: !!t, text: t }, grid: { color: '#eef1f7' } });

// ------------------------------------------------------------------ navigation
const ICON = {
  dashboard: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
  clean: '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>',
  eda: '<line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/>',
  segment: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  churn: '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="18" y1="8" x2="23" y2="13"/><line x1="23" y1="8" x2="18" y2="13"/>',
  credit: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  trend: '<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/>',
  report: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>'
};
const NAV = [['dashboard', 'Dashboard'], ['upload', 'Dataset upload'], ['clean', 'Data cleaning'], ['eda', 'Exploratory analysis'],
  ['segment', 'Customer segmentation'], ['churn', 'Churn prediction'], ['credit', 'Credit risk'], ['trend', 'Trend analysis'], ['report', 'Reports & insights']];
const loaders = { dashboard: () => loadDashboard(), eda: () => loadEda(), report: () => loadInsights() };

function buildNav() {
  $('#nav').innerHTML = NAV.map(([k, t]) => `<button data-k="${k}"><svg viewBox="0 0 24 24">${ICON[k]}</svg>${esc(t)}</button>`).join('');
  $('#nav').addEventListener('click', e => { const b = e.target.closest('button'); if (b) go(b.dataset.k); });
  $$('.view.needs-data').forEach(v => v.insertAdjacentHTML('afterbegin',
    '<div class="card empty gate"><h3>No dataset loaded yet</h3><p class="muted">Upload a CSV or Excel file to unlock this module.</p><button class="btn" data-go="upload">Go to upload</button></div>'));
  document.addEventListener('click', e => { const g = e.target.closest('[data-go]'); if (g) go(g.dataset.go); });
  $('#menu').onclick = () => { $('#sidebar').classList.toggle('open'); $('#scrim').classList.toggle('show'); };
  $('#scrim').onclick = closeMenu;
}
function closeMenu() { $('#sidebar').classList.remove('open'); $('#scrim').classList.remove('show'); }
function go(key) {
  if (!NAV.some(n => n[0] === key)) return;
  current = key;
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'view-' + key));
  $$('#nav button').forEach(b => b.classList.toggle('on', b.dataset.k === key));
  $('#page-title').textContent = NAV.find(n => n[0] === key)[1];
  closeMenu(); window.scrollTo(0, 0);
  if (DS && loaders[key] && !seen[key]) { seen[key] = true; run(loaders[key]).then(ok => { if (!ok) seen[key] = false; }); }
}

// ------------------------------------------------------------------ dataset state
function updatePill() {
  document.body.classList.toggle('has-data', !!DS);
  const p = $('#ds-pill');
  p.classList.toggle('on', !!DS);
  p.textContent = DS ? `${DS.name} · ${DS.rows.toLocaleString()} rows` : 'No dataset loaded';
  $('#top-ds').textContent = DS ? `${DS.rows.toLocaleString()} rows × ${DS.columns_count} columns` : 'Upload a dataset to begin';
  $('#hero-title').textContent = DS ? `Working with ${DS.name}` : 'Turn banking data into decisions';
}
function afterChange() {
  updatePill(); seen = {};
  if (DS) populate();
  if (loaders[current] && DS) { seen[current] = true; run(loaders[current]); }
  if (!DS) { $('#cards').innerHTML = ''; renderCards(null); }
}
function resetOutputs() {
  ['clean-result', 'eda-out', 'seg-out', 'trend-out', 'churn-out', 'credit-out', 'cr-out'].forEach(i => { const e = $('#' + i); if (e) e.innerHTML = ''; });
  $('#e-box').hidden = true; $('#e-notes').innerHTML = ''; $('#s-dl').hidden = true;
  Object.keys(charts).forEach(k => { charts[k].destroy(); delete charts[k]; });
}

function populate() {
  const cols = DS.columns, names = cols.map(c => c.name);
  const numericKind = cols.filter(c => c.is_numeric && c.kind === 'numeric').map(c => c.name);
  const numericAny = cols.filter(c => c.is_numeric && c.kind !== 'id').map(c => c.name);
  chips('c-drop', names);
  chips('c-num', DS.suspect_numeric);
  chips('c-date', DS.suspect_date);
  chips('c-outcols', numericKind);
  options('e-x', names); options('e-y', numericAny, '(none)');
  chips('s-feats', numericKind, c => numericKind.indexOf(c) < 4); updateSegAxes();
  options('t-date', cols.filter(c => c.kind === 'date').map(c => c.name));
  options('t-metric', numericKind, '(count of records)');
  ['churn', 'credit'].forEach(k => {
    const targets = cols.filter(c => c.unique >= 2 && c.unique <= 20 && c.kind !== 'id').map(c => c.name);
    options(`${k}-target`, targets, 'Select target column…');
    const sug = DS.suggested[k];
    if (sug && targets.includes(sug)) { $(`#${k}-target`).value = sug; run(() => loadClasses(k)); }
    else { $(`#${k}-pos`).innerHTML = '<option value="">Choose a target first</option>'; $(`#${k}-classes`).innerHTML = ''; renderFeats(k); }
  });
  chips('cr-high', numericKind); chips('cr-low', numericKind);
  $('#e-type').dispatchEvent(new Event('change'));
}

// ------------------------------------------------------------------ dashboard
const CARDS = [['records', 'Total records'], ['customers', 'Total customers'], ['balance', 'Average account balance'],
  ['missing', 'Missing values'], ['churn', 'Churn rate'], ['risk', 'High-risk customers']];
function renderCards(data) {
  $('#cards').innerHTML = CARDS.map(([k, lbl]) => {
    const c = data && data[k];
    if (!c) return stat(lbl, '—', 'Upload a dataset to begin');
    if (c.value === null || c.value === undefined) return `<div class="stat"><div class="lbl">${esc(lbl)}</div><div class="val na">Not available for this dataset</div><div class="note">Required columns or models are missing</div></div>`;
    const v = c.fmt === 'pct' ? pct(c.value) : c.fmt === 'num' ? num(c.value, 2) : num(c.value);
    return stat(lbl, v, c.note);
  }).join('');
}
async function loadDashboard() {
  const [d, i] = await Promise.all([api('/api/dashboard'), api('/api/insights')]);
  renderCards(d.cards);
  $('#dash-insights').innerHTML = i.insights.length ? insightList(i.insights.slice(0, 6)) : '<span class="muted">No insights yet.</span>';
}
const insightList = list => list.map(i => `<div class="insight"><span class="tag">${esc(i.category)}</span><div>${esc(i.text)}</div></div>`).join('');
async function loadInsights() { $('#rep-insights').innerHTML = insightList((await api('/api/insights')).insights); }

// ------------------------------------------------------------------ upload
function renderUpload() {
  if (!DS) return;
  $('#upload-result').hidden = false;
  $('#info-cards').innerHTML = [stat('Rows', num(DS.rows), DS.modified ? `Original: ${num(DS.original_rows)}` : 'As uploaded'), stat('Columns', num(DS.columns_count)),
    stat('Missing values', num(DS.missing_total), DS.missing_pct.toFixed(2) + '% of cells'), stat('Duplicate rows', num(DS.duplicates)), stat('Memory', DS.memory_mb + ' MB', 'Working copy')].join('');
  $('#col-table').innerHTML = table(['Column', 'Type', 'Kind', 'Missing', 'Unique', 'Sample values'],
    DS.columns.map(c => [c.name, c.dtype, { html: `<span class="kind ${esc(c.kind)}">${esc(c.kind)}</span>` }, c.missing, c.unique, c.sample.join(', ')]), { raw: true });
  $('#upload-msgs').innerHTML = notices('warn', DS.warnings);
  run(async () => { const p = await api('/api/dataset/preview?rows=10'); $('#preview-table').innerHTML = recordsTable(p.columns, p.rows); });
}
async function uploadFile(file) {
  if (!file) return;
  if (!/\.(csv|xlsx)$/i.test(file.name)) return toast('Please choose a .csv or .xlsx file.', 'err');
  if (file.size === 0) return toast('That file is empty.', 'err');
  if (file.size > CFG.max_upload_mb * 1048576) return toast(`File is ${(file.size / 1048576).toFixed(1)} MB; the limit is ${CFG.max_upload_mb} MB.`, 'err');
  const fd = new FormData(); fd.append('file', file);
  $('#drop').classList.add('busy');
  const ok = await run(async () => {
    DS = await api('/api/upload', { method: 'POST', form: fd });
    resetOutputs(); afterChange(); renderUpload();
    toast(`Loaded ${DS.rows.toLocaleString()} rows × ${DS.columns_count} columns`, 'ok');
  });
  $('#drop').classList.remove('busy');
  return ok;
}
function wireUpload() {
  const drop = $('#drop'), input = $('#file');
  input.onchange = () => { uploadFile(input.files[0]); input.value = ''; };
  ['dragenter', 'dragover'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(e => drop.addEventListener(e, ev => { ev.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', ev => uploadFile(ev.dataTransfer.files[0]));
}
function setLimits() {
  $('#limits').textContent = `Supported: .csv, .xlsx · Max ${CFG.max_upload_mb} MB · First ${CFG.max_rows.toLocaleString()} rows are loaded; models use at most ${(CFG.max_ml_rows || 100000).toLocaleString()} rows.`;
}

// ------------------------------------------------------------------ cleaning
function cleanOps() {
  return { drop_duplicates: $('#c-dup').checked, numeric_fill: $('#c-nfill').value, cat_fill: $('#c-cfill').value, cat_custom: $('#c-ccustom').value,
    outliers: $('#c-out').value, outlier_columns: picked('c-outcols'), to_numeric: picked('c-num'), to_date: picked('c-date'), drop_columns: picked('c-drop') };
}
async function doClean(preview) {
  const r = await api('/api/clean', { method: 'POST', json: { preview, operations: cleanOps() } });
  const cmp = (a, b) => `${num(a)} → ${num(b)}`;
  $('#clean-result').innerHTML = `<div class="card"><h3>${preview ? 'Preview of changes (nothing applied yet)' : 'Changes applied to the working copy'}</h3>
    <div class="cards">${stat('Rows', cmp(r.rows_before, r.rows_after))}${stat('Columns', cmp(r.cols_before, r.cols_after))}${stat('Missing values', cmp(r.missing_before, r.missing_after))}${stat('Duplicates', cmp(r.dups_before, r.dups_after))}</div>
    ${notices(preview ? 'info' : 'good', r.log)}<h3 style="margin-top:16px">Resulting data (first 10 rows)</h3>${recordsTable(r.columns, r.preview_rows)}</div>`;
  if (!preview) { DS = r.info; afterChange(); $('#s-dl').hidden = true; ['churn', 'credit'].forEach(k => { const p = $(`#${k}-pred`); if (p) p.innerHTML = ''; }); toast('Cleaning applied', 'ok'); }
}
function wireClean() {
  $('#c-preview').onclick = e => run(() => doClean(true), e.target);
  $('#c-apply').onclick = e => run(() => doClean(false), e.target);
  $('#c-reset').onclick = e => run(async () => {
    DS = await api('/api/clean', { method: 'POST', json: { reset: true } });
    $('#clean-result').innerHTML = ''; afterChange(); toast('Working copy reset to the original upload', 'ok');
  }, e.target);
}

// ------------------------------------------------------------------ EDA
async function loadEda() {
  const d = await api('/api/eda/summary');
  const heat = d.correlation.columns.length ? (() => {
    const cs = d.correlation.columns;
    const cell = r => {
      if (r === null) return '<td>—</td>';
      const a = Math.min(Math.abs(r), 1), bg = r >= 0 ? `rgba(37,99,235,${a})` : `rgba(220,38,38,${a})`;
      return `<td style="background:${bg};color:${a > 0.55 ? '#fff' : '#0b1b3a'}">${r.toFixed(2)}</td>`;
    };
    return `<div class="scroll"><table class="heat"><thead><tr><th></th>${cs.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${
      d.correlation.matrix.map((row, i) => `<tr><th>${esc(cs[i])}</th>${row.map(cell).join('')}</tr>`).join('')}</tbody></table></div>`;
  })() : '<p class="muted">At least two varying numeric columns are needed for correlations.</p>';
  const maxM = Math.max(1, ...d.missing.map(m => m.count));
  $('#eda-out').innerHTML = `
    <div class="card"><h3>Descriptive statistics (numeric columns)</h3>${d.stats.length ? table(['Column', 'Count', 'Mean', 'Median', 'Mode', 'Min', 'Max', 'Std dev', 'Q1', 'Q3', 'Missing'],
      d.stats.map(s => [s.column, s.count, s.mean, s.median, s.mode, s.min, s.max, s.std, s.q25, s.q75, s.missing])) : '<p class="muted">No numeric columns detected. Convert text numbers in Data cleaning.</p>'}</div>
    <div class="grid2"><div class="card"><h3>Missing values</h3>${d.missing.length ? `<div class="bars">${d.missing.map(m => `<div class="b"><span>${esc(m.column)}</span><div class="track"><div class="fill" style="width:${(m.count / maxM * 100).toFixed(1)}%"></div></div><span>${num(m.count)} (${pct(m.pct)})</span></div>`).join('')}</div>` : notice('good', 'No missing values.')}</div>
    <div class="card"><h3>Outliers (IQR method)</h3>${d.outliers.length ? table(['Column', 'Outliers', '% of values', 'Lower fence', 'Upper fence'], d.outliers.map(o => [o.column, o.count, pct(o.pct), o.lower, o.upper])) : notice('good', 'No IQR outliers detected.')}</div></div>
    <div class="card"><h3>Correlation matrix</h3>${heat}${d.correlation.strong.length ? `<p class="muted small">Strongest pairs: ${d.correlation.strong.slice(0, 5).map(p => `${esc(p.a)} ↔ ${esc(p.b)} (${p.r})`).join(' · ')}. Correlation does not prove causation.</p>` : ''}</div>
    <div class="card"><h3>Categorical value counts</h3>${d.categorical.length ? `<div class="mini">${d.categorical.map(c => `<div class="card"><b>${esc(c.column)}</b> <span class="muted small">${c.unique} unique</span>${table(['Value', 'Count', 'Share'], c.top.map(t => [t.value, t.count, pct(t.pct)]))}</div>`).join('')}</div>` : '<p class="muted">No categorical columns detected.</p>'}</div>`;
}
async function buildChart() {
  const type = $('#e-type').value;
  const r = await api('/api/eda/chart', { method: 'POST', json: { chart: type, x: $('#e-x').value, y: $('#e-y').value || null, agg: $('#e-agg').value, top_n: 15, bins: 20 } });
  $('#e-box').hidden = false;
  let notes = notices('warn', r.warnings);
  if (type === 'scatter' && r.correlation !== null && r.correlation !== undefined) notes += notice('info', `Pearson correlation between ${r.x} and ${r.y}: ${r.correlation.toFixed(3)} (association only, not causation).`);
  $('#e-notes').innerHTML = notes;
  const base = { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } } };
  if (type === 'scatter') {
    draw('e-canvas', { type: 'scatter', data: { datasets: [{ label: `${r.y} vs ${r.x}`, data: r.points, backgroundColor: 'rgba(37,99,235,.45)', pointRadius: 3 }] },
      options: { ...base, scales: { x: axis(r.x), y: axis(r.y) } } });
  } else {
    const isLine = type === 'line', isHist = type === 'histogram';
    draw('e-canvas', { type: isLine ? 'line' : 'bar',
      data: { labels: r.labels, datasets: [{ label: r.y_label, data: r.values, backgroundColor: isLine ? 'rgba(37,99,235,.12)' : '#2563eb', borderColor: '#2563eb', borderWidth: isLine ? 2.5 : 0,
        borderRadius: isHist ? 0 : 6, barPercentage: isHist ? 1 : 0.8, categoryPercentage: isHist ? 1 : 0.9, fill: isLine, tension: 0.3, pointRadius: isLine && r.labels.length > 60 ? 0 : 3, spanGaps: true }] },
      options: { ...base, scales: { x: { ...axis(r.x), ticks: { maxRotation: 60, autoSkip: true } }, y: { ...axis(r.y_label), beginAtZero: true } } } });
  }
}
function wireEda() {
  $('#e-go').onclick = e => run(buildChart, e.target);
  $('#e-type').onchange = () => {
    const t = $('#e-type').value;
    $('#e-y').disabled = t === 'histogram'; $('#e-agg').disabled = t === 'histogram' || t === 'scatter';
  };
}

// ------------------------------------------------------------------ segmentation
function updateSegAxes() {
  const f = picked('s-feats');
  const keepX = $('#s-x').value, keepY = $('#s-y').value;
  options('s-x', f); options('s-y', f);
  if (f.includes(keepX)) $('#s-x').value = keepX;
  if (f.includes(keepY)) $('#s-y').value = keepY; else if (f.length > 1) $('#s-y').value = f.find(c => c !== $('#s-x').value);
}
async function runSegment() {
  const feats = picked('s-feats');
  if (feats.length < 2) throw new Error('Select at least two numeric features.');
  const r = await api('/api/segment', { method: 'POST', json: { features: feats, k: Number($('#s-k').value), x_feature: $('#s-x').value, y_feature: $('#s-y').value } });
  $('#s-dl').hidden = false;
  const sc = Object.keys(r.scatter).map((c, i) => ({ label: `Cluster ${c}`, data: r.scatter[c], backgroundColor: COLORS[i % COLORS.length] + 'aa', pointRadius: 3.5 }));
  $('#seg-out').innerHTML = `
    <div class="cards">${r.sizes.map(s => stat(`Cluster ${s.cluster}`, num(s.count), pct(s.pct) + ' of customers')).join('')}${stat('Silhouette score', r.silhouette === null ? '—' : r.silhouette.toFixed(3), 'Higher = better separated (max 1)')}</div>
    <div class="card"><h3>${esc(r.x_feature)} vs ${esc(r.y_feature)}</h3><div class="chart-box"><canvas id="seg-canvas"></canvas></div></div>
    <div class="card"><h3>What distinguishes each cluster</h3>${r.descriptions.map(d => notice('info', d)).join('')}
      <h3 style="margin-top:16px">Cluster averages (original units)</h3>${table(['Cluster', ...r.profile.columns], [...r.profile.rows.map(x => [`Cluster ${x.cluster}`, ...x.values]), ['Overall', ...r.profile.overall]])}
      ${notices('warn', r.notes)}</div>`;
  draw('seg-canvas', { type: 'scatter', data: { datasets: sc }, options: { responsive: true, maintainAspectRatio: false, scales: { x: axis(r.x_feature), y: axis(r.y_feature) } } });
}
function wireSegment() {
  $('#s-feats').addEventListener('change', updateSegAxes);
  $('#s-go').onclick = e => run(runSegment, e.target);
}

// ------------------------------------------------------------------ churn & credit (shared classifier UI)
const API = { churn: { train: '/api/churn/train', predict: '/api/churn/predict' }, credit: { train: '/api/credit-risk/train', predict: '/api/credit-risk/predict' } };
const WORDS = { churn: ['churned', 'Churn prediction', 'Predict which customers are likely to leave. Choose the target column, confirm which value means “churned”, then train and evaluate a model on held-out test data.'],
  credit: ['risky / defaulted', 'Credit risk model', 'Requires a column with a real default or credit-risk label. Choose it, confirm which value is the risky outcome, then train. Results are experimental estimates for learning only.'] };

function classifierView(k) {
  const [w, title, intro] = WORDS[k];
  return `<div class="card"><h3>${title}</h3><p class="muted">${intro}</p>
    <div class="row"><label>Target column<select id="${k}-target"></select></label>
    <label>Value meaning “${w}”<select id="${k}-pos"></select></label>
    <label>Model<select id="${k}-model"><option value="logreg">Logistic Regression (baseline)</option><option value="rf">Random Forest</option></select></label>
    <label>Test data share<select id="${k}-test"><option value="0.2">20%</option><option value="0.25">25%</option><option value="0.3">30%</option></select></label></div>
    <div id="${k}-classes"></div>
    <label class="check"><input type="checkbox" id="${k}-confirm"> <span id="${k}-confirm-txt">I confirm the selected value means “${w}”.</span></label>
    <label class="check"><input type="checkbox" id="${k}-bal"> Balance classes (helps when the outcome is rare)</label>
    <p class="muted small">Input features — identifiers, free text and dates are excluded automatically.</p><div class="chips" id="${k}-feats"></div>
    <div class="actions"><button class="btn" id="${k}-train">Train model</button></div></div><div id="${k}-out"></div>`;
}
function creditExtra() {
  return `<div class="card"><h3>No default label? Descriptive risk flags</h3>
    <p class="muted">A transparent rule-based screen, <b>not</b> a trained model. A customer is flagged on a column when they fall in the riskiest percentile tail of that column; “high-flag” customers meet at least half of the selected rules.</p>
    <p class="muted small">Higher value = riskier (e.g. loan amount)</p><div class="chips" id="cr-high"></div>
    <p class="muted small">Lower value = riskier (e.g. balance, credit score)</p><div class="chips" id="cr-low"></div>
    <div class="row"><label>Tail percentile<input type="number" id="cr-pct" value="90" min="50" max="99"></label></div>
    <button class="btn ghost" id="cr-go">Flag risk indicators</button><div id="cr-out"></div></div>`;
}
function renderFeats(k) {
  if (!DS) return;
  const t = $(`#${k}-target`).value;
  const ok = DS.columns.filter(c => ['numeric', 'binary', 'categorical'].includes(c.kind) && c.name !== t).map(c => c.name);
  chips(`${k}-feats`, ok, () => true);
}
async function loadClasses(k) {
  const col = $(`#${k}-target`).value;
  renderFeats(k);
  if (!col) { $(`#${k}-pos`).innerHTML = '<option value="">Choose a target first</option>'; $(`#${k}-classes`).innerHTML = ''; return; }
  const r = await api('/api/target/classes?column=' + encodeURIComponent(col));
  $(`#${k}-pos`).innerHTML = '<option value="">Select…</option>' + r.classes.map(c => `<option value="${esc(c.value)}">${esc(c.value)} (${num(c.count)} rows, ${pct(c.pct)})</option>`).join('');
  if (r.suggested) $(`#${k}-pos`).value = r.suggested;
  $(`#${k}-confirm`).checked = false;
  $(`#${k}-classes`).innerHTML = r.binary
    ? notice('info', `Detected two classes in “${col}”.${r.suggested ? ` Suggested positive value: ${r.suggested}.` : ''} Please check it and tick the confirmation box.${r.missing ? ` ${num(r.missing)} rows with a missing target will be ignored.` : ''}`)
    : notice('warn', `“${col}” has ${r.n_classes} distinct values. Only binary (two-class) targets are supported in this version.`);
}
async function trainModel(k) {
  const target = $(`#${k}-target`).value, pos = $(`#${k}-pos`).value;
  if (!target) throw new Error('Select a target column.');
  if (!pos) throw new Error('Select which value represents the outcome you want to predict.');
  if (!$(`#${k}-confirm`).checked) throw new Error('Please tick the confirmation box to confirm the class meaning.');
  const feats = picked(`${k}-feats`);
  if (!feats.length) throw new Error('Select at least one input feature.');
  const r = await api(API[k].train, { method: 'POST', json: { target, positive_class: pos, confirmed: true, features: feats, model: $(`#${k}-model`).value,
    test_size: Number($(`#${k}-test`).value), balance: $(`#${k}-bal`).checked } });
  showModel(k, r);
  toast('Model trained', 'ok');
}
function showModel(k, r) {
  const m = r.metrics, neg = r.classes.find(c => c !== r.positive) || 'other';
  const cm = r.confusion;
  $(`#${k}-out`).innerHTML = `
    <div class="card"><h3>Model evaluation (held-out test set)</h3>
      ${r.disclaimer ? notice('warn', r.disclaimer) : ''}${notices('warn', r.warnings)}
      <div class="cards">${[['Accuracy', m.accuracy], ['Precision', m.precision], ['Recall', m.recall], ['F1-score', m.f1], ['ROC-AUC', m.roc_auc]].map(([l, v]) => stat(l, v === null ? 'n/a' : v.toFixed(3))).join('')}</div>
      <p class="muted small">Trained on ${num(r.n_train)} rows, tested on ${num(r.n_test)} rows. Always predicting the majority class would score ${pct(m.baseline_accuracy)} accuracy. Positive class: “${esc(r.positive)}” in “${esc(r.target)}”. Preprocessing was fitted on training data only.</p>
      <div class="grid2"><div><h3>Confusion matrix</h3><div class="cm"><div class="h"></div><div class="h">Predicted “${esc(neg)}”</div><div class="h">Predicted “${esc(r.positive)}”</div>
        <div class="h">Actual “${esc(neg)}”</div><div class="good">${num(cm.tn)}</div><div class="bad">${num(cm.fp)}</div>
        <div class="h">Actual “${esc(r.positive)}”</div><div class="bad">${num(cm.fn)}</div><div class="good">${num(cm.tp)}</div></div></div>
      <div><h3>ROC curve</h3><div class="chart-box sm"><canvas id="${k}-roc"></canvas></div></div></div>
      <h3 style="margin-top:20px">Feature importance</h3><p class="muted small">${esc(r.importance_note)}</p><div class="chart-box"><canvas id="${k}-imp"></canvas></div></div>
    <div class="card"><h3>Score every customer</h3>
      <p class="muted">Apply the trained model to all ${num(r.total_rows)} rows. Lower the threshold to catch more positives (higher recall, lower precision).</p>
      <div class="row"><label>Probability threshold<input type="number" id="${k}-thr" value="0.5" min="0.01" max="0.99" step="0.05"></label></div>
      <div class="actions"><button class="btn" id="${k}-predict">Predict</button>
      <a class="btn ghost" href="/api/download/predictions?kind=${k}">Download predictions CSV</a><a class="btn ghost" href="/api/download/model?kind=${k}">Download model file</a></div>
      <div id="${k}-pred"></div></div>`;
  $(`#${k}-predict`).onclick = e => run(() => predict(k), e.target);
  if (r.roc.fpr.length) draw(`${k}-roc`, { type: 'line', data: { labels: r.roc.fpr.map(v => v.toFixed(2)),
    datasets: [{ label: 'Model', data: r.roc.tpr.map((y, i) => ({ x: r.roc.fpr[i], y })), borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.1)', fill: true, pointRadius: 0, tension: 0.1 },
      { label: 'Chance', data: [{ x: 0, y: 0 }, { x: 1, y: 1 }], borderColor: '#94a3b8', borderDash: [6, 6], pointRadius: 0 }] },
    options: { responsive: true, maintainAspectRatio: false, parsing: false, scales: { x: { ...axis('False positive rate'), type: 'linear', min: 0, max: 1 }, y: { ...axis('True positive rate'), min: 0, max: 1 } } } });
  draw(`${k}-imp`, { type: 'bar', data: { labels: r.importance.map(i => i.feature), datasets: [{ label: 'Importance', data: r.importance.map(i => i.importance),
    backgroundColor: r.importance.map(i => (r.model === 'logreg' && i.signed < 0) ? '#f59e0b' : '#2563eb'), borderRadius: 5 }] },
    options: { indexAxis: 'y', responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: axis(''), y: { grid: { display: false } } } } });
}
async function predict(k) {
  const r = await api(API[k].predict, { method: 'POST', json: { threshold: Number($(`#${k}-thr`).value) } });
  $(`#${k}-pred`).innerHTML = `${r.disclaimer ? notice('warn', r.disclaimer) : ''}
    ${notice('good', `${num(r.predicted_positive)} of ${num(r.rows)} customers (${pct(r.rate)}) are predicted as “${r.positive}” at a ${r.threshold} threshold.`)}
    <div class="grid2"><div><h3>Probability distribution</h3><div class="chart-box sm"><canvas id="${k}-hist"></canvas></div></div>
    <div><h3>Ten highest-probability rows</h3>${table(['Row #', 'Probability'], r.top.map(t => [t.row, t.probability.toFixed(3)]), { raw: true })}</div></div>`;
  draw(`${k}-hist`, { type: 'bar', data: { labels: r.histogram.map((_, i) => `${(i / 10).toFixed(1)}–${((i + 1) / 10).toFixed(1)}`), datasets: [{ label: 'Customers', data: r.histogram, backgroundColor: '#2563eb', borderRadius: 4 }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: axis('Predicted probability'), y: axis('Customers') } } });
  if (k === 'credit' || k === 'churn') run(loadDashboardCardsOnly);
}
async function loadDashboardCardsOnly() { renderCards((await api('/api/dashboard')).cards); }
async function runRules() {
  const high = picked('cr-high'), low = picked('cr-low');
  if (!high.length && !low.length) throw new Error('Select at least one risk indicator column.');
  const r = await api('/api/credit-risk/descriptive', { method: 'POST', json: { high_features: high, low_features: low, percentile: Number($('#cr-pct').value) } });
  $('#cr-out').innerHTML = `${notice('warn', r.disclaimer)}${notice('good', `${num(r.high_count)} customers (${pct(r.high_pct)}) meet at least ${r.min_flags} of ${r.indicators.length} risk flag(s).`)}
    <div class="grid2"><div>${table(['Indicator', 'Riskier when', 'Threshold', 'Customers flagged'], r.indicators.map(i => [i.feature, i.direction === 'high' ? 'Higher' : 'Lower', i.threshold, i.flagged]))}</div>
    <div><div class="chart-box sm"><canvas id="cr-dist"></canvas></div></div></div>`;
  draw('cr-dist', { type: 'bar', data: { labels: r.distribution.map(d => d.flags + ' flag(s)'), datasets: [{ label: 'Customers', data: r.distribution.map(d => d.count), backgroundColor: '#2563eb', borderRadius: 5 }] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: axis('Rule-based flags per customer'), y: axis('Customers') } } });
  run(loadDashboardCardsOnly);
}
function wireClassifiers() {
  $('#churn-root').innerHTML = classifierView('churn');
  $('#credit-root').innerHTML = classifierView('credit') + creditExtra();
  ['churn', 'credit'].forEach(k => {
    $(`#${k}-target`).onchange = () => run(() => loadClasses(k));
    $(`#${k}-pos`).onchange = () => { $(`#${k}-confirm`).checked = false; };
    $(`#${k}-train`).onclick = e => run(() => trainModel(k), e.target);
  });
  $('#cr-go').onclick = e => run(runRules, e.target);
}

// ------------------------------------------------------------------ trend
async function runTrend() {
  if (!$('#t-date').value) throw new Error('No date column was detected. Use Data cleaning → Fix data types to convert a text column to a date.');
  const agg = $('#t-agg').value, metric = $('#t-metric').value;
  const r = await api('/api/forecast', { method: 'POST', json: { date_column: $('#t-date').value, metric: metric || null, agg: metric ? agg : 'count',
    freq: $('#t-freq').value, window: Number($('#t-win').value), horizon: Number($('#t-hor').value) } });
  const s = r.stats, n = r.labels.length;
  const labels = [...r.labels, ...r.forecast_labels];
  const actual = [...r.actual, ...r.forecast_labels.map(() => null)];
  const fc = [...r.labels.map((_, i) => (i === n - 1 ? r.actual[n - 1] : null)), ...r.forecast];
  const ma = [...r.moving_average, ...r.forecast_labels.map(() => null)];
  const ch = v => (v === null || v === undefined ? '—' : (v > 0 ? '+' : '') + pct(v));
  $('#trend-out').innerHTML = `<div class="cards">${stat('Periods analysed', num(r.periods), `Grouped by ${r.freq === 'M' ? 'month' : r.freq === 'W' ? 'week' : 'day'}`)}${stat('First → last', `${num(s.first)} → ${num(s.last)}`, ch(s.change_pct) + ' overall')}
      ${stat('Latest period change', ch(s.last_change_pct))}${stat('Linear trend', esc(s.trend), 'Direction over the whole history')}${stat('Baseline error (MAE)', num(s.mae_moving_average), `Naive “same as last” MAE: ${num(s.mae_naive)}`)}</div>
    <div class="card"><h3>${esc(r.metric_label)} — actual vs forecast</h3>${notice('info', r.summary)}<div class="chart-box"><canvas id="trend-canvas"></canvas></div>${notices('warn', r.warnings)}</div>`;
  draw('trend-canvas', { type: 'line', data: { labels, datasets: [
    { label: 'Actual (historical)', data: actual, borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.1)', fill: true, tension: 0.25, pointRadius: n > 60 ? 0 : 3 },
    { label: `Moving average (${r.window})`, data: ma, borderColor: '#94a3b8', borderWidth: 1.5, pointRadius: 0, tension: 0.25 },
    { label: 'Forecast (baseline)', data: fc, borderColor: '#f59e0b', borderDash: [8, 5], borderWidth: 2.5, pointRadius: 4, pointBackgroundColor: '#f59e0b', tension: 0 }] },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, scales: { x: { ...axis(r.date_column), ticks: { maxRotation: 60, autoSkip: true } }, y: axis(r.metric_label) } } });
}

// ------------------------------------------------------------------ boot
document.addEventListener('DOMContentLoaded', async () => {
  buildNav(); wireClassifiers(); wireUpload(); wireClean(); wireEda(); wireSegment();
  $('#logout').onclick = e => run(async () => { await api('/api/auth/logout', { method: 'POST', json: {} }); window.location.href = '/login'; }, e.target);
  $('#t-go').onclick = e => run(runTrend, e.target);
  $('#rep-refresh').onclick = e => run(loadInsights, e.target);
  $('#t-agg').onchange = () => { if ($('#t-agg').value === 'count') $('#t-metric').value = ''; };
  try { CFG = await api('/api/config'); } catch (e) { /* keep defaults */ }
  setLimits();
  try { DS = await api('/api/dataset/info'); } catch (e) { DS = null; }
  renderCards(null);
  afterChange();
  if (DS) renderUpload();
  go('dashboard');
});
