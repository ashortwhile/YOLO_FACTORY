'use strict';
/* YOLO Studio front-end. No external libraries, works fully offline. */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtSize = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : (n / 1024).toFixed(0) + ' KB';
const COLORS = ['#e74c3c', '#2563eb', '#16a34a', '#d97706', '#8b5cf6', '#0d9488',
                '#db2777', '#0891b2', '#65a30d', '#dc2626'];

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
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
}
function closeModal(ok) {
  const fn = modalResolve; modalResolve = null; $('modal').classList.add('hidden');
  if (ok && fn) return fn();
}
$('modalOk').onclick = () => { if (closeModal(true) === false) return; };
$('modalCancel').onclick = () => closeModal(false);
$('modal').onclick = (e) => { if (e.target.id === 'modal') closeModal(false); };

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
  train: { taskId: '', metrics: [], log: '', offset: 0 },
  predict: { boxes: [], img: null },
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
$('btnQuit').onclick = () => showModal('退出软件',
  '<p>将关闭本地服务。标注内容已自动保存，确认退出？</p>', () => {
    api('/api/shutdown', {}).catch(() => {});
    setTimeout(() => window.close(), 600);
  });

/* ---------------- SVG line chart ---------------- */
function lineChart(series, height) {
  const W = 620, H = height || 200, P = { l: 42, r: 12, t: 18, b: 24 };
  const all = series.flatMap(s => s.values).filter(v => isFinite(v));
  if (!all.length) return '<span class="hint">暂无数据</span>';
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
  const local = (S.models.local || []).map(m => ({ value: m.path, label: m.name + ' · 本地' }));
  const names = new Set((S.models.local || []).map(m => m.name));
  const presets = (S.models.presets || []).map(m => ({
    value: m.name,
    label: m.name + (m.offline ? ' · 无需下载' : (names.has(m.name) ? '' : ' · 需下载')) + ' — ' + m.desc
  }));
  return [...local.filter(x => !x.label.includes('weights/')), ...presets];
}
function fillModelSelectors() {
  const def = S.config.default_model || 'yolo26n.pt';
  ['trModel', 'prModel', 'cfgModel'].forEach(id => {
    const el = $(id); if (!el) return;
    const keep = el.value || (id === 'prModel' ? (S.models.local[0] || {}).path : def);
    fillSelect(el, modelItems(), keep);
  });
  const hint = $('trModelHint');
  if (hint) hint.textContent = '默认 YOLO26（2026 最新正式版）；首次使用会自动下载权重，也可在「环境」页管理。';
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
  const h = $('trDeviceHint');
  if (h && S.hw) {
    const adv = S.hw.training_advice || {};
    h.textContent = `当前后端 ${S.hw.active.backend}；推荐 ${S.hw.recommended.backend} (${S.hw.recommended.device})。` +
      (adv.note ? ' ' + adv.note : '');
  }
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
      : '<div class="hint">还没有训练记录。完成标注后到「训练」页开始。</div>';
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
      : '<div class="hint">还没有数据集。右侧可以新建或挂载已有目录。</div>';
    $('dsList').querySelectorAll('[data-ds]').forEach(el => el.onclick = () => {
      S.activeDs = el.dataset.ds; renderDatasets();
    });
    if (!S.activeDs && S.datasets.length) S.activeDs = S.datasets[0].name;
    const d = S.datasets.find(x => x.name === S.activeDs);
    if (!d) { $('dsDetail').innerHTML = '<div class="hint">从左侧选择一个数据集</div>'; return; }
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
      <label>图片文件夹路径（粘贴的带引号路径也可以）<input id="dsImportSrc" placeholder="例如 D:\\photos\\raw"></label>
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
    $('btnSplit').onclick = async () => {
      const ratio = parseFloat($('dsSplitRatio').value);
      try {
        const r = await api('/api/dataset/split', { name: d.name, ratio });
        toast(`划分完成，移动了 ${r.moved} / ${r.total} 张`); renderDatasets();
      } catch (e) { toast(e.message, true); }
    };
  } catch (e) { toast(e.message, true); }
}
$('btnRefreshDs').onclick = renderDatasets;
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
  drag: null, preview: null, canvas: null, ctx: null
};
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
  drawLabel();
}
function renderClassList() {
  $('lbClassList').innerHTML = L.classes.map((c, i) =>
    `<div class="class-item ${i === L.activeCls ? 'active' : ''}" data-cls="${i}">
      <span class="sw" style="background:${COLORS[i % COLORS.length]}"></span>
      <span>${esc(c)}</span><span class="idx">${i}</span></div>`).join('');
  $('lbClassList').querySelectorAll('[data-cls]').forEach(el => el.onclick = () => {
    L.activeCls = parseInt(el.dataset.cls, 10);
    if (L.selected >= 0) { L.boxes[L.selected].cls = L.activeCls; L.dirty = true; drawLabel(); renderBoxList(); }
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
    : '<div class="hint small">暂无标注框，在图上拖拽画框</div>';
  $('lbBoxList').querySelectorAll('[data-box]').forEach(el => el.onclick = (e) => {
    if (e.target.tagName === 'SELECT') return;
    L.selected = parseInt(el.dataset.box, 10); drawLabel(); renderBoxList();
  });
  $('lbBoxList').querySelectorAll('[data-boxsel]').forEach(el => el.onchange = () => {
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
  if (L.idx < 0 || !L.images[L.idx]) return;
  try {
    await api('/api/label/save', {
      name: L.ds, split: L.split, file: L.images[L.idx].name, boxes: L.boxes
    });
    L.images[L.idx].labeled = true; L.images[L.idx].boxes = L.boxes.length;
    L.dirty = false; renderFileList(); renderBoxList();
    $('lbStatus').textContent = '已保存 ' + new Date().toLocaleTimeString('zh-CN');
    if (!silent) toast('标注已保存');
  } catch (e) { toast(e.message, true); }
}
async function loadLabelData(keepImage) {
  const ds = $('lbDataset').value; if (!ds) return;
  L.ds = ds; L.split = $('lbSplit').value; L.filter = $('lbFilter').value;
  const keepName = keepImage && L.idx >= 0 && L.images[L.idx] ? L.images[L.idx].name : null;
  const info = await api(`/api/dataset/images?name=${encodeURIComponent(ds)}&split=${L.split}&filter=${L.filter}`);
  L.images = info.images; L.classes = info.classes;
  if (!L.classes.length) L.classes = ['object'];
  if (L.activeCls >= L.classes.length) L.activeCls = 0;
  renderClassList(); renderFileList();
  let idx = 0;
  if (keepName) { const k = L.images.findIndex(x => x.name === keepName); if (k >= 0) idx = k; }
  await openImage(idx, true);
}
async function openImage(i, skipSave) {
  if (!skipSave && L.dirty && L.autosave) await saveLabel(true);
  if (!L.images.length) { L.idx = -1; return; }
  L.idx = clamp(i, 0, L.images.length - 1);
  const item = L.images[L.idx];
  $('lbCounter').textContent = `${L.idx + 1} / ${L.images.length}`;
  $('lbImgInfo').textContent = `${item.name} · ${fmtSize(item.size)}`;
  const img = new Image();
  img.onload = async () => {
    L.img = img; L.selected = -1; L.preview = null;
    fitCanvas();
    try {
      const lab = await api(`/api/label?name=${encodeURIComponent(L.ds)}&split=${L.split}&file=${encodeURIComponent(item.name)}`);
      L.boxes = lab.boxes || [];
    } catch (e) { L.boxes = []; }
    L.dirty = false; drawLabel(); renderBoxList(); renderFileList();
  };
  img.onerror = () => toast('图片加载失败', true);
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
  c.addEventListener('mousedown', e => {
    if (!L.img) return;
    const p = canvasPos(e), hit = hitTest(p.x, p.y);
    if (hit) {
      L.selected = hit.i;
      L.drag = hit.handle
        ? { mode: 'resize', i: hit.i, sx: p.x, sy: p.y, b: { ...L.boxes[hit.i] } }
        : { mode: 'move', i: hit.i, sx: p.x, sy: p.y, b: { ...L.boxes[hit.i] } };
      drawLabel(); renderBoxList(); return;
    }
    L.selected = -1; L.drag = { mode: 'new', sx: p.x, sy: p.y };
    L.preview = normRect(p.x, p.y, p.x, p.y);
  });
  c.addEventListener('mousemove', e => {
    if (!L.img) return;
    const p = canvasPos(e);
    if (!L.drag) {
      const hit = hitTest(p.x, p.y);
      c.style.cursor = hit ? (hit.handle ? 'nwse-resize' : 'move') : 'crosshair';
      return;
    }
    if (L.drag.mode === 'new') { L.preview = normRect(L.drag.sx, L.drag.sy, p.x, p.y); drawLabel(); return; }
    const d = L.drag, b = L.boxes[d.i], W = c.width, H = c.height;
    const dx = (p.x - d.sx) / W, dy = (p.y - d.sy) / H;
    if (d.mode === 'move') {
      b.x = clamp(d.b.x + dx, 0, 1 - d.b.w); b.y = clamp(d.b.y + dy, 0, 1 - d.b.h);
    } else {
      b.w = clamp(d.b.w + dx, 0.005, 1 - d.b.x); b.h = clamp(d.b.h + dy, 0.005, 1 - d.b.y);
    }
    L.dirty = true; drawLabel();
  });
  const finish = () => {
    if (!L.drag) return;
    if (L.drag.mode === 'new') {
      const p = L.preview;
      if (p && p.w * c.width > 6 && p.h * c.height > 6) {
        L.boxes.push({ cls: L.activeCls, ...p }); L.selected = L.boxes.length - 1;
        L.dirty = true;
      }
      L.preview = null;
    }
    L.drag = null; drawLabel(); renderBoxList();
  };
  c.addEventListener('mouseup', finish);
  c.addEventListener('mouseleave', () => { if (L.drag) finish(); });
}
async function initLabel() {
  if (!L.canvas) {
    L.canvas = $('labelCanvas'); L.ctx = L.canvas.getContext('2d'); bindCanvas();
    window.addEventListener('resize', () => { if (S.view === 'label' && L.img) fitCanvas(); });
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
    L.boxes = []; L.selected = -1; L.dirty = true; drawLabel(); renderBoxList();
  });
};
$('lbAutoSave').onchange = (e) => { L.autosave = e.target.checked; };
$('lbAutoLabel').onclick = () => {
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
document.addEventListener('keydown', e => {
  if (S.view !== 'label' || !$('modal').classList.contains('hidden')) return;
  const tag = document.activeElement ? document.activeElement.tagName : '';
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return;
  const k = e.key;
  if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === 's') { e.preventDefault(); saveLabel(false); return; }
  if (k === 'Escape') { L.selected = -1; drawLabel(); renderBoxList(); return; }
  if (k === 'Delete' || k === 'Backspace') {
    if (L.selected >= 0) { e.preventDefault(); L.boxes.splice(L.selected, 1); L.selected = -1; L.dirty = true; drawLabel(); renderBoxList(); }
    return;
  }
  if (/^[1-9]$/.test(k)) {
    const n = parseInt(k, 10) - 1;
    if (n < L.classes.length) {
      L.activeCls = n;
      if (L.selected >= 0) { L.boxes[L.selected].cls = n; L.dirty = true; drawLabel(); renderBoxList(); }
      renderClassList();
    }
    return;
  }
  const step = e.shiftKey ? 5 : 1;
  if (L.selected >= 0 && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(k)) {
    e.preventDefault(); const b = L.boxes[L.selected], W = L.canvas.width, H = L.canvas.height;
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
function watchTask(id, opt) {
  opt = opt || {};
  return new Promise(resolve => {
    let offset = 0;
    const timer = setInterval(async () => {
      try {
        const [log, st] = await Promise.all([
          api(`/api/task/log?id=${encodeURIComponent(id)}&offset=${offset}`),
          api(`/api/task/status?id=${encodeURIComponent(id)}`)
        ]);
        offset = log.offset;
        if (opt.onLog && log.text) opt.onLog(log.text);
        (log.events || []).forEach(ev => { if (opt.onEvent) opt.onEvent(ev); });
        if (opt.onStatus) opt.onStatus(st);
        if (st.status !== 'running') {
          clearInterval(timer); resolve(st);
        }
      } catch (e) { clearInterval(timer); resolve({ status: 'error', error: e.message }); }
    }, 1000);
  });
}
function appendLog(el, text) {
  if (!el) return;
  el.textContent = (el.textContent === '等待训练启动...' || el.textContent === '等待操作...' ? '' : el.textContent) + text;
  el.scrollTop = el.scrollHeight;
}

/* ---------------- train ---------------- */
function initTrain() {
  fillDatasetSelectors(); fillModelSelectors(); fillDeviceSelectors();
  if (!S.train.taskId) { $('trTaskState').textContent = '空闲'; $('trTaskState').className = 'badge badge-muted'; }
}
$('btnRefreshTrain').onclick = async () => { await loadCore(); initTrain(); toast('状态已刷新'); };
$('btnOpenRuns').onclick = () => api('/api/open', { path: S.status.workspace + '/runs' }).catch(e => toast(e.message, true));
$('btnStartTrain').onclick = async () => {
  const ds = $('trDataset').value;
  if (!ds) return toast('请先创建数据集', true);
  const model = $('trCustomModel').value.trim() || $('trModel').value;
  const params = {
    dataset: ds, model, epochs: parseInt($('trEpochs').value, 10),
    imgsz: parseInt($('trImgsz').value, 10), batch: parseInt($('trBatch').value, 10),
    workers: parseInt($('trWorkers').value, 10), patience: parseInt($('trPatience').value, 10),
    name: $('trName').value.trim() || 'train', device: $('trDevice').value,
    optimizer: $('trOptimizer').value, lr0: $('trLr0').value ? parseFloat($('trLr0').value) : null,
    resume: $('trResume').checked, amp: $('trAmp').checked, cache: $('trCache').checked
  };
  $('trLog').textContent = ''; S.train.metrics = []; $('trChart').innerHTML = '<span class="hint">等待指标...</span>';
  notice($('trNotice'), '正在启动训练任务...');
  try {
    const t = await api('/api/task/start', { type: 'train', params, label: '训练 ' + ds });
    S.train.taskId = t.id; $('btnStartTrain').disabled = true; $('btnStopTrain').disabled = false;
    $('trTaskState').textContent = '运行中'; $('trTaskState').className = 'badge badge-info';
    toast('训练已启动');
    const st = await watchTask(t.id, {
      onLog: txt => appendLog($('trLog'), txt),
      onEvent: ev => {
        if (ev.type === 'metric') {
          S.train.metrics.push(ev.row);
          const total = parseInt(params.epochs, 10);
          $('trProgress').style.width = Math.min(100, S.train.metrics.length / total * 100) + '%';
          const last = ev.row;
          $('trMetrics').innerHTML = [
            ['Epoch', last.epoch + ' / ' + params.epochs],
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
    await loadCore();
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
  drawBoxes(ctx, S.predict.boxes.map(b => ({
    cls: b.cls, x: b.x1 / img.width, y: b.y1 / img.height,
    w: (b.x2 - b.x1) / img.width, h: (b.y2 - b.y1) / img.height
  })), null, -1);
  $('prCount').textContent = S.predict.boxes.length;
  $('prBoxList').innerHTML = S.predict.boxes.length ? S.predict.boxes.map((b, i) =>
    `<div class="box-item"><span class="dot" style="background:${COLORS[b.cls % COLORS.length]}"></span>
      #${i + 1} ${esc(b.name || b.cls)} · ${(b.conf * 100).toFixed(1)}%
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
async function runSimple(type, params, label, onDone) {
  $('prLog').textContent = '';
  notice($('prNotice'), label + ' 正在运行...');
  try {
    const t = await api('/api/task/start', { type, params, label });
    const st = await watchTask(t.id, { onLog: txt => appendLog($('prLog'), txt) });
    if (st.status === 'done') {
      notice($('prNotice'), label + ' 完成'); await onDone(st.result || {});
    } else notice($('prNotice'), label + ' 失败：' + (st.error || st.status), true);
  } catch (e) { notice($('prNotice'), e.message, true); toast(e.message, true); }
}
$('btnPredict').onclick = async () => {
  await loadPredictImage();
  await runSimple('predict', commonParams(), '推理', async (res) => {
    S.predict.boxes = res.boxes || []; drawPredict();
  });
};
$('btnValidate').onclick = async () => {
  const p = commonParams(); delete p.file;
  p.type = 'validate';
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
      : '<div class="hint">还没有训练记录</div>';
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
async function renderEnv() {
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
      ['训练建议', (h.training_advice || {}).note || '-'],
    ];
    $('hwDetail').innerHTML = rows.map(r =>
      `<div class="k">${esc(r[0])}</div><div class="v">${esc(r[1])}</div>`).join('');
    $('hwNote').textContent = h.note || '';
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
      '首次启动会自动按显卡安装匹配的最新版 PyTorch 与 Ultralytics。若当时离线或源不可达，可在这里手动重试 / 切换后端（需下载数百 MB）；失败会自动回退 CPU，不会破坏环境。';
  } catch (e) { $('hwDetail').innerHTML = '<div class="hint">' + esc(e.message) + '</div>'; }
  await renderEnvRest();
}
async function renderEnvRest() {
  try {
    const cfg = await api('/api/config'); S.config = cfg;
    $('cfgMirror').value = cfg.mirror || 'tsinghua';
    $('cfgAppMode').checked = !!cfg.app_mode;
    fillModelSelectors();
    const models = await api('/api/models');
    $('modelList').innerHTML = (models.presets || []).map(m => {
      const has = (models.local || []).some(x => x.name === m.name);
      const action = m.offline
        ? '<span class="badge badge-ok">无需下载</span>'
        : `<button class="btn btn-sm ${has ? '' : 'btn-primary'}" data-dl="${esc(m.name)}" ${has ? 'disabled' : ''}>${has ? '已存在' : '下载'}</button>`;
      return `<div class="model-item"><div><div class="m-name">${esc(m.name)}</div>
        <div class="m-desc">${esc(m.desc)}</div></div>${action}</div>`;
    }).join('');
    $('modelList').querySelectorAll('[data-dl]').forEach(b => b.onclick = async () => {
      toast('开始下载 ' + b.dataset.dl);
      try {
        const t = await api('/api/task/start', { type: 'download_model',
          params: { model: b.dataset.dl }, label: '下载 ' + b.dataset.dl });
        const st = await watchTask(t.id, { onLog: txt => appendLog($('hwLog'), txt) });
        toast(st.status === 'done' ? '下载完成' : '下载失败: ' + (st.error || ''), st.status !== 'done');
        renderEnvRest();
      } catch (e) { toast(e.message, true); }
    });
    const st = await api('/api/status');
    $('envRuntime').innerHTML = [
      ['安装目录', st.root], ['工作区', st.workspace], ['Python', st.config.venv_python || '-'],
      ['上次配置', st.config.last_setup || '-'], ['服务端口', st.config.port],
    ].map(r => `<div class="k">${esc(r[0])}</div><div class="v">${esc(r[1])}</div>`).join('');
    const tasks = await api('/api/tasks');
    $('taskList').innerHTML = tasks.length ? tasks.slice(-8).reverse().map(t =>
      `<div class="task-item"><span>${esc(t.label || t.type)}</span>
       <span class="badge ${t.status === 'done' ? 'badge-ok' : t.status === 'running' ? 'badge-info' : 'badge-err'}">${esc(t.status)}</span></div>`).join('')
      : '<div class="hint">暂无任务</div>';
  } catch (e) { /* non critical */ }
}
$('btnRefreshHw').onclick = renderEnv;
$('btnOpenModels').onclick = () => api('/api/open', { path: S.models.models_dir }).catch(e => toast(e.message, true));
$('btnImportModel').onclick = () => showModal('导入本地权重', `
  <p class="hint">把已有的 .pt / .onnx 权重复制到软件的模型目录，之后即可在训练和推理里选择。</p>
  <label>文件完整路径<input id="impPath" placeholder="C:\\models\\best.pt"></label>`, async () => {
  const p = $('impPath').value.trim().replace(/^"|"$/g, '');
  if (!p) { toast('请填写文件路径', true); return; }
  try {
    const r = await api('/api/model/import', { path: p });
    toast('已导入 ' + r.name); await loadCore(); renderEnvRest();
  } catch (e) { toast(e.message, true); }
});
$('btnCustomUrl').onclick = () => {
  const opts = (S.models.presets || []).filter(m => !m.offline)
    .map(m => `<option value="${esc(m.name)}">${esc(m.name)}</option>`).join('');
  showModal('自定义下载地址', `
    <p class="hint">填写可直接下载的 .pt 链接，软件会以指定文件名保存到模型目录。</p>
    <label>保存为<select id="cuName">${opts}</select></label>
    <label>下载地址<input id="cuUrl" placeholder="https://.../yolo26n.pt"></label>`, async () => {
    const url = $('cuUrl').value.trim(), name = $('cuName').value;
    if (!url) { toast('请填写下载地址', true); return; }
    $('hwLog').textContent = '';
    try {
      const t = await api('/api/task/start', { type: 'download_model',
        params: { model: name, url }, label: '下载 ' + name });
      toast('下载任务已启动');
      const st = await watchTask(t.id, { onLog: txt => appendLog($('hwLog'), txt) });
      toast(st.status === 'done' ? '下载完成' : '下载失败，请查看日志', st.status !== 'done');
      await loadCore(); renderEnvRest();
    } catch (e) { toast(e.message, true); }
  });
};
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
  go('home');
  await loadCore();
  await renderHome();
  setInterval(async () => {
    try {
      const st = await api('/api/status'); S.status = st; updateBadges();
    } catch (e) { /* service may be shutting down */ }
  }, 5000);
})();
