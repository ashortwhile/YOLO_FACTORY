'use strict';
/* YOLO Studio front-end. No external libraries, works fully offline. */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtSize = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1024).toFixed(0) + ' KB';
const COLORS = ['#e74c3c', '#2563eb', '#16a34a', '#d97706', '#8b5cf6', '#0d9488',
                '#db2777', '#0891b2', '#65a30d', '#dc2626'];

async function api(path, body) {
  const auth = window.YSTUDIO_TOKEN ? { 'X-YStudio-Token': window.YSTUDIO_TOKEN } : {};
  const opt = body === undefined ? { headers: auth } : {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, auth),
    body: JSON.stringify(body)
  };
  const r = await fetch(path, opt);
  let j;
  try { j = await r.json(); } catch (e) { throw new Error('服务返回异常: ' + r.status); }
  if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
function toast(msg, err) {
  const t = $('toast'); t.textContent = msg; t.className = 'toast show' + (err ? ' err' : '');
  clearTimeout(t._timer); t._timer = setTimeout(() => t.className = 'toast', 3000);
}
function notice(el, msg, warn) {
  el.textContent = msg || ''; el.className = 'notice show' + (warn ? ' warn' : '');
  if (!msg) el.className = 'notice';
}
let modalResolve = null;
function showModal(title, html, onOk) {
  $('modalTitle').textContent = title; $('modalBody').innerHTML = html;
  $('modal').classList.remove('hidden'); modalResolve = onOk;
  const first = $('modalBody').querySelector('input, select, textarea, button');
  if (first) first.focus();
}
async function closeModal(ok) {
  const fn = modalResolve; modalResolve = null;
  if (!ok || !fn) { $('modal').classList.add('hidden'); return; }
  let result;
  try { result = await fn(); }
  catch (e) { toast(e.message, true); return; }
  // Validation failed: keep the dialog open so the user can fix the input.
  if (result === false) return;
  $('modal').classList.add('hidden');
}
$('modalOk').onclick = () => closeModal(true);
$('modalCancel').onclick = () => closeModal(false);
$('modal').onclick = (e) => { if (e.target.id === 'modal') closeModal(false); };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('modal').classList.contains('hidden')) {
    e.preventDefault(); closeModal(false);
  }
});

function fillSelect(sel, items, selected) {
  sel.innerHTML = (items || []).map(o => {
    const v = typeof o === 'string' ? o : o.value;
    const l = typeof o === 'string' ? o : o.label;
    return `<option value="${esc(v)}" ${String(v) === String(selected) ? 'selected' : ''}>${esc(l)}</option>`;
  }).join('');
}

const S = {
  view: 'home', datasets: [], models: { local: [], presets: [] }, config: {}, status: {},
  hw: null, runs: [], activeDs: '', activeRun: '',
  train: { taskId: '', watching: '', epochs: 0, metrics: [], log: '', offset: 0 },
  predict: { boxes: [], img: null, busy: false, taskId: '' },
};

/* ---------------- navigation ---------------- */
function go(view) {
  S.view = view;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === view));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + view));
  document.querySelectorAll('[data-go]').forEach(b => b.onclick = () => go(b.dataset.go));
  if (view === 'home') renderHome();
  if (view === 'datasets') renderDatasets();
  if (view === 'label') initLabel();
  if (view === 'train') initTrain();
  if (view === 'predict') initPredict();
  if (view === 'runs') renderRuns();
  if (view === 'env') renderEnv();
}
document.querySelectorAll('.nav-item').forEach(n => n.onclick = () => go(n.dataset.view));
document.querySelectorAll('[data-go]').forEach(b => b.onclick = () => go(b.dataset.go));
$('btnHelpTop').onclick = () => go('help');
let statusTimer = null;
function stopBackgroundPolling() {
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
  if (typeof cancelAllTaskWatches === 'function') cancelAllTaskWatches();
}
window.addEventListener('pagehide', stopBackgroundPolling);
$('btnQuit').onclick = () => showModal('退出软件',
  '<p>将关闭本地服务，并终止仍在运行的训练 / 推理任务。标注内容已自动保存，确认退出？</p>', () => {
    stopBackgroundPolling();
    api('/api/shutdown', {}).catch(() => {});
    setTimeout(() => window.close(), 600);
  });

/* ---------------- SVG line chart ---------------- */
function lineChart(series, height) {
  const W = 620, H = height || 200, P = { l: 42, r: 12, t: 18, b: 24 };
  const all = series.flatMap(s => s.values).filter(v => isFinite(v));
  if (!all.length) return '';
  let lo = Math.min(...all), hi = Math.max(...all);
  if (hi - lo < 1e-6) { hi += 0.1; lo -= 0.1; }
  const pad = (hi - lo) * 0.1; lo -= pad; hi += pad;
  const n = Math.max(...series.map(s => s.values.length), 2);
  const X = i => P.l + (W - P.l - P.r) * (i / (n - 1));
  const Y = v => H - P.b - (H - P.t - P.b) * ((v - lo) / (hi - lo));
  let g = '';
  for (let k = 0; k <= 4; k++) {
    const y = P.t + (H - P.t - P.b) * k / 4;
    const val = hi - (hi - lo) * k / 4;
    g += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="#eef2f7"/>` +
         `<text x="${P.l - 6}" y="${y + 4}" text-anchor="end" font-size="9" fill="#94a3b8">${val.toFixed(2)}</text>`;
  }
  series.forEach(s => {
    const pts = s.values.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
    if (pts) g += `<polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round"/>`;
  });
  let legend = '';
  series.forEach((s, i) => {
    legend += `<g transform="translate(${P.l + i * 118},${H - 4})">` +
      `<rect width="9" height="3" y="-4" fill="${s.color}"/>` +
      `<text x="13" font-size="9" fill="#64748b">${esc(s.name)}</text></g>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${g}${legend}</svg>`;
}
function chartFromRows(rows) {
  const get = (r, ...keys) => {
    for (const k of keys) if (r[k] !== undefined && r[k] !== '') return parseFloat(r[k]);
    return NaN;
  };
  return [
    { name: 'mAP50', color: '#2563eb', values: rows.map(r => get(r, 'metrics/mAP50(B)', 'map50')).filter(isFinite) },
    { name: 'mAP50-95', color: '#16a34a', values: rows.map(r => get(r, 'metrics/mAP50-95(B)', 'map')).filter(isFinite) },
    { name: 'box_loss', color: '#e74c3c', values: rows.map(r => get(r, 'train/box_loss', 'box_loss')).filter(isFinite) },
  ];
}

/* ---------------- shared loaders ---------------- */
async function loadCore() {
  try {
    const [st, ds, models, cfg] = await Promise.all([
      api('/api/status'), api('/api/datasets'), api('/api/models'), api('/api/config')
    ]);
    S.status = st; S.datasets = ds; S.models = models; S.config = cfg;
    S.hw = st.hardware && Object.keys(st.hardware).length ? st.hardware : S.hw;
    if (st.version) $('sideVersion').textContent = 'v' + st.version;
    updateBadges();
    fillDatasetSelectors();
    fillModelSelectors();
  } catch (e) { toast('初始化失败: ' + e.message, true); }
}
function updateBadges() {
  const h = S.hw || {};
  const g = (h.gpus || [])[0];
  const b = $('hwBadge');
  if (g) {
    const vram = g.vram_mb >= 1024 ? (g.vram_mb / 1024).toFixed(0) + 'GB' : g.vram_mb + 'MB';
    b.textContent = `${g.vendor.toUpperCase()} ${vram} · ${h.ready ? '加速就绪' : 'CPU 模式'}`;
    b.className = 'badge ' + (h.ready ? 'badge-ok' : 'badge-warn');
  } else { b.textContent = 'CPU 模式'; b.className = 'badge badge-muted'; }
  const n = S.status.tasks_running || 0;
  $('taskBadge').textContent = n ? n + ' 个任务运行中' : '空闲';
  $('taskBadge').className = 'badge ' + (n ? 'badge-info' : 'badge-muted');
}
function datasetNames() { return S.datasets.map(d => ({ value: d.name, label: d.name })); }
function fillDatasetSelectors() {
  ['lbDataset', 'trDataset', 'prDataset'].forEach(id => {
    const el = $(id); if (!el) return;
    const keep = el.value; fillSelect(el, datasetNames(), keep || (S.datasets[0] && S.datasets[0].name));
  });
}
function modelItems() {
  const local = (S.models.local || []).map(m => ({ value: m.path, label: m.name }));
  const online = (S.onlineModels || []).map(m => ({ value: m.name, label: m.name }));
  const presets = (S.models.presets || []).map(m => ({ value: m.name, label: m.name }));
  // online catalogue first (always current), then anything installed locally.
  const seen = new Set();
  const out = [];
  for (const it of [...online, ...local.filter(x => !x.label.includes('weights/')), ...presets]) {
    if (seen.has(it.label)) continue;
    seen.add(it.label);
    out.push(it);
  }
  return out;
}
function fillModelSelectors() {
  const def = S.config.default_model || 'yolo26n.pt';
  ['trModel', 'prModel', 'cfgModel'].forEach(id => {
    const el = $(id); if (!el) return;
    const keep = el.value || (id === 'prModel' ? (S.models.local[0] || {}).path : def);
    fillSelect(el, modelItems(), keep);
  });
}
function deviceItems() {
  const opts = (S.hw && S.hw.device_options) || [{ value: 'auto', label: '自动' }, { value: 'cpu', label: 'CPU' }];
  return opts.map(o => ({ value: o.value, label: o.label + (o.ready === false ? '（未启用）' : '') }));
}
function fillDeviceSelectors() {
  ['trDevice', 'prDevice'].forEach(id => {
    const el = $(id); if (!el) return;
    const keep = el.value || (S.config.device && S.config.device !== 'auto' ? S.config.device : 'auto');
    fillSelect(el, deviceItems(), keep);
  });
}

/* ---------------- home ---------------- */
async function renderHome() {
  try {
    const [st, runs] = await Promise.all([api('/api/status'), api('/api/runs')]);
    S.status = st; S.runs = runs; updateBadges();
    const images = S.datasets.reduce((a, d) => a + d.train.images + d.val.images, 0);
    const labeled = S.datasets.reduce((a, d) => a + d.train.labeled + d.val.labeled, 0);
    const cards = [
      ['数据集', S.datasets.length], ['图片', images], ['已标注', labeled],
      ['模型', S.models.local.length], ['训练记录', runs.length]
    ];
    const icons = ['▦', '▤', '✓', '◈', '▶'];
    $('homeStats').innerHTML = cards.map((c, i) =>
      `<div class="stat"><div class="stat-top"><span class="stat-ico">${icons[i]}</span>
       <span class="stat-spark"></span></div><div class="num">${c[1]}</div>
       <div class="lbl">${c[0]}</div></div>`).join('');
    const h = S.hw || {};
    const g = (h.gpus || [])[0];
    const rows = [
      ['显卡', g ? g.name : '未检测到（CPU）'],
      ['显存', g ? (g.vram_mb / 1024).toFixed(1) + ' GB' : '-'],
      ['驱动', g ? g.driver : '-'],
      ['当前后端', (h.active || {}).backend || 'cpu'],
      ['推荐后端', (h.recommended || {}).backend + ' / ' + (h.recommended || {}).device],
      ['PyTorch', ((h.torch || {}).version || '-')],
      ['Ultralytics', ((h.torch || {}).ultralytics || '-')],
      ['状态', h.ready ? '加速已就绪' : '未启用显卡加速（当前 CPU）'],
    ];
    $('homeHardware').innerHTML = rows.map(r =>
      `<div class="k">${esc(r[0])}</div><div class="v">${esc(r[1])}</div>`).join('') +
      (h.ready ? '' : `<div class="k"></div><div class="v"><button class="btn btn-sm btn-primary" id="homeEnable">一键启用显卡加速</button></div>`);
    const he = $('homeEnable');
    if (he) he.onclick = () => go('env');
    $('homeRuns').innerHTML = runs.length ? runs.slice(0, 4).map(r =>
      `<div class="ds-item" data-run="${esc(r.dir)}"><b>${esc(r.name)}</b>
       <div class="meta"><span>${r.epochs} epochs · mAP50 ${esc(r.best_map || '-')}</span>
       <span>${new Date(r.mtime * 1000).toLocaleString('zh-CN')}</span></div></div>`).join('')
      : '';
    $('homeRuns').querySelectorAll('[data-run]').forEach(el => el.onclick = () => {
      S.activeRun = el.dataset.run; go('runs');
    });
  } catch (e) { toast(e.message, true); }
}
$('btnRefreshHome').onclick = async () => { await loadCore(); renderHome(); };

/* ---------------- datasets ---------------- */
async function renderDatasets() {
  try {
    S.datasets = await api('/api/datasets'); fillDatasetSelectors();
    $('dsList').innerHTML = S.datasets.length ? S.datasets.map(d =>
      `<div class="ds-item ${d.name === S.activeDs ? 'active' : ''}" data-ds="${esc(d.name)}">
        <b>${esc(d.name)}</b> <span class="badge badge-muted">${d.kind === 'external' ? '已挂载' : '本地'}</span>
        <div class="meta"><span>${d.train.images + d.val.images} 图 · ${d.train.labeled + d.val.labeled} 已标注 · ${d.classes.length} 类</span>
        <span>${d.train.boxes + d.val.boxes} 框</span></div></div>`).join('')
      : '';
    $('dsList').querySelectorAll('[data-ds]').forEach(el => el.onclick = () => {
      S.activeDs = el.dataset.ds; renderDatasets();
    });
    if (!S.activeDs && S.datasets.length) S.activeDs = S.datasets[0].name;
    const d = S.datasets.find(x => x.name === S.activeDs);
    if (!d) { $('dsDetail').innerHTML = ''; return; }
    $('dsDetail').innerHTML = `
      ${(d.val.images > 0 && d.val.labeled === 0)
        ? '<div class="warn show">验证集图片还没有标签，训练时无法计算 mAP。请到标注页切换到 val 完成标注。</div>' : ''}
      <div class="kv-list">
        <div class="k">根目录</div><div class="v">${esc(d.root)}</div>
        <div class="k">train</div><div class="v">${d.train.images} 图 / ${d.train.labeled} 已标注 / ${d.train.boxes} 框</div>
        <div class="k">val</div><div class="v">${d.val.images} 图 / ${d.val.labeled} 已标注 / ${d.val.boxes} 框</div>
        <div class="k">类别</div><div class="v">${esc(d.classes.join('、') || '未设置')}</div>
      </div>
      <div class="divider"></div><h4>编辑类别（保存会同步更新 data.yaml）</h4>
      <textarea id="dsClassesEdit" rows="4">${esc(d.classes.join('\n'))}</textarea>
      <div class="btn-row"><button class="btn btn-primary btn-sm" id="btnSaveClasses">保存类别</button>
        <button class="btn btn-sm" id="btnOpenDs">打开目录</button></div>
      <div class="divider"></div><h4>导入图片（自动划分 train / val）</h4>
      <label>图片文件夹（点「浏览」弹出文件管理器选择）<div class="input-row"><input id="dsImportSrc" placeholder="也可直接粘贴路径"><button class="btn btn-sm" id="btnBrowseImport" type="button">浏览…</button></div></label>
      <label>验证集比例<input id="dsImportRatio" type="number" value="0.2" min="0" max="0.9" step="0.05"></label>
      <div class="btn-row"><button class="btn btn-primary btn-sm" id="btnImportImgs">导入整个文件夹</button></div>
      <div class="divider"></div><h4>重新划分 train / val</h4>
      <label>验证集比例<input id="dsSplitRatio" type="number" value="0.2" min="0" max="0.9" step="0.05"></label>
      <button class="btn btn-sm" id="btnSplit">执行划分（移动图片与标签）</button>
      <div class="divider"></div><h4>data.yaml</h4>
      <pre class="logbox small">${esc(d.yaml_text || ('path: ' + d.root + '\ntrain: images/train\nval: images/val\nnc: ' + d.classes.length))}</pre>`;
    $('btnSaveClasses').onclick = async () => {
      const classes = $('dsClassesEdit').value.split(/[\n,]/).map(x => x.trim()).filter(Boolean);
      if (!classes.length) return toast('至少需要一个类别', true);
      try { await api('/api/dataset/classes', { name: d.name, classes }); toast('类别与 data.yaml 已更新'); renderDatasets(); }
      catch (e) { toast(e.message, true); }
    };
    $('btnOpenDs').onclick = () => api('/api/open', { path: d.root }).catch(e => toast(e.message, true));
    $('btnBrowseImport').onclick = async () => {
      toast('已弹出文件夹选择窗口，请在屏幕上选择图片文件夹…');
      try {
        const picked = await api('/api/pick_folder', {});
        if (picked.canceled || !picked.path) return toast('已取消选择');
        $('dsImportSrc').value = picked.path;
        const r = await api('/api/dataset/import', {
          name: d.name, src_dir: picked.path,
          val_ratio: parseFloat($('dsImportRatio').value)
        });
        toast(`已从所选文件夹导入 ${r.imported} 张图片，自动划分 train / val`);
        renderDatasets();
      } catch (e) { toast(e.message, true); }
    };
    $('btnImportImgs').onclick = async () => {
      try {
        const r = await api('/api/dataset/import', {
          name: d.name, src_dir: $('dsImportSrc').value.trim(),
          val_ratio: parseFloat($('dsImportRatio').value)
        });
        toast(`已导入 ${r.imported} 张图片，已自动划分 train / val`);
        renderDatasets();
      } catch (e) { toast(e.message, true); }
    };
    $('btnSplit').onclick = () => {
      const ratio = parseFloat($('dsSplitRatio').value);
      showModal('重新划分数据集',
        `<p>将按 <b>${esc(String(ratio))}</b> 的比例在 train / val 之间移动图片与标签文件。` +
        '此操作会改变文件位置且不可撤销，建议先备份数据集。确认继续？</p>', async () => {
        const btn = $('btnSplit'); if (!btn || btn.disabled) return;
        btn.disabled = true;
        try {
          const r = await api('/api/dataset/split', { name: d.name, ratio });
          toast(`划分完成，移动了 ${r.moved} / ${r.total} 张`); renderDatasets();
        } catch (e) { toast(e.message, true); }
        finally { if (btn.isConnected) btn.disabled = false; }
      });
    };
  } catch (e) { toast(e.message, true); }
}
$('btnRefreshDs').onclick = renderDatasets;
$('btnBrowseNewSrc').onclick = async () => {
  toast('已弹出文件夹选择窗口，请在屏幕上选择图片文件夹…');
  try {
    const picked = await api('/api/pick_folder', {});
    if (picked.canceled || !picked.path) return toast('已取消选择');
    $('newDsSrc').value = picked.path;
    toast('已选择：' + picked.path + '（点击「创建并导入」完成）');
  } catch (e) { toast(e.message, true); }
};
$('btnCreateDs').onclick = async () => {
  const name = $('newDsName').value.trim();
  const classes = $('newDsClasses').value.split(/[\n,]/).map(x => x.trim()).filter(Boolean);
  if (!name || !classes.length) return toast('请填写名称和类别', true);
  try {
    const r = await api('/api/dataset/create', {
      name, classes, src_dir: $('newDsSrc').value.trim(),
      val_ratio: parseFloat($('newDsRatio').value)
    });
    toast(`数据集已创建，导入 ${r.imported} 张图片`); S.activeDs = name; await loadCore(); renderDatasets();
  } catch (e) { toast(e.message, true); }
};
$('btnAttachDs').onclick = async () => {
  const root = $('attachRoot').value.trim();
  if (!root) return toast('请填写目录路径', true);
  try {
    const r = await api('/api/dataset/attach', { root, name: $('attachName').value.trim() });
    toast('已挂载 ' + r.name); S.activeDs = r.name; await loadCore(); renderDatasets();
  } catch (e) { toast(e.message, true); }
};
$('btnDiscover').onclick = async () => {
  try {
    const r = await api('/api/dataset/discover', {});
    toast(r.datasets.length ? `发现 ${r.datasets.length} 个数据集并已登记` : '同级目录未发现 YOLO 数据集');
    await loadCore(); renderDatasets();
  } catch (e) { toast(e.message, true); }
};

/* ---------------- label tool ---------------- */
const L = {
  ds: '', split: 'train', filter: 'all', images: [], idx: -1, classes: [], activeCls: 0,
  boxes: [], selected: -1, img: null, scale: 1, dirty: false, autosave: true,
  drag: null, preview: null, canvas: null, ctx: null,
  // undo / redo
  history: [], histIdx: -1,
  // canvas view (CSS transform; getBoundingClientRect already includes it, so
  // canvasPos() needs no change)
  zoom: 1, panX: 0, panY: 0, spaceDown: false,
  // touch pinch
  pinch: null
};
/* ---- undo / redo ---- */
function snapshot() { return L.boxes.map(b => ({ ...b })); }
function pushHistory() {
  L.history = L.history.slice(0, L.histIdx + 1);
  L.history.push(snapshot());
  if (L.history.length > 80) L.history.shift();
  L.histIdx = L.history.length - 1;
  updateHistoryButtons();
}
function resetHistory() { L.history = [snapshot()]; L.histIdx = 0; updateHistoryButtons(); }
function applyHistory() {
  L.boxes = snapshotOf(L.history[L.histIdx]);
  L.selected = -1; L.dirty = true;
  drawLabel(); renderBoxList(); updateHistoryButtons();
}
function snapshotOf(boxes) { return (boxes || []).map(b => ({ ...b })); }
function undoLabel() {
  if (L.histIdx <= 0) return;
  L.histIdx--; applyHistory();
}
function redoLabel() {
  if (L.histIdx >= L.history.length - 1) return;
  L.histIdx++; applyHistory();
}
function updateHistoryButtons() {
  const u = $('lbUndo'), r = $('lbRedo');
  if (u) u.disabled = L.histIdx <= 0;
  if (r) r.disabled = L.histIdx >= L.history.length - 1;
}

/* ---- canvas view: zoom / pan via CSS transform ---- */
function applyView() {
  if (!L.canvas) return;
  L.canvas.style.transform = `translate(${L.panX}px,${L.panY}px) scale(${L.zoom})`;
  const el = $('lbZoomVal');
  if (el) el.textContent = Math.round(L.zoom * 100) + '%';
}
function setZoom(z) {
  L.zoom = clamp(z, 0.25, 8);
  applyView();
}
function zoomAt(factor, cssX, cssY) {
  const wrap = $('canvasWrap').getBoundingClientRect();
  const cx = cssX - wrap.left, cy = cssY - wrap.top;
  const before = L.zoom;
  const after = clamp(before * factor, 0.25, 8);
  const k = after / before;
  L.panX = cx - (cx - L.panX) * k;
  L.panY = cy - (cy - L.panY) * k;
  L.zoom = after;
  applyView();
}
function resetView() { L.zoom = 1; L.panX = 0; L.panY = 0; applyView(); }

function boxRect(b) {
  const W = L.canvas.width, H = L.canvas.height;
  return { x: b.x * W, y: b.y * H, w: b.w * W, h: b.h * H };
}
function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
function normRect(x1, y1, x2, y2) {
  const W = L.canvas.width, H = L.canvas.height;
  const x = clamp(Math.min(x1, x2), 0, W), y = clamp(Math.min(y1, y2), 0, H);
  const w = clamp(Math.abs(x2 - x1), 0, W - x), h = clamp(Math.abs(y2 - y1), 0, H - y);
  return { x: x / W, y: y / H, w: w / W, h: h / H };
}
function drawBoxes(ctx, boxes, names, selected) {
  const W = ctx.canvas.width, H = ctx.canvas.height;
  boxes.forEach((b, i) => {
    const r = { x: b.x * W, y: b.y * H, w: b.w * W, h: b.h * H };
    const c = COLORS[(b.cls || 0) % COLORS.length];
    ctx.strokeStyle = c; ctx.lineWidth = i === selected ? 3 : 2;
    ctx.strokeRect(r.x, r.y, r.w, r.h);
    ctx.fillStyle = c; ctx.globalAlpha = 0.13;
    ctx.fillRect(r.x, r.y, r.w, r.h); ctx.globalAlpha = 1;
    const label = String((b.cls != null && names && names[b.cls]) || (b.cls == null ? 0 : b.cls));
    ctx.font = '12px "Microsoft YaHei", sans-serif';
    const tw = ctx.measureText(label).width + 10;
    ctx.fillStyle = c; ctx.fillRect(r.x, Math.max(0, r.y - 18), tw, 18);
    ctx.fillStyle = '#fff'; ctx.fillText(label, r.x + 5, Math.max(12, r.y - 5));
    if (i === selected) {
      ctx.fillStyle = '#fff'; ctx.strokeStyle = c;
      ctx.fillRect(r.x + r.w - 5, r.y + r.h - 5, 10, 10);
      ctx.strokeRect(r.x + r.w - 5, r.y + r.h - 5, 10, 10);
    }
  });
}
function drawLabel() {
  if (!L.ctx || !L.img) return;
  const c = L.canvas, ctx = L.ctx;
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.drawImage(L.img, 0, 0, c.width, c.height);
  drawBoxes(ctx, L.boxes, L.classes, L.selected);
  if (L.preview) {
    const r = { x: L.preview.x * c.width, y: L.preview.y * c.height,
                w: L.preview.w * c.width, h: L.preview.h * c.height };
    ctx.setLineDash([5, 4]); ctx.strokeStyle = '#2563eb'; ctx.lineWidth = 2;
    ctx.strokeRect(r.x, r.y, r.w, r.h); ctx.setLineDash([]);
  }
}
function fitCanvas() {
  const wrap = $('canvasWrap');
  const maxW = Math.max(320, wrap.clientWidth - 24);
  const maxH = Math.max(300, window.innerHeight - 310);
  L.scale = clamp(Math.min(maxW / L.img.width, maxH / L.img.height), 0.08, 2.5);
  L.canvas.width = Math.round(L.img.width * L.scale);
  L.canvas.height = Math.round(L.img.height * L.scale);
  resetView();
  drawLabel();
}
function renderClassList() {
  $('lbClassList').innerHTML = L.classes.map((c, i) =>
    `<div class="class-item ${i === L.activeCls ? 'active' : ''}" data-cls="${i}">
      <span class="sw" style="background:${COLORS[i % COLORS.length]}"></span>
      <span>${esc(c)}</span><span class="idx">${i}</span></div>`).join('');
  $('lbClassList').querySelectorAll('[data-cls]').forEach(el => el.onclick = () => {
    L.activeCls = parseInt(el.dataset.cls, 10);
    if (L.selected >= 0) { pushHistory(); L.boxes[L.selected].cls = L.activeCls; L.dirty = true; drawLabel(); renderBoxList(); }
    renderClassList();
  });
  $('lbActiveClass').textContent = L.classes[L.activeCls] || '-';
}
function renderBoxList() {
  $('lbBoxCount').textContent = L.boxes.length;
  $('lbBoxList').innerHTML = L.boxes.length ? L.boxes.map((b, i) =>
    `<div class="box-item ${i === L.selected ? 'active' : ''}" data-box="${i}">
      <span class="dot" style="background:${COLORS[(b.cls || 0) % COLORS.length]}"></span>
      #${i + 1} ${esc(L.classes[b.cls] || b.cls)}
      <select data-boxsel="${i}">${L.classes.map((c, k) =>
        `<option value="${k}" ${k === b.cls ? 'selected' : ''}>${esc(c)}</option>`).join('')}</select></div>`).join('')
    : '';
  $('lbBoxList').querySelectorAll('[data-box]').forEach(el => el.onclick = (e) => {
    if (e.target.tagName === 'SELECT') return;
    L.selected = parseInt(el.dataset.box, 10); drawLabel(); renderBoxList();
  });
  $('lbBoxList').querySelectorAll('[data-boxsel]').forEach(el => el.onchange = () => {
    pushHistory();
    L.boxes[parseInt(el.dataset.boxsel, 10)].cls = parseInt(el.value, 10);
    L.dirty = true; drawLabel(); renderBoxList();
  });
}
function renderFileList() {
  const q = ($('lbSearch').value || '').toLowerCase();
  const items = L.images.map((im, i) => ({ im, i })).filter(x => x.im.name.toLowerCase().includes(q));
  $('lbFileCount').textContent = `(${L.images.length})`;
  $('lbFileList').innerHTML = items.length ? items.map(({ im, i }) =>
    `<div class="file-item ${i === L.idx ? 'active' : ''}" data-i="${i}">
      <b>${esc(im.name)}</b><div class="meta">
      <span><span class="dot ${im.labeled ? 'ok' : 'todo'}"></span>${im.labeled ? im.boxes + ' 框' : '未标注'}</span>
      <span>${fmtSize(im.size)}</span></div></div>`).join('')
    : '<div class="hint small">没有匹配的图片</div>';
  $('lbFileList').querySelectorAll('[data-i]').forEach(el => el.onclick = () => openImage(parseInt(el.dataset.i, 10)));
  const done = L.images.filter(x => x.labeled).length;
  const totalBoxes = L.images.reduce((a, x) => a + x.boxes, 0);
  $('lbStats').innerHTML =
    `<div class="k">已标注</div><div class="v">${done} / ${L.images.length}</div>` +
    `<div class="k">标注框</div><div class="v">${totalBoxes}</div>` +
    `<div class="k">类别数</div><div class="v">${L.classes.length}</div>` +
    `<div class="k">当前划分</div><div class="v">${L.split}</div>`;
}
async function saveLabel(silent) {
  if (L.idx < 0 || !L.images[L.idx]) return true;
  try {
    await api('/api/label/save', {
      name: L.ds, split: L.split, file: L.images[L.idx].name, boxes: L.boxes
    });
    L.images[L.idx].labeled = true; L.images[L.idx].boxes = L.boxes.length;
    L.dirty = false; renderFileList(); renderBoxList();
    $('lbStatus').textContent = '已保存 ' + new Date().toLocaleTimeString('zh-CN');
    if (!silent) toast('标注已保存');
    return true;
  } catch (e) { toast(e.message, true); return false; }
}
async function loadLabelData(keepImage) {
  const ds = $('lbDataset').value; if (!ds) return;
  const split = $('lbSplit').value, filter = $('lbFilter').value;
  const keepName = keepImage && L.idx >= 0 && L.images[L.idx] ? L.images[L.idx].name : null;
  try {
    const info = await api(`/api/dataset/images?name=${encodeURIComponent(ds)}&split=${split}&filter=${filter}`);
    L.ds = ds; L.split = split; L.filter = filter;
    L.images = info.images; L.classes = info.classes;
    if (!L.classes.length) L.classes = ['object'];
    if (L.activeCls >= L.classes.length) L.activeCls = 0;
    renderClassList(); renderFileList();
    let idx = 0;
    if (keepName) { const k = L.images.findIndex(x => x.name === keepName); if (k >= 0) idx = k; }
    await openImage(idx, true);
  } catch (e) {
    $('lbStatus').textContent = '加载失败：' + e.message;
    toast(e.message, true);
  }
}
let imageToken = 0;
async function openImage(i, skipSave) {
  if (!skipSave && L.dirty && L.autosave) {
    const saved = await saveLabel(true);
    if (!saved) { toast('标注保存失败，已停留在当前图片以免丢失', true); return; }
  }
  if (!L.images.length) { L.idx = -1; return; }
  // A monotonically increasing token discards stale image/label responses when
  // the user flips through pictures quickly (out-of-order replies used to paint
  // the previous image's boxes onto the new one).
  const token = ++imageToken;
  L.idx = clamp(i, 0, L.images.length - 1);
  const item = L.images[L.idx];
  $('lbCounter').textContent = `${L.idx + 1} / ${L.images.length}`;
  $('lbImgInfo').textContent = `${item.name} · ${fmtSize(item.size)}`;
  const img = new Image();
  img.onload = async () => {
    if (token !== imageToken) return;
    L.img = img; L.selected = -1; L.preview = null;
    fitCanvas();
    let boxes = [];
    try {
      const lab = await api(`/api/label?name=${encodeURIComponent(L.ds)}&split=${L.split}&file=${encodeURIComponent(item.name)}`);
      boxes = lab.boxes || [];
    } catch (e) { /* no label file yet */ }
    if (token !== imageToken) return;
    L.boxes = boxes;
    resetHistory();
    L.dirty = false; drawLabel(); renderBoxList(); renderFileList();
  };
  img.onerror = () => { if (token === imageToken) toast('图片加载失败', true); };
  img.src = `/api/image?name=${encodeURIComponent(L.ds)}&split=${L.split}&file=${encodeURIComponent(item.name)}&t=${Date.now()}`;
}
function hitTest(x, y) {
  for (let i = L.boxes.length - 1; i >= 0; i--) {
    const r = boxRect(L.boxes[i]);
    if (x >= r.x - 5 && x <= r.x + r.w + 5 && y >= r.y - 5 && y <= r.y + r.h + 5) {
      const handle = Math.abs(x - (r.x + r.w)) < 11 && Math.abs(y - (r.y + r.h)) < 11;
      return { i, r, handle };
    }
  }
  return null;
}
function canvasPos(e) {
  const r = L.canvas.getBoundingClientRect();
  return { x: (e.clientX - r.left) * (L.canvas.width / r.width),
           y: (e.clientY - r.top) * (L.canvas.height / r.height) };
}
function bindCanvas() {
  const c = L.canvas;
  // Pointer helpers shared by mouse and touch. Coordinates come from the
  // *untransformed* canvas box, so CSS zoom/pan cancels out automatically.
  function down(p, panMode) {
    if (!L.img) return;
    if (panMode) { L.drag = { mode: 'pan', cx: p.clientX, cy: p.clientY }; return; }
    const hit = hitTest(p.x, p.y);
    if (hit) {
      L.selected = hit.i;
      L.drag = hit.handle
        ? { mode: 'resize', i: hit.i, sx: p.x, sy: p.y, b: { ...L.boxes[hit.i] }, changed: false }
        : { mode: 'move', i: hit.i, sx: p.x, sy: p.y, b: { ...L.boxes[hit.i] }, changed: false };
      drawLabel(); renderBoxList(); return;
    }
    L.selected = -1; L.drag = { mode: 'new', sx: p.x, sy: p.y };
    L.preview = normRect(p.x, p.y, p.x, p.y);
  }
  function move(p) {
    if (!L.img) return;
    if (!L.drag) {
      const hit = hitTest(p.x, p.y);
      c.style.cursor = L.spaceDown ? 'grab' : (hit ? (hit.handle ? 'nwse-resize' : 'move') : 'crosshair');
      return;
    }
    if (L.drag.mode === 'pan') {
      L.panX += p.clientX - L.drag.cx; L.panY += p.clientY - L.drag.cy;
      L.drag.cx = p.clientX; L.drag.cy = p.clientY; applyView(); return;
    }
    if (L.drag.mode === 'new') { L.preview = normRect(L.drag.sx, L.drag.sy, p.x, p.y); drawLabel(); return; }
    const d = L.drag, b = L.boxes[d.i], W = c.width, H = c.height;
    const dx = (p.x - d.sx) / W, dy = (p.y - d.sy) / H;
    if (!d.changed) { pushHistory(); d.changed = true; }
    if (d.mode === 'move') {
      b.x = clamp(d.b.x + dx, 0, 1 - d.b.w); b.y = clamp(d.b.y + dy, 0, 1 - d.b.h);
    } else {
      b.w = clamp(d.b.w + dx, 0.005, 1 - d.b.x); b.h = clamp(d.b.h + dy, 0.005, 1 - d.b.y);
    }
    L.dirty = true; drawLabel();
  }
  function finish() {
    if (!L.drag) return;
    const mode = L.drag.mode;
    if (mode === 'new') {
      const p = L.preview;
      if (p && p.w * c.width > 6 && p.h * c.height > 6) {
        pushHistory();
        L.boxes.push({ cls: L.activeCls, ...p }); L.selected = L.boxes.length - 1;
        L.dirty = true;
      }
      L.preview = null;
    } else if ((mode === 'move' || mode === 'resize') && !L.drag.changed) {
      // A click that did not move anything should not leave a history entry.
    }
    L.drag = null; drawLabel(); renderBoxList(); updateHistoryButtons();
  }

  // --- mouse ---
  c.addEventListener('mousedown', e => {
    if (e.button === 1 || L.spaceDown) { e.preventDefault(); down(canvasPos(e), true); return; }
    if (e.button !== 0) return;
    down(canvasPos(e), false);
  });
  c.addEventListener('mousemove', e => move(canvasPos(e)));
  c.addEventListener('mouseup', finish);
  c.addEventListener('mouseleave', () => { if (L.drag && L.drag.mode !== 'pan') finish(); });
  c.addEventListener('auxclick', e => e.preventDefault());
  c.addEventListener('wheel', e => {
    if (!L.img) return;
    e.preventDefault();
    zoomAt(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX, e.clientY);
  }, { passive: false });

  // --- touch: one finger draws/moves, two fingers pan + pinch-zoom ---
  const touchPoint = t => ({ ...canvasPos({ clientX: t.clientX, clientY: t.clientY }),
                             clientX: t.clientX, clientY: t.clientY });
  const dist = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
  c.addEventListener('touchstart', e => {
    if (!L.img) return;
    e.preventDefault();
    if (e.touches.length === 1) down(touchPoint(e.touches[0]), false);
    else if (e.touches.length === 2) {
      L.drag = null; L.preview = null;
      L.pinch = { d: dist(e.touches[0], e.touches[1]), zoom: L.zoom,
                  cx: (e.touches[0].clientX + e.touches[1].clientX) / 2,
                  cy: (e.touches[0].clientY + e.touches[1].clientY) / 2,
                  panX: L.panX, panY: L.panY };
    }
  }, { passive: false });
  c.addEventListener('touchmove', e => {
    if (!L.img) return;
    e.preventDefault();
    if (e.touches.length === 1 && L.pinch === null) move(touchPoint(e.touches[0]));
    else if (e.touches.length === 2 && L.pinch) {
      const cx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
      const cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
      const wrap = $('canvasWrap').getBoundingClientRect();
      const target = clamp(L.pinch.zoom * dist(e.touches[0], e.touches[1]) / L.pinch.d, 0.25, 8);
      const k = target / L.pinch.zoom;
      const px = cx - wrap.left, py = cy - wrap.top;
      L.zoom = target;
      L.panX = px - (px - (L.pinch.panX + (cx - L.pinch.cx))) * k;
      L.panY = py - (py - (L.pinch.panY + (cy - L.pinch.cy))) * k;
      applyView();
    }
  }, { passive: false });
  const endTouch = e => {
    if (L.pinch !== null) { L.pinch = null; return; }
    finish();
  };
  c.addEventListener('touchend', endTouch);
  c.addEventListener('touchcancel', endTouch);
}
async function initLabel() {
  if (!L.canvas) {
    L.canvas = $('labelCanvas'); L.ctx = L.canvas.getContext('2d'); bindCanvas();
    window.addEventListener('resize', () => { if (S.view === 'label' && L.img) fitCanvas(); });
    $('lbUndo').onclick = undoLabel;
    $('lbRedo').onclick = redoLabel;
    $('lbZoomIn').onclick = () => setZoom(L.zoom * 1.25);
    $('lbZoomOut').onclick = () => setZoom(L.zoom / 1.25);
    $('lbZoomFit').onclick = () => resetView();
    updateHistoryButtons();
  }
  fillDatasetSelectors();
  if (!L.ds && S.datasets.length) $('lbDataset').value = S.datasets[0].name;
  try { await loadLabelData(false); $('lbStatus').textContent = '就绪'; }
  catch (e) { $('lbStatus').textContent = e.message; }
}
$('lbDataset').onchange = () => loadLabelData(false);
$('lbSplit').onchange = () => loadLabelData(false);
$('lbFilter').onchange = () => loadLabelData(true);
$('lbSearch').oninput = renderFileList;
$('lbPrev').onclick = () => openImage(L.idx - 1);
$('lbNext').onclick = () => openImage(L.idx + 1);
$('lbSave').onclick = () => saveLabel(false);
$('lbClear').onclick = () => {
  if (!L.boxes.length) return;
  showModal('清空标注框', '<p>确认清空当前图片的所有标注框？</p>', () => {
    pushHistory();
    L.boxes = []; L.selected = -1; L.dirty = true; drawLabel(); renderBoxList();
  });
};
$('lbAutoSave').onchange = (e) => { L.autosave = e.target.checked; };
$('lbAutoLabel').onclick = () => {
  if (runtimeNeeded()) return;
  if (!L.ds) return toast('请先选择数据集', true);
  const models = modelItems().map(o => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('');
  const cls = L.classes.map((c, i) => `<option value="${i}" ${i === L.activeCls ? 'selected' : ''}>${esc(c)}</option>`).join('');
  showModal('AI 预标注（起草，仍需人工确认）', `
    <p class="hint">用现有权重先跑一遍自动出框，人再修改。若使用 COCO 官方权重，类别与你的自定义类别不匹配，所有框会被设为下面指定的默认类别。</p>
    <label>模型<select id="alModel">${models}</select></label>
    <label>范围<select id="alScope"><option value="unlabeled">仅未标注图片</option><option value="current">仅当前图片</option><option value="all">全部图片（覆盖已有框）</option></select></label>
    <div class="grid-2"><label>置信度<input id="alConf" type="number" value="0.25" step="0.05" min="0.01" max="0.99"></label>
    <label>默认类别<select id="alCls">${cls}</select></label></div>`, async () => {
    const params = {
      type: 'autolabel', label: 'AI 预标注', params: {
        dataset: L.ds, split: L.split, file: L.images[L.idx] ? L.images[L.idx].name : '',
        scope: $('alScope').value, model: $('alModel').value,
        conf: parseFloat($('alConf').value), default_cls: parseInt($('alCls').value, 10),
        merge: false, device: 'auto'
      }
    };
    toast('预标注任务已启动');
    try {
      const t = await api('/api/task/start', params);
      const st = await watchTask(t.id, { onEvent: ev => {
        if (ev.type === 'progress') $('lbStatus').textContent = `预标注 ${ev.done}/${ev.total} · ${ev.file}`;
      } });
      if (st.status === 'done') { toast('预标注完成，请人工检查并修正'); await loadLabelData(true); }
      else toast('预标注失败: ' + (st.error || st.status), true);
    } catch (e) { toast(e.message, true); }
  });
};
document.addEventListener('keyup', e => { if (e.key === ' ') L.spaceDown = false; });
window.addEventListener('blur', () => { L.spaceDown = false; });
document.addEventListener('keydown', e => {
  if (S.view !== 'label' || !$('modal').classList.contains('hidden')) return;
  const tag = document.activeElement ? document.activeElement.tagName : '';
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
  const k = e.key;
  const mod = e.ctrlKey || e.metaKey;
  if (mod && k.toLowerCase() === 's') { e.preventDefault(); saveLabel(false); return; }
  if (mod && k.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? redoLabel() : undoLabel(); return; }
  if (mod && k.toLowerCase() === 'y') { e.preventDefault(); redoLabel(); return; }
  if (k === ' ') { e.preventDefault(); L.spaceDown = true; if (L.canvas) L.canvas.style.cursor = 'grab'; return; }
  if (k === 'Escape') { L.selected = -1; drawLabel(); renderBoxList(); return; }
  if (k === 'Delete' || k === 'Backspace') {
    if (L.selected >= 0) { e.preventDefault(); pushHistory(); L.boxes.splice(L.selected, 1); L.selected = -1; L.dirty = true; drawLabel(); renderBoxList(); }
    return;
  }
  if (/^[1-9]$/.test(k)) {
    const n = parseInt(k, 10) - 1;
    if (n < L.classes.length) {
      L.activeCls = n;
      if (L.selected >= 0) { pushHistory(); L.boxes[L.selected].cls = n; L.dirty = true; drawLabel(); renderBoxList(); }
      renderClassList();
    }
    return;
  }
  const step = e.shiftKey ? 5 : 1;
  if (L.selected >= 0 && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(k)) {
    e.preventDefault();
    if (!e.repeat) pushHistory();
    const b = L.boxes[L.selected], W = L.canvas.width, H = L.canvas.height;
    if (k === 'ArrowLeft') b.x = clamp(b.x - step / W, 0, 1 - b.w);
    if (k === 'ArrowRight') b.x = clamp(b.x + step / W, 0, 1 - b.w);
    if (k === 'ArrowUp') b.y = clamp(b.y - step / H, 0, 1 - b.h);
    if (k === 'ArrowDown') b.y = clamp(b.y + step / H, 0, 1 - b.h);
    L.dirty = true; drawLabel(); return;
  }
  if (k === 'a' || k === 'A' || k === 'ArrowLeft') { e.preventDefault(); openImage(L.idx - 1); }
  if (k === 'd' || k === 'D' || k === 'ArrowRight') { e.preventDefault(); openImage(L.idx + 1); }
});

/* ---------------- task watcher ---------------- */
const TASK_WATCHERS = new Map();
function watchTask(id, opt) {
  opt = opt || {};
  const existing = TASK_WATCHERS.get(id);
  if (existing) return existing.promise;
  let offset = 0, failures = 0, inFlight = false, timer = null, finish = null;
  const started = Date.now();
  const MAX_FAILURES = 10;
  const MAX_DURATION_MS = 24 * 60 * 60 * 1000;
  const promise = new Promise(resolve => {
    finish = (st) => {
      if (timer) clearInterval(timer);
      TASK_WATCHERS.delete(id);
      resolve(st);
    };
    const tick = async () => {
      if (inFlight) return;              // never overlap two polls
      inFlight = true;
      try {
        const log = await api(`/api/task/log?id=${encodeURIComponent(id)}&offset=${offset}`);
        const st = await api(`/api/task/status?id=${encodeURIComponent(id)}`);
        failures = 0;
        offset = log.offset;
        if (opt.onLog && log.text) opt.onLog(log.text);
        (log.events || []).forEach(ev => { if (opt.onEvent) opt.onEvent(ev); });
        if (opt.onStatus) opt.onStatus(st);
        if (st.status !== 'running') return finish(st);
        if (Date.now() - started > MAX_DURATION_MS) {
          return finish({ status: 'error', error: '任务监控超时' });
        }
      } catch (e) {
        // One failed poll is usually a transient hiccup; the worker is still
        // running, so retry instead of declaring the task failed.
        failures++;
        if (failures >= MAX_FAILURES) {
          return finish({ status: 'error', error: '监控中断：' + e.message });
        }
      } finally {
        inFlight = false;
      }
    };
    timer = setInterval(tick, 1000);
    tick();
  });
  TASK_WATCHERS.set(id, {
    promise,
    cancel: () => { if (finish) finish({ status: 'stopped' }); }
  });
  return promise;
}
function cancelAllTaskWatches() {
  Array.from(TASK_WATCHERS.values()).forEach(w => w.cancel());
}
function appendLog(el, text) {
  if (!el) return;
  el.textContent = (el.textContent === '等待训练启动...' || el.textContent === '等待操作...' ? '' : el.textContent) + text;
  el.scrollTop = el.scrollHeight;
}

/* ---------------- train ---------------- */
function initTrain() {
  fillDatasetSelectors(); fillModelSelectors(); fillDeviceSelectors();
  if (!S.train.watching) {
    $('trTaskState').textContent = '空闲';
    $('trTaskState').className = 'badge badge-muted';
    resumeTrainWatch();
  }
}
// The server keeps running tasks in memory; re-attach after an F5 so the UI can
// still show progress and stop the training instead of reporting "idle".
async function resumeTrainWatch() {
  if (S.train.watching) return;
  let tasks;
  try { tasks = await api('/api/tasks'); } catch (e) { return; }
  const running = (tasks || []).filter(t => t.type === 'train' && t.status === 'running');
  if (!running.length) return;
  if (!S.train.epochs) S.train.epochs = parseInt($('trEpochs').value, 10) || 0;
  toast('已恢复对运行中训练任务的监控');
  attachTrainWatch(running[running.length - 1].id);
}
async function attachTrainWatch(id) {
  if (S.train.watching === id) return;
  S.train.watching = id; S.train.taskId = id;
  $('btnStartTrain').disabled = true; $('btnStopTrain').disabled = false;
  $('trTaskState').textContent = '运行中'; $('trTaskState').className = 'badge badge-info';
  const total = parseInt(S.train.epochs, 10) || 0;
  try {
    const st = await watchTask(id, {
      onLog: txt => appendLog($('trLog'), txt),
      onEvent: ev => {
        if (ev.type === 'metric') {
          S.train.metrics.push(ev.row);
          if (total) $('trProgress').style.width = Math.min(100, S.train.metrics.length / total * 100) + '%';
          const last = ev.row;
          $('trMetrics').innerHTML = [
            ['Epoch', last.epoch + ' / ' + (total || '?')],
            ['mAP50', last['metrics/mAP50(B)'] || '-'],
            ['mAP50-95', last['metrics/mAP50-95(B)'] || '-'],
            ['box_loss', last['train/box_loss'] || '-']
          ].map(m => `<div class="metric"><b>${esc(m[1])}</b>${m[0]}</div>`).join('');
          $('trChart').innerHTML = lineChart(chartFromRows(S.train.metrics));
        }
        if (ev.type === 'stage' || ev.type === 'warn') notice($('trNotice'), ev.message || '', ev.type === 'warn');
      }
    });
    $('btnStartTrain').disabled = false; $('btnStopTrain').disabled = true;
    $('trTaskState').textContent = st.status === 'done' ? '已完成' : st.status;
    $('trTaskState').className = 'badge ' + (st.status === 'done' ? 'badge-ok' : 'badge-err');
    if (st.status === 'done') { toast('训练完成'); notice($('trNotice'), '训练完成，权重已保存到 runs 目录'); }
    else notice($('trNotice'), '训练结束：' + (st.error || st.status), true);
    try { await loadCore(); } catch (e) { /* status refresh is best-effort */ }
  } finally {
    S.train.watching = ''; S.train.taskId = '';
  }
}
$('btnRefreshTrain').onclick = async () => { await loadCore(); initTrain(); toast('状态已刷新'); };
$('btnOpenRuns').onclick = () => api('/api/open', { path: S.status.workspace + '/runs' }).catch(e => toast(e.message, true));
$('btnStartTrain').onclick = async () => {
  if (runtimeNeeded()) return;
  if (S.train.watching) return toast('已有训练任务在运行', true);
  const ds = $('trDataset').value;
  if (!ds) return toast('请先创建数据集', true);
  const model = $('trModel').value;
  const params = {
    dataset: ds, model, epochs: parseInt($('trEpochs').value, 10),
    imgsz: parseInt($('trImgsz').value, 10), batch: parseInt($('trBatch').value, 10),
    workers: parseInt($('trWorkers').value, 10), patience: parseInt($('trPatience').value, 10),
    name: $('trName').value.trim() || 'train', device: $('trDevice').value,
    optimizer: $('trOptimizer').value, lr0: $('trLr0').value ? parseFloat($('trLr0').value) : null,
    resume: $('trResume').checked, amp: $('trAmp').checked, cache: $('trCache').checked
  };
  S.train.epochs = params.epochs;
  $('trLog').textContent = ''; S.train.metrics = []; $('trChart').innerHTML = '';
  notice($('trNotice'), '正在启动训练任务...');
  try {
    const t = await api('/api/task/start', { type: 'train', params, label: '训练 ' + ds });
    toast('训练已启动');
    await attachTrainWatch(t.id);
  } catch (e) {
    $('btnStartTrain').disabled = false; $('btnStopTrain').disabled = true;
    notice($('trNotice'), '启动失败：' + e.message, true); toast(e.message, true);
  }
};
$('btnStopTrain').onclick = async () => {
  if (!S.train.taskId) return;
  try { await api('/api/task/stop', { id: S.train.taskId }); toast('已发送停止信号'); }
  catch (e) { toast(e.message, true); }
};

/* ---------------- predict ---------------- */
async function initPredict() {
  fillDatasetSelectors(); fillModelSelectors(); fillDeviceSelectors();
  await fillPredictImages();
}
async function fillPredictImages() {
  const ds = $('prDataset').value, split = $('prSplit').value;
  if (!ds) return;
  try {
    const r = await api(`/api/dataset/images?name=${encodeURIComponent(ds)}&split=${split}`);
    fillSelect($('prImage'), r.images.map(x => ({ value: x.name, label: x.name })), $('prImage').value);
  } catch (e) { /* ignore */ }
}
$('prDataset').onchange = fillPredictImages;
$('prSplit').onchange = fillPredictImages;
function drawPredict() {
  const c = $('predictCanvas'), ctx = c.getContext('2d');
  const img = S.predict.img;
  if (!img) { ctx.clearRect(0, 0, c.width, c.height); return; }
  const wrap = c.parentElement;
  const scale = Math.min((wrap.clientWidth - 24) / img.width, 460 / img.height, 2);
  c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
  ctx.drawImage(img, 0, 0, c.width, c.height);
  const names = {};
  S.predict.boxes.forEach(b => { if (b.name != null) names[b.cls] = b.name; });
  drawBoxes(ctx, S.predict.boxes.map(b => ({
    cls: b.cls, x: b.x1 / img.width, y: b.y1 / img.height,
    w: (b.x2 - b.x1) / img.width, h: (b.y2 - b.y1) / img.height
  })), names, -1);
  $('prCount').textContent = S.predict.boxes.length;
  $('prBoxList').innerHTML = S.predict.boxes.length ? S.predict.boxes.map((b, i) =>
    `<div class="box-item"><span class="dot" style="background:${COLORS[b.cls % COLORS.length]}"></span>
      #${i + 1} ${esc(b.name || b.cls)} · ${b.conf == null ? '-' : (b.conf * 100).toFixed(1) + '%'}
      <span style="margin-left:auto;color:#94a3b8">${Math.round(b.x1)},${Math.round(b.y1)} → ${Math.round(b.x2)},${Math.round(b.y2)}</span></div>`).join('')
    : '<div class="hint small">没有检测到目标</div>';
}
async function loadPredictImage() {
  const ds = $('prDataset').value, split = $('prSplit').value, file = $('prImage').value;
  if (!file) return;
  const img = new Image();
  img.onload = () => { S.predict.img = img; drawPredict(); };
  img.src = `/api/image?name=${encodeURIComponent(ds)}&split=${split}&file=${encodeURIComponent(file)}&t=${Date.now()}`;
}
$('prImage').onchange = loadPredictImage;
function commonParams(extra) {
  return Object.assign({
    model: $('prModel').value, device: $('prDevice').value,
    dataset: $('prDataset').value, split: $('prSplit').value,
    file: $('prImage').value, conf: parseFloat($('prConf').value),
    imgsz: 640
  }, extra || {});
}
function setPredictBusy(busy) {
  ['btnPredict', 'btnValidate', 'btnExport'].forEach(id => {
    const b = $(id); if (b) b.disabled = busy;
  });
}
async function runSimple(type, params, label, onDone) {
  if (S.predict.busy) {
    notice($('prNotice'), '已有任务在运行，请等待完成', true);
    return;
  }
  S.predict.busy = true; setPredictBusy(true);
  $('prLog').textContent = '';
  notice($('prNotice'), label + ' 正在运行...');
  try {
    const t = await api('/api/task/start', { type, params, label });
    S.predict.taskId = t.id;
    const st = await watchTask(t.id, { onLog: txt => appendLog($('prLog'), txt) });
    if (st.status === 'done') {
      notice($('prNotice'), label + ' 完成'); await onDone(st.result || {});
    } else notice($('prNotice'), label + ' 失败：' + (st.error || st.status), true);
  } catch (e) { notice($('prNotice'), e.message, true); toast(e.message, true); }
  finally { S.predict.busy = false; S.predict.taskId = ''; setPredictBusy(false); }
}
$('btnPredict').onclick = async () => {
  await loadPredictImage();
  await runSimple('predict', commonParams(), '推理', async (res) => {
    S.predict.boxes = res.boxes || []; drawPredict();
  });
};
$('btnValidate').onclick = async () => {
  const p = commonParams(); delete p.file;
  await runSimple('validate', p, '验证', async (res) => {
    $('prLog').textContent += '\n\nmAP50    : ' + res.map50 + '\nmAP50-95 : ' + res.map +
      '\nPrecision: ' + res.precision + '\nRecall   : ' + res.recall;
  });
};
$('btnExport').onclick = () => {
  const p = commonParams({ format: 'onnx' }); delete p.file;
  showModal('导出模型', `<label>格式<select id="exFormat">
      <option value="onnx">ONNX（通用）</option>
      <option value="openvino">OpenVINO（Intel GPU / CPU 推荐）</option>
      <option value="torchscript">TorchScript</option>
      <option value="tflite">TFLite</option></select></label>
    <p class="hint small">部分格式需要额外依赖；失败时日志会给出安装命令。</p>`, async () => {
    p.format = $('exFormat').value;
    await runSimple('export', p, '导出', async (res) => {
      $('prLog').textContent += '\n\n导出文件: ' + (res.path || '-');
    });
  });
};

/* ---------------- runs ---------------- */
async function renderRuns() {
  try {
    S.runs = await api('/api/runs');
    $('runsList').innerHTML = S.runs.length ? S.runs.map(r =>
      `<div class="ds-item ${r.dir === S.activeRun ? 'active' : ''}" data-run="${esc(r.dir)}">
        <b>${esc(r.name)}</b><div class="meta">
        <span>${r.epochs} epochs · mAP50 ${esc(r.best_map || '-')}</span>
        <span>${new Date(r.mtime * 1000).toLocaleDateString('zh-CN')}</span></div></div>`).join('')
      : '';
    $('runsList').querySelectorAll('[data-run]').forEach(el => el.onclick = () => {
      S.activeRun = el.dataset.run; showRunDetail();
    });
    if (!S.activeRun && S.runs.length) S.activeRun = S.runs[0].dir;
    if (S.activeRun) showRunDetail();
  } catch (e) { toast(e.message, true); }
}
async function showRunDetail() {
  try {
    const r = await api('/api/run/detail?path=' + encodeURIComponent(S.activeRun));
    const chart = lineChart(chartFromRows(r.rows));
    const last = r.rows[r.rows.length - 1] || {};
    $('runDetail').innerHTML = `
      <div class="kv-list">
        <div class="k">目录</div><div class="v">${esc(r.dir)}</div>
        <div class="k">轮数</div><div class="v">${r.rows.length}</div>
        <div class="k">最终 mAP50</div><div class="v">${esc(last['metrics/mAP50(B)'] || '-')}</div>
        <div class="k">最终 mAP50-95</div><div class="v">${esc(last['metrics/mAP50-95(B)'] || '-')}</div>
      </div>
      <div class="divider"></div><h4>指标曲线</h4><div class="chart-box">${chart}</div>
      <h4>权重</h4>${(r.weights || []).map(w =>
        `<div class="model-item"><span class="m-name">${esc(w.split(/[\\/]/).pop())}</span>
         <span class="hint small">${esc(w)}</span></div>`).join('') || '<div class="hint">无权重文件</div>'}
      <div class="btn-row"><button class="btn btn-sm" id="btnRunOpen">打开目录</button></div>
      <div class="divider"></div><h4>训练参数 args.yaml</h4>
      <pre class="logbox small">${esc(r.args || '')}</pre>`;
    $('btnRunOpen').onclick = () => api('/api/open', { path: r.dir }).catch(e => toast(e.message, true));
  } catch (e) { $('runDetail').innerHTML = '<div class="hint">' + esc(e.message) + '</div>'; }
}
$('btnRefreshRuns').onclick = renderRuns;

/* ---------------- environment ---------------- */
function renderRuntime() {
  const ready = S.status && S.status.runtime_ready;
  const badge = $('runtimeBadge'), desc = $('runtimeDesc'), btn = $('btnSetupEnv');
  if (!badge || !btn) return;
  if (ready) {
    badge.textContent = '已就绪'; badge.className = 'badge badge-ok';
    desc.textContent = '训练、预标注、推理等功能均可使用。';
    btn.textContent = '重新安装 / 更新';
    btn.className = 'btn';
  } else {
    badge.textContent = '未安装'; badge.className = 'badge badge-warn';
    desc.textContent = '软件可直接浏览；开始训练前需安装运行环境（自动匹配显卡，后台下载）。';
    btn.textContent = '安装运行环境';
    btn.className = 'btn btn-primary';
  }
}

async function setupEnv() {
  const btn = $('btnSetupEnv'), box = $('setupLog');
  btn.disabled = true;
  btn.textContent = '安装中…';
  box.style.display = ''; box.textContent = '';
  try {
    const t = await api('/api/task/start', {
      type: 'setup_env',
      params: { force: true, with_model: true },
      label: '安装运行环境'
    });
    toast('运行环境安装已启动，可切换到其他页面');
    const st = await watchTask(t.id, { onLog: txt => appendLog(box, txt) });
    if (st.status === 'done') {
      toast('运行环境安装完成，所有功能已可用');
    } else {
      toast('安装失败：' + (st.error || '请查看日志'), true);
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    await loadCore();
    renderRuntime();
    renderEnvRest();
  }
}
$('btnSetupEnv').onclick = setupEnv;

function runtimeNeeded() {
  if (S.status && S.status.runtime_ready === false) {
    toast('请先到「环境与显卡」页安装运行环境', true);
    return true;
  }
  return false;
}

async function renderEnv() {
  renderRuntime();
  $('hwDetail').innerHTML = '检测中（约 5-10 秒）...';
  try {
    S.hw = await api('/api/hardware?refresh=1'); updateBadges(); fillDeviceSelectors();
    const h = S.hw, gpus = h.gpus || [];
    const rows = [
      ['显卡', gpus.length ? gpus.map(g => `${g.name}（${g.vendor.toUpperCase()}，${(g.vram_mb / 1024).toFixed(1)} GB，驱动 ${g.driver}）`).join('；') : '未检测到'],
      ['显存来源', (gpus[0] || {}).vram_source && (gpus[0] || {}).vram_source.indexOf('registry') >= 0
        ? '注册表 64 位数值（精确）' : ((gpus[0] || {}).vram_source || '-')],
      ['当前后端', h.active.backend + ' / ' + h.active.device],
      ['推荐后端', h.recommended.backend + ' / ' + h.recommended.device],
      ['PyTorch', (h.torch || {}).version || '-'],
      ['torchvision', (h.torch || {}).torchvision || '-'],
      ['Ultralytics', (h.torch || {}).ultralytics || '-'],
      ['xpu / cuda', `${(h.torch || {}).xpu ? '可用' : '不可用'} / ${(h.torch || {}).cuda ? '可用' : '不可用'}`],
      ['ROCm(HIP)', (h.torch || {}).hip || '未使用'],
      ['需升级框架', h.need_upgrade ? '是（需要 >= ' + (h.recommended.min_ultralytics || '-') + '）' : '否'],
    ];
    $('hwDetail').innerHTML = rows.map(r =>
      `<div class="k">${esc(r[0])}</div><div class="v">${esc(r[1])}</div>`).join('');
    $('hwReadyBadge').textContent = h.ready ? '加速已就绪' : '未启用加速';
    $('hwReadyBadge').className = 'badge ' + (h.ready ? 'badge-ok' : 'badge-warn');
    const backends = [
      { value: 'cpu', label: 'CPU（恢复/回退）' },
      { value: 'cuda', label: 'NVIDIA CUDA' },
      { value: 'xpu', label: 'Intel XPU' },
      { value: 'rocm', label: 'AMD ROCm（Windows 需 HIP SDK）' },
      { value: 'mps', label: 'Apple MPS' },
      { value: 'dml', label: 'DirectML（实验性）' },
    ];
    fillSelect($('hwBackend'), backends, h.recommended.backend);
    $('hwWarn').className = 'warn' + (h.ready ? '' : ' show');
    $('hwWarn').textContent = h.ready ? '' :
      '运行环境尚未安装。请在上方「运行环境」卡片点击安装，按显卡自动匹配；也可在下方手动切换后端（需下载数百 MB），失败会自动回退 CPU。';
  } catch (e) { $('hwDetail').innerHTML = '<div class="hint">' + esc(e.message) + '</div>'; }
  await renderEnvRest();
}
function renderModelList(models) {
  const localNames = new Set((models.local || []).map(x => x.name));
  const online = S.onlineModels || [];
  const rows = [];
  // Official online weights (live catalogue).
  for (const m of online) {
    const has = localNames.has(m.name);
    const action = has
      ? '<span class="badge badge-ok">已安装</span>'
      : `<button class="btn btn-sm btn-primary" data-dl="${esc(m.name)}" data-url="${esc(m.url)}">下载</button>`;
    rows.push(`<div class="model-item"><div class="m-name">${esc(m.name)}</div>${action}</div>`);
  }
  // Built-in presets not already in the online list (e.g. from-scratch yaml).
  const seen = new Set(online.map(m => m.name));
  for (const m of (models.presets || [])) {
    if (seen.has(m.name)) continue;
    const has = localNames.has(m.name);
    const action = m.offline ? ''
      : has ? '<span class="badge badge-ok">已安装</span>'
      : `<button class="btn btn-sm" data-dl="${esc(m.name)}">下载</button>`;
    rows.push(`<div class="model-item"><div class="m-name">${esc(m.name)}</div>${action}</div>`);
  }
  const box = $('modelList');
  if (!box) return;
  box.innerHTML = rows.join('');
  box.querySelectorAll('[data-dl]').forEach(b => b.onclick = async () => {
    const name = b.dataset.dl, url = b.dataset.url || '';
    toast('开始下载 ' + name);
    try {
      const params = { model: name };
      if (url) params.url = url;
      const t = await api('/api/task/start', { type: 'download_model', params, label: '下载 ' + name });
      const st = await watchTask(t.id, { onLog: txt => appendLog($('hwLog'), txt) });
      toast(st.status === 'done' ? '下载完成' : '下载失败: ' + (st.error || ''), st.status !== 'done');
      await loadCore(); renderEnvRest();
    } catch (e) { toast(e.message, true); }
  });
}

async function loadOnlineModels(force) {
  const badge = $('onlineModelState');
  try {
    const r = await api('/api/models/online' + (force ? '?refresh=1' : ''));
    S.onlineModels = r.models || [];
    if (badge) {
      badge.textContent = r.online ? `官方最新 · ${S.onlineModels.length} 个` : '在线不可用 · 使用内置列表';
      badge.className = 'badge ' + (r.online ? 'badge-ok' : 'badge-muted');
    }
    fillModelSelectors();
    if ($('modelList')) renderModelList(await api('/api/models'));
  } catch (e) {
    if (badge) { badge.textContent = '在线不可用'; badge.className = 'badge badge-muted'; }
  }
}

async function renderEnvRest() {
  try {
    const cfg = await api('/api/config'); S.config = cfg;
    $('cfgMirror').value = cfg.mirror || 'tsinghua';
    $('cfgAppMode').checked = !!cfg.app_mode;
    fillModelSelectors();
    const models = await api('/api/models');
    renderModelList(models);
    const st = await api('/api/status');
    $('envRuntime').innerHTML = [
      ['安装目录', st.root], ['工作区', st.workspace], ['Python', st.config.venv_python || '-'],
      ['上次配置', st.config.last_setup || '-'], ['服务端口', st.config.port],
    ].map(r => `<div class="k">${esc(r[0])}</div><div class="v">${esc(r[1])}</div>`).join('');
    const tasks = await api('/api/tasks');
    $('taskList').innerHTML = tasks.length ? tasks.slice(-8).reverse().map(t =>
      `<div class="task-item"><span>${esc(t.label || t.type)}</span>
       <span class="badge ${t.status === 'done' ? 'badge-ok' : t.status === 'running' ? 'badge-info' : 'badge-err'}">${esc(t.status)}</span></div>`).join('')
      : '';
  } catch (e) { /* non critical */ }
}
$('btnRefreshHw').onclick = renderEnv;
$('btnOpenModels').onclick = () => api('/api/open', { path: S.models.models_dir }).catch(e => toast(e.message, true));
$('btnImportModel').onclick = () => showModal('导入本地权重', `
  <p class="hint">把已有的 .pt / .onnx 权重复制到软件的模型目录，之后即可在训练和推理里选择。</p>
  <label>文件完整路径<input id="impPath" placeholder="C:\\models\\best.pt"></label>`, async () => {
  const p = $('impPath').value.trim().replace(/^"|"$/g, '');
  if (!p) { toast('请填写文件路径', true); return false; }
  try {
    const r = await api('/api/model/import', { path: p });
    toast('已导入 ' + r.name); await loadCore(); renderEnvRest();
  } catch (e) { toast(e.message, true); }
});
$('btnRefreshModels').onclick = () => loadOnlineModels(true);
$('btnSaveCfg').onclick = async () => {
  try {
    await api('/api/config/save', {
      mirror: $('cfgMirror').value, default_model: $('cfgModel').value,
      app_mode: $('cfgAppMode').checked
    });
    toast('设置已保存'); await loadCore();
  } catch (e) { toast(e.message, true); }
};
function switchBackend(backend) {
  const warn = backend === 'cpu' ? '将重新安装 CPU 版 PyTorch。' :
    '将替换当前 PyTorch 为 ' + backend.toUpperCase() + ' 版本，并升级 ultralytics。需要下载依赖，可能耗时数分钟。';
  showModal('切换加速后端', `<p class="hint">${esc(warn)} 失败时会自动回退到 CPU，不影响已有数据。</p>`, async () => {
    $('hwLog').textContent = '';
    try {
      const t = await api('/api/task/start', { type: 'backend', label: '切换后端 ' + backend,
        params: { backend } });
      toast('后端切换任务已启动，请稍候');
      const st = await watchTask(t.id, { onLog: txt => appendLog($('hwLog'), txt) });
      toast(st.status === 'done' ? '后端已切换' : '切换未完成，请查看日志', st.status !== 'done');
      setTimeout(renderEnv, 800);
    } catch (e) { toast(e.message, true); }
  });
}
$('btnSwitchBackend').onclick = () => switchBackend($('hwBackend').value);
$('btnBackToCpu').onclick = () => switchBackend('cpu');

/* ---------------- boot ---------------- */
(async function boot() {
  // go('home') used to fire an un-awaited renderHome() before loadCore() filled
  // S.datasets, causing duplicate /api/status calls and a zeroed-stats flash.
  // The home view is already the active one in the markup.
  S.view = 'home';
  await loadCore();
  await renderHome();
  loadOnlineModels(false);   // populated async; never blocks first paint
  resumeTrainWatch();
  const deepView = new URLSearchParams(location.search).get('view');
  if (deepView && ['datasets','label','train','predict','runs','env','help'].includes(deepView)) go(deepView);
  statusTimer = setInterval(async () => {
    if (document.hidden) return;
    try {
      const st = await api('/api/status'); S.status = st; updateBadges();
    } catch (e) { /* service may be shutting down */ }
  }, 5000);
})();
