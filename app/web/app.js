/* 可循环周转箱台账 —— 原生 JS，零依赖。 */
'use strict';
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const yuan = c => `¥${((c || 0) / 100).toFixed(2)}`;
const ROLE_NAME = {warehouse: '仓库 · 实物', finance: '财务 · 折抵', admin: '管理员 · 全部'};

let state = {
  role: localStorage.getItem('role') || 'warehouse',
  customers: [], types: [],
  returnCid: null, scanItems: [], scanCtx: null, photos: {},
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: {'Content-Type': 'application/json', 'X-Role': state.role},
    ...opts,
    body: opts.body ? (() => {
      const b = typeof opts.body === 'string' ? JSON.parse(opts.body) : opts.body;
      b._role = state.role;
      return JSON.stringify(b);
    })() : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.message || res.statusText), {data, status: res.status});
  return data;
}
const get = p => api(p);
const post = (p, b) => api(p, {method: 'POST', body: JSON.stringify(b)});

function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = `toast show ${kind}`;
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = 'toast'), 3600);
}

function can(action) {
  if (state.role === 'admin') return true;
  if (['issue', 'return', 'repair_done'].includes(action)) return state.role === 'warehouse';
  return state.role === 'finance';
}
const deny = a => toast(`当前角色无权：${ROLE_NAME[state.role]}。请在左下角切换角色。`, 'err');

const STATE_TAG = {
  OUT: ['rep', '在客户处'], IN_STOCK: ['ok', '库内可用'], REPAIR: ['rep', '维修中'],
  SCRAPPED: ['bad', '已报废'], LOST: ['bad', '丢失核销'],
};
const VERDICT_TAG = {
  OK: ['ok', '合格入库'], REPAIR: ['rep', '送维修'], SCRAP: ['bad', '报废'],
};

/* ---------------- 路由 ---------------- */
const routes = {
  dashboard: ['总览', '全厂周转箱与押金概览', renderDashboard],
  customers: ['客户箱账', '按客户汇总，点击展开到箱号', renderCustomers],
  issue: ['发箱扫描', '仓库：按出库批次把箱号分配给客户并收押金', renderIssue],
  return: ['返箱验收', '扫码检查箱体/封盖/标签，折抵金额随验收结论即时生成', renderReturn],
  repair: ['维修池', '仓库修完回库；财务复核维修折抵', renderRepair],
  overdue: ['逾期清单', '长期未返箱子：财务可核销并转赔偿', renderOverdue],
  ledger: ['押金流水', '逐箱押金收支与财务回执', renderLedger],
};
async function boot() {
  $('#roleSel').value = state.role;
  $('#roleSel').onchange = e => {
    state.role = e.target.value;
    localStorage.setItem('role', state.role);
    render();
  };
  window.onhashchange = render;
  await render();
}
async function render() {
  const key = location.hash.replace('#', '') || 'dashboard';
  const [title, sub, fn] = routes[key] || routes.dashboard;
  $('#pageTitle').textContent = title;
  $('#pageSub').textContent = sub;
  $('#roleBadge').textContent = ROLE_NAME[state.role];
  $$('#nav a').forEach(a => a.classList.toggle('on',
    a.getAttribute('href') === `#${routes[key] ? key : 'dashboard'}`));
  try {
    state.customers = await get('/api/customers');
    state.types = await get('/api/types');
    await fn($('#view'));
  } catch (e) {
    $('#view').innerHTML = `<div class="card"><div class="empty">加载失败：${esc(e.message)}</div></div>`;
  }
}
const custName = id => state.customers.find(c => c.id === id)?.name || `#${id}`;
const typeOf = code => state.types.find(t => t.code === code);
const custSel = (cid, id = 'cidSel') =>
  `<select id="${id}">${state.customers.map(c =>
    `<option value="${c.id}" ${c.id === cid ? 'selected' : ''}>${esc(c.code)} · ${esc(c.name)}</option>`).join('')}</select>`;

/* ---------------- 总览 ---------------- */
async function renderDashboard(v) {
  const d = await get('/api/dashboard');
  v.innerHTML = `
    <div class="grid k4">
      <div class="stat blue"><div class="n">${d.out_total}</div><div class="l">在客户处（应返）</div></div>
      <div class="stat amber"><div class="n">${d.repair_pool}</div><div class="l">维修池待处理</div></div>
      <div class="stat red"><div class="n">${d.overdue}</div><div class="l">逾期未返</div></div>
      <div class="stat green"><div class="n">${yuan(d.customers.reduce((s, c) => s + c.balance_cents, 0))}</div><div class="l">押金池余额（在库冻结）</div></div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-h">客户押金与箱量</div>
      <table>
        <thead><tr><th>客户</th><th class="num">发出</th><th class="num">合格实返</th><th class="num">在客户处</th>
          <th class="num">待修</th><th class="num">报废</th><th class="num">丢失</th>
          <th class="num">已收押金</th><th class="num">已退</th><th class="num">已折抵</th>
          <th class="num">待复核折抵</th><th class="num">押金余额</th></tr></thead>
        <tbody>${d.customers.map(c => `
          <tr class="click" onclick="location.hash='#customers';setTimeout(()=>toggleCust(${c.id}),60)">
            <td><b>${esc(c.code)}</b> ${esc(c.name)}</td>
            <td class="num">${c.issued}</td><td class="num">${c.returned_ok}</td>
            <td class="num">${c.out}</td><td class="num">${c.in_repair}</td>
            <td class="num">${c.scrapped}</td><td class="num">${c.lost}</td>
            <td class="num">${yuan(c.deposit_charged_cents)}</td>
            <td class="num green">${yuan(c.refunded_cents)}</td>
            <td class="num red">${yuan(c.deducted_cents)}</td>
            <td class="num"><span class="${c.pending_deduct_cents ? 'pending' : ''}">${yuan(c.pending_deduct_cents)}</span></td>
            <td class="num"><b>${yuan(c.balance_cents)}</b></td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}
window.toggleCust = async function (id) {
  const row = $(`#cust-${id}`);
  if (row) { row.remove(); return; }
  const boxes = await get(`/api/customers/${id}/boxes`);
  const tr = document.createElement('tr');
  tr.id = `cust-${id}`;
  tr.innerHTML = `<td colspan="12" class="subtable">
    <b>逐箱台账（${boxes.length}）</b>
    <table style="margin-top:8px">
      <thead><tr><th>箱号</th><th>箱型</th><th>状态</th><th>出库批次</th><th>应返期</th>
        <th>返还时间</th><th>照片</th><th>备注</th><th></th></tr></thead>
      <tbody>${boxes.map(b => `
        <tr>
          <td><b>${esc(b.code)}</b></td><td>${esc(b.type_code)}</td>
          <td><span class="tag ${STATE_TAG[b.state][0]}">${STATE_TAG[b.state][1]}</span>
              ${b.disposition === 'REPAIR' ? '<span class="tag rep">待财务复核</span>' : ''}</td>
          <td>${b.batch_code ? `<span class="pill-batch">${esc(b.batch_code)}</span>` : '—'}</td>
          <td>${b.due_at || '—'}</td><td>${b.returned_at || '—'}</td>
          <td>${b.photo_path ? `<img class="photo-thumb" src="/${b.photo_path}">` : ''}</td>
          <td class="muted">${esc(b.note || '')}</td>
          <td><span class="expand" onclick="showEvents('${b.code}',event)">事件流</span></td>
        </tr>`).join('')}
      </tbody>
    </table></td>`;
  $(`#custrow-${id}`).after(tr);
};
window.showEvents = async function (code, ev) {
  ev.stopPropagation();
  const rows = await get(`/api/boxes/${encodeURIComponent(code)}/events`);
  const names = {ISSUE: '发箱出库', RETURN_OK: '合格返箱入库', RETURN_REPAIR: '返箱→维修',
    RETURN_SCRAP: '返箱→报废', REPAIR_DONE: '维修完成回库', FINANCE_REVIEW: '财务复核折抵',
    SETTLE_LOST: '逾期核销赔偿', DISPUTE_OPEN: '押金争议登记', DISPUTE_RESOLVE: '争议处理'};
  toast(`${code}：` + rows.map(r => `${names[r.event] || r.event}(${r.actor_role})`).join(' → '));
};

/* ---------------- 客户箱账 ---------------- */
async function renderCustomers(v) {
  v.innerHTML = `<div class="card">
    <div class="card-h">客户箱账 <span class="legend">（汇总行可展开到箱号；押金口径：余额 = 已收 − 已退 − 已折抵）</span></div>
    <table><thead><tr><th style="width:22px"></th><th>客户</th>
      <th class="num">发出</th><th class="num">合格实返</th><th class="num">在客户处(应返)</th>
      <th class="num">待修</th><th class="num">报废/丢失</th>
      <th class="num">已收押金</th><th class="num">已退</th><th class="num">已折抵(确认)</th>
      <th class="num">待复核</th><th class="num">应收款</th><th class="num">押金余额</th></tr></thead>
      <tbody>${state.customers.map(c => `
        <tr id="custrow-${c.id}" class="click">
          <td class="expand" onclick="toggleCust(${c.id});event.stopPropagation()">▸</td>
          <td><b>${esc(c.code)}</b> ${esc(c.name)}</td>
          <td class="num">${c.issued}</td>
          <td class="num"><span class="tag ok">${c.returned_ok}</span></td>
          <td class="num">${c.out}${c.out ? '' : ''}</td>
          <td class="num">${c.in_repair ? `<span class="tag rep">${c.in_repair}</span>` : 0}</td>
          <td class="num">${c.scrapped + c.lost}</td>
          <td class="num">${yuan(c.deposit_charged_cents)}</td>
          <td class="num green">${yuan(c.refunded_cents)}</td>
          <td class="num red">${yuan(c.deducted_cents)}</td>
          <td class="num"><span class="${c.pending_deduct_cents ? 'pending' : ''}">${yuan(c.pending_deduct_cents)}</span></td>
          <td class="num">${yuan(c.receivable_cents + c.pending_receivable_cents)}</td>
          <td class="num"><b>${yuan(c.balance_cents)}</b></td>
        </tr>`).join('')}
      </tbody></table></div>`;
}

/* ---------------- 发箱 ---------------- */
async function renderIssue(v) {
  v.innerHTML = `<div class="grid k3">
    <div class="card" style="grid-column:span 2">
      <div class="card-h">新建出库批次</div>
      <div class="card-b">
        <div class="form-row">
          <div class="field"><label>客户</label>${custSel(null, 'iCid')}</div>
          <div class="field"><label>箱型</label>
            <select id="iType">${state.types.map(t =>
              `<option value="${t.code}">${t.code} ${esc(t.name)}（押金 ${yuan(t.deposit_cents)}）</option>`).join('')}</select></div>
          <div class="field"><label>数量</label><input id="iQty" type="number" value="5" min="1" style="width:90px"></div>
          <div class="field"><label>应返期限</label><input id="iDue" type="date" value="2026-10-20"></div>
          <div class="field"><label>批次号(可选)</label><input id="iBatch" type="text" placeholder="自动生成"></div>
          <button class="btn primary" id="iGo" ${can('issue') ? '' : 'disabled'}>发箱并收押金</button>
        </div>
        <p class="muted" style="font-size:12.5px">库内同箱型空箱会优先复用，不足自动生成新箱号；逐箱登记 ISSUE 事件与押金 CHARGE 流水。</p>
        <div id="iRes"></div>
      </div>
    </div>
    <div class="card"><div class="card-h">角色边界</div><div class="card-b">
      <span class="tag ${can('issue') ? 'ok' : 'bad'}">${can('issue') ? '仓库/管理员可发箱' : '财务角色禁止发箱'}</span>
      <p class="muted" style="font-size:12.5px;margin-top:10px">仓库只登记实物出库；押金按箱号自动入账，无需手工收款。</p>
    </div></div>
  </div>
  <div class="card" style="margin-top:14px"><div class="card-h">出库批次</div><div id="batchTbl"></div></div>`;
  $('#iGo').onclick = async () => {
    if (!can('issue')) return deny('issue');
    try {
      const r = await post('/api/issue', {
        customer_id: +$('#iCid').value, type_code: $('#iType').value,
        qty: +$('#iQty').value, due_at: $('#iDue').value,
        batch_code: $('#iBatch').value.trim() || null,
      });
      $('#iRes').innerHTML = `<div class="result-ok">
        批次 <b>${esc(r.batch_code)}</b> 已出库 ${r.boxes.length} 箱，收取押金
        <b class="money green">${yuan(r.deposit_total_cents)}</b>：
        ${r.boxes.map(x => `<span class="pill-batch">${esc(x.box_code)}</span>`).join(' ')}</div>`;
      toast('发箱成功', 'ok');
      loadBatches();
    } catch (e) { toast(e.message, 'err'); }
  };
  loadBatches();
}
async function loadBatches() {
  const bs = await get('/api/batches');
  $('#batchTbl').innerHTML = `<table><thead><tr><th>批次</th><th>客户</th><th>箱型</th>
    <th class="num">发出</th><th class="num">在客户处</th><th class="num">库内</th>
    <th class="num">待修</th><th class="num">报废</th><th class="num">丢失</th><th>应返期</th></tr></thead>
    <tbody>${bs.map(b => `<tr>
      <td><b>${esc(b.code)}</b></td><td>${esc(b.customer_name)}</td><td>${esc(b.type_code)}</td>
      <td class="num">${b.qty}</td>
      <td class="num">${b.out_qty ? `<span class="tag rep">${b.out_qty}</span>` : 0}</td>
      <td class="num">${b.in_qty || 0}</td>
      <td class="num">${b.repair_qty ? `<span class="tag rep">${b.repair_qty}</span>` : 0}</td>
      <td class="num">${b.scrap_qty || 0}</td>
      <td class="num">${b.lost_qty || 0}</td><td>${b.due_at}</td></tr>`).join('')}</tbody></table>`;
}

/* ---------------- 返箱验收 ---------------- */
async function renderReturn(v) {
  v.innerHTML = `
    <div class="card"><div class="card-b">
      <div class="form-row">
        <div class="field"><label>返箱客户</label>${custSel(state.returnCid, 'rCid')}</div>
        <div class="field"><label>核对批次</label><select id="rBatch" style="min-width:230px"></select></div>
        <label class="check"><input type="checkbox" id="rCross"> 允许跨批次放行（混批）</label>
        <span class="sp" style="flex:1"></span>
        <button class="btn" id="rClear">清空扫码清单</button>
      </div>
    </div></div>
    <div class="grid k4" id="rStats" style="margin-top:14px"></div>
    <div class="card" style="margin-top:14px">
      <div class="card-h">扫码 / 录入箱号
        <span class="sp"></span>
        <span class="tag gray">箱体</span><span class="tag gray">封盖</span><span class="tag gray">标签</span>
        <span class="legend">全部合格自动判“合格”，任一不合格默认“送维修”，可手改</span>
      </div>
      <div class="card-b">
        <div class="scanbox">
          <input id="rCode" type="text" placeholder="扫描或输入箱号后回车，如 R60-00008" autocomplete="off">
          <input id="rFile" type="file" accept="image/*" style="display:none">
          <button class="btn" id="rAdd">加入验收清单</button>
        </div>
        <div id="rItems" style="margin-top:14px"></div>
        <div class="right-actions" style="margin-top:6px">
          <button class="btn primary" id="rSubmit" ${can('return') ? '' : 'disabled'}>
            提交验收并即时折抵（${0} 箱）</button>
        </div>
        <div id="rResult"></div>
      </div>
    </div>`;
  $('#rCid').onchange = async e => {
    state.returnCid = +e.target.value;
    state.scanItems = [];
    await loadReturnCtx();
  };
  state.returnCid = state.returnCid || state.customers[0]?.id || null;
  $('#rCid').value = state.returnCid;
  $('#rCode').focus();
  $('#rCode').onkeydown = e => { if (e.key === 'Enter') { $('#rAdd').click(); } };
  $('#rAdd').onclick = addScanItem;
  $('#rClear').onclick = () => { state.scanItems = []; drawItems(); };
  $('#rSubmit').onclick = submitReturn;
  $('#rBatch').onchange = drawItems;
  $('#rCross').onchange = drawItems;
  await loadReturnCtx();
}
async function loadReturnCtx() {
  state.scanCtx = await get(`/api/return-context?customer_id=${state.returnCid}`);
  const x = state.scanCtx;
  const opts = [`<option value="">混批（不区分批次，自动按箱归批）</option>`]
    .concat(x.open_batches.map(b =>
      `<option value="${b.id}">${esc(b.code)} · ${b.type_code} · 未返 ${b.open_qty}（应返 ${b.due_at}）</option>`));
  $('#rBatch').innerHTML = opts.join('');
  $('#rStats').innerHTML = `
    <div class="stat blue"><div class="n">${x.should_return}</div><div class="l">应返（在客户处）</div></div>
    <div class="stat green"><div class="n">${x.returned_ok}</div><div class="l">实返（合格已入库）</div></div>
    <div class="stat amber"><div class="n">${x.in_repair}</div><div class="l">待修（维修池）</div></div>
    <div class="stat ${x.overdue ? 'red' : 'green'}"><div class="n">${x.overdue}</div><div class="l">其中逾期</div></div>`;
  drawItems();
}
function findOutBox(code) {
  return state.scanCtx.out_boxes.find(b => b.code === code.trim().toUpperCase());
}
async function addScanItem() {
  const code = $('#rCode').value.trim().toUpperCase();
  if (!code) return;
  if (state.scanItems.some(i => i.box_code === code)) { toast('清单中已有该箱号', 'err'); return; }
  const box = findOutBox(code);
  const item = {box_code: code, body_ok: true, lid_ok: true, label_ok: true,
    verdict: box ? 'OK' : 'OK', note: '', photo_data: null};
  state.scanItems.push(item);
  $('#rCode').value = '';
  $('#rCode').focus();
  if (!box) toast(`提示：${code} 不在该客户“应返”清单中，提交后将按规则拒收`, 'err');
  drawItems();
}
function previewAdj(box, verdict) {
  if (!box) return null;
  const t = typeOf(box.type_code);
  if (verdict === 'OK') return {kind: '退押金', amt: t.deposit_cents, cls: 'green', pending: false};
  if (verdict === 'REPAIR') {
    return {kind: '维修抵扣(待复核)', amt: Math.min(t.deposit_cents, t.repair_cents),
      extra: Math.max(0, t.repair_cents - t.deposit_cents), cls: 'amber', pending: true};
  }
  return {kind: '报废赔偿抵扣(待复核)', amt: Math.min(t.deposit_cents, t.price_cents),
    extra: Math.max(0, t.price_cents - t.deposit_cents), cls: 'red', pending: true};
}
function drawItems() {
  const wrap = $('#rItems');
  if (!wrap) return;
  const batchId = $('#rBatch') ? +$('#rBatch').value || null : null;
  wrap.innerHTML = state.scanItems.length ? state.scanItems.map((it, idx) => {
    const box = findOutBox(it.box_code);
    const cross = box && batchId && box.batch_id !== batchId;
    const allowCross = $('#rCross')?.checked;
    const verdict = it.verdict || ((it.body_ok && it.lid_ok && it.label_ok) ? 'OK' : 'REPAIR');
    const adj = previewAdj(box, verdict);
    return `<div class="scan-item">
      <div>
        <div class="code">${esc(it.box_code)}</div>
        <div class="batch">${box ? `${esc(box.type_code)} · <span class="pill-batch">${esc(box.batch_code)}</span> · 应返 ${box.due_at}`
          : '<span class="tag bad">非本客户应返箱号</span>'}</div>
        ${cross ? `<div class="mix-note" style="margin:6px 0 0">跨批次箱号${allowCross ? '（已勾选允许混批，将放行并标注）' : '（不勾选允许混批将被拒收）'}</div>` : ''}
      </div>
      <div class="checks">
        <label class="check"><input type="checkbox" ${it.body_ok ? 'checked' : ''}
          onchange="updItem(${idx},'body_ok',this.checked)">箱体完好</label>
        <label class="check"><input type="checkbox" ${it.lid_ok ? 'checked' : ''}
          onchange="updItem(${idx},'lid_ok',this.checked)">封盖完好</label>
        <label class="check"><input type="checkbox" ${it.label_ok ? 'checked' : ''}
          onchange="updItem(${idx},'label_ok',this.checked)">标签清晰</label>
      </div>
      <div class="field"><label>验收结论</label>
        <select onchange="updItem(${idx},'verdict',this.value)">
          <option value="OK" ${verdict === 'OK' ? 'selected' : ''}>合格 · 重新入库</option>
          <option value="REPAIR" ${verdict === 'REPAIR' ? 'selected' : ''}>破损 · 送维修</option>
          <option value="SCRAP" ${verdict === 'SCRAP' ? 'selected' : ''}>报废</option>
        </select></div>
      <input type="text" placeholder="备注" value="${esc(it.note)}" onchange="updItem(${idx},'note',this.value)" style="width:130px">
      <label class="btn sm" style="margin-top:18px">破损照片
        <input type="file" accept="image/*" style="display:none" onchange="attachPhoto(${idx},this)"></label>
      ${it.photo_data ? '<span class="tag ok" style="margin-top:20px">已拍照</span>' : ''}
      <div class="right">
        <div class="concl"><span class="tag ${VERDICT_TAG[verdict][0]}">${VERDICT_TAG[verdict][1]}</span></div>
        ${adj ? `<div class="money ${adj.cls}" style="margin-top:5px">${adj.kind}：${yuan(adj.amt)}
            ${adj.pending ? '<span class="pending">待财务复核</span>' : ''}</div>
          ${adj.extra ? `<div class="adj">押金不足，另应收 <b class="money red">${yuan(adj.extra)}</b></div>` : ''}`
          : '<div class="adj muted">提交后拒收，不产生折抵</div>'}
      </div>
      <button class="btn sm danger" style="margin-top:16px" onclick="delItem(${idx})">移除</button>
    </div>`;
  }).join('') : '<div class="empty">尚未扫码。扫描箱号后逐箱检查并给出验收结论，折抵金额显示在结论旁。</div>';
  const btn = $('#rSubmit');
  if (btn) {
    btn.textContent = `提交验收并即时折抵（${state.scanItems.length} 箱）`;
    btn.disabled = !can('return') || state.scanItems.length === 0;
  }
}
window.updItem = (i, k, v) => {
  const it = state.scanItems[i];
  it[k] = v;
  if (k !== 'verdict' && k !== 'note')
    it.verdict = (it.body_ok && it.lid_ok && it.label_ok) ? 'OK' : 'REPAIR';
  drawItems();
};
window.delItem = i => { state.scanItems.splice(i, 1); drawItems(); };
window.attachPhoto = (i, input) => {
  const f = input.files[0];
  if (!f) return;
  const rd = new FileReader();
  rd.onload = () => { state.scanItems[i].photo_data = rd.result; drawItems(); };
  rd.readAsDataURL(f);
};
async function submitReturn() {
  if (!can('return')) return deny('return');
  const batchVal = $('#rBatch').value;
  try {
    const r = await post('/api/return', {
      customer_id: state.returnCid,
      expected_batch_id: batchVal ? +batchVal : null,
      allow_cross_batch: $('#rCross').checked,
      items: state.scanItems.map(({photo_data, ...rest}) => ({
        ...rest, photo_data: photo_data || undefined,
      })),
    });
    const ok = r.accepted, rej = r.rejected;
    $('#rResult').innerHTML = `
      <div class="result-ok"><b>回执 ${esc(r.ref)}</b>（已写入财务模拟适配器）
        ${r.mixed_batch ? '<span class="tag blue">混批返还</span>' : ''}
        受理 ${ok.length} 箱：实退 <b class="money green">${yuan(r.totals.refund_cents)}</b>，
        折抵 <b class="money amber">${yuan(r.totals.deduct_cents)}</b>（待复核），
        另应收 <b class="money red">${yuan(r.totals.receivable_cents)}</b>
        <table style="margin-top:8px"><thead><tr><th>箱号</th><th>来源批次</th><th>验收结论</th>
          <th class="num">押金</th><th class="num">折抵/退款（紧挨结论）</th></tr></thead>
          <tbody>${ok.map(l => `<tr>
            <td><b>${esc(l.box_code)}</b>${l.cross_batch ? ' <span class="tag blue">跨批</span>' : ''}</td>
            <td><span class="pill-batch">${esc(l.batch_code)}</span></td>
            <td><span class="tag ${VERDICT_TAG[l.verdict][0]}">${VERDICT_TAG[l.verdict][1]}</span></td>
            <td class="num">${yuan(l.deposit_cents)}</td>
            <td class="num">${l.refund_cents ? `<span class="money green">退 ${yuan(l.refund_cents)}</span>` : ''}
              ${l.deduct_cents ? `<span class="money amber">抵 ${yuan(l.deduct_cents)}（待复核）</span>` : ''}
              ${l.receivable_cents ? `<div class="adj money red">另应收 ${yuan(l.receivable_cents)}</div>` : ''}</td>
          </tr>`).join('')}</tbody></table>
      </div>
      ${rej.length ? `<div class="result-rej"><b>拒收 ${rej.length} 箱（不计折抵）：</b>
        ${rej.map(x => `<div>· ${esc(x.box_code)} — ${esc(x.message)} <span class="tag bad">${x.reason}</span></div>`).join('')}</div>` : ''}`;
    toast(`受理 ${ok.length}，拒收 ${rej.length}`, 'ok');
    state.scanItems = [];
    await loadReturnCtx();
  } catch (e) { toast(e.message, 'err'); }
}

/* ---------------- 维修池 ---------------- */
async function renderRepair(v) {
  const rows = await get('/api/repair-pool');
  v.innerHTML = `<div class="card"><div class="card-h">维修池
    <span class="sp"></span><span class="legend">仓库负责“修完回库”；折抵确认/豁免仅财务可操作</span></div>
    ${rows.length ? `<table><thead><tr><th>照片</th><th>箱号</th><th>箱型</th><th>客户</th>
      <th>来源批次</th><th>返还时间</th><th class="num">维修抵扣</th><th>备注</th><th>操作</th></tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td>${r.photo_path ? `<img class="photo-thumb" src="/${r.photo_path}">` : ''}</td>
          <td><b>${esc(r.code)}</b></td><td>${esc(r.type_code)}</td>
          <td>${esc(r.customer_name)}</td><td><span class="pill-batch">${esc(r.batch_code)}</span></td>
          <td>${r.returned_at}</td>
          <td class="num">${r.deduct_cents ? `<span class="money ${r.provisional ? 'amber' : 'red'}">
              ${yuan(r.deduct_cents)}</span>${r.provisional ? ' <span class="pending">待复核</span>' : ' <span class="tag ok">已确认</span>'}` : '—'}</td>
          <td class="muted">${esc(r.note || '')}</td>
          <td style="min-width:300px">
            <div class="right-actions">
              <button class="btn sm" ${can('return') ? '' : 'disabled'}
                onclick="repairDone(${r.id})">修完重新入库</button>
              <button class="btn sm primary" ${can('review') ? '' : 'disabled'}
                onclick="reviewBox(${r.id},'CONFIRM')">财务确认折抵</button>
              <button class="btn sm danger" ${can('review') ? '' : 'disabled'}
                onclick="reviewBox(${r.id},'WAIVE')">豁免</button>
            </div>
          </td>
        </tr>`).join('')}</tbody></table>`
      : '<div class="empty">维修池为空</div>'}
    </div>
    <div class="card" style="margin-top:14px"><div class="card-h">财务复核口径</div><div class="card-b muted" style="font-size:13px">
      确认时可按实际维修工单调整金额（押金内抵扣，不足转应收）；豁免后该箱折抵不成立、押金不被扣减。
      操作均产生 FINANCE_REVIEW 事件与财务回执，流水可逐笔复核。</div></div>`;
}
window.repairDone = async id => {
  if (!can('repair_done')) return deny('repair_done');
  try { await post(`/api/repair/${id}/done`, {}); toast('已维修完成并重新入库', 'ok'); render(); }
  catch (e) { toast(e.message, 'err'); }
};
window.reviewBox = async (id, action) => {
  if (!can('review')) return deny('review');
  const fee = prompt(action === 'CONFIRM'
    ? '确认维修折抵：输入实际维修费（元，留空用标准费）' : '确认豁免该箱维修折抵？输入备注（可留空）',
    action === 'CONFIRM' ? '' : '客户无责');
  if (fee === null) return;
  try {
    const body = {box_id: id, action, note: action === 'WAIVE' ? (fee || '客户无责') : ''};
    if (action === 'CONFIRM' && fee !== '') body.repair_fee_yuan = +fee;
    const r = await post('/api/finance/review', body);
    toast(`复核完成（回执 ${r.ref}）`, 'ok');
    render();
  } catch (e) { toast(e.message, 'err'); }
};

/* ---------------- 逾期清单 ---------------- */
async function renderOverdue(v) {
  const rows = await get('/api/overdue');
  v.innerHTML = `<div class="card"><div class="card-h">长期未返（超过应返期）
    <span class="sp"></span><span class="legend">财务核销：箱子转 LOST，押金抵赔偿、不足转应收</span></div>
    ${rows.length ? `<table><thead><tr><th>箱号</th><th>箱型</th><th>客户</th><th>出库批次</th>
      <th>应返期</th><th class="num">逾期天数</th><th class="num">赔偿基准(采购价)</th><th>操作</th></tr></thead>
      <tbody>${rows.map(r => `<tr>
        <td><b>${esc(r.code)}</b></td><td>${esc(r.type_code)}</td>
        <td>${esc(r.customer_name)}</td><td><span class="pill-batch">${esc(r.batch_code)}</span></td>
        <td>${r.due_at}</td><td class="num"><span class="tag bad">${r.days_overdue} 天</span></td>
        <td class="num">${yuan(r.price_cents)}</td>
        <td><button class="btn sm danger" ${can('settle_lost') ? '' : 'disabled'}
          onclick="settleLost(${r.id})">核销并转赔偿</button></td></tr>`).join('')}</tbody></table>`
      : '<div class="empty">没有逾期箱子</div>'}</div>`;
}
window.settleLost = async id => {
  if (!can('settle_lost')) return deny('settle_lost');
  if (!confirm('确认按长期未返核销？押金将抵赔偿，不足部分转应收款。')) return;
  try { const r = await post(`/api/finance/settle/${id}`, {note: '长期未返核销'});
    toast(`已核销：抵扣 ${yuan(r.comp_cents)}，应收 ${yuan(r.receivable_cents)}`, 'ok'); render();
  } catch (e) { toast(e.message, 'err'); }
};

/* ---------------- 押金流水 / 争议 ---------------- */
const KIND_NAME = {
  CHARGE: ['收押金', 'gray'], REFUND: ['退押金', 'ok'],
  REPAIR_DEDUCT: ['维修抵扣', 'rep'], COMPENSATION: ['赔偿抵扣', 'bad'],
  REPAIR_RECEIVABLE: ['应收维修费', 'bad'], RECEIVABLE: ['应收赔偿', 'bad'],
  ADJUST: ['财务调整', 'blue'], RECEIVABLE_REVERSE: ['应收冲销', 'ok'],
};
async function renderLedger(v) {
  const disputes = await get('/api/disputes');
  v.innerHTML = `
    <div class="card"><div class="card-h">押金争议
      <span class="sp"></span>
      <select id="dCid" style="min-width:180px">${state.customers.map(c =>
        `<option value="${c.id}">${esc(c.name)}</option>`).join('')}</select>
      <input id="dBox" type="text" placeholder="争议箱号" style="width:130px">
      <input id="dAmt" type="number" placeholder="主张金额(元)" style="width:120px">
      <button class="btn sm" id="dAdd" ${can('dispute_open') ? '' : 'disabled'}>登记争议</button>
    </div>
    ${disputes.length ? `<table><thead><tr><th>箱号</th><th>客户</th><th class="num">主张金额</th>
      <th>状态</th><th>说明 / 处理结果</th><th>操作</th></tr></thead><tbody>
      ${disputes.map(d => `<tr>
        <td><b>${esc(d.box_code)}</b></td><td>${esc(d.customer_name)}</td>
        <td class="num">${yuan(d.claim_amount_cents)}</td>
        <td>${d.status === 'OPEN' ? '<span class="tag rep">待处理</span>' : '<span class="tag ok">已解决</span>'}</td>
        <td class="muted">${esc(d.note)} ${esc(d.resolution)}</td>
        <td>${d.status === 'OPEN' ? `
          <button class="btn sm primary" ${can('dispute_resolve') ? '' : 'disabled'}
            onclick="resolveDispute(${d.id})">财务处理（可补退）</button>` : ''}</td>
      </tr>`).join('')}</tbody></table>`
      : '<div class="empty">暂无押金争议</div>'}</div>

    <div class="card" style="margin-top:14px">
      <div class="card-h">押金流水（逐箱事件）
        <span class="sp"></span>
        <select id="lCid"><option value="">全部客户</option>
          ${state.customers.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('')}
        </select></div>
      <div id="lTbl"></div></div>`;
  $('#dAdd').onclick = async () => {
    if (!can('dispute_open')) return deny('dispute_open');
    const code = $('#dBox').value.trim().toUpperCase();
    const cid = +$('#dCid').value;
    if (!code || !$('#dAmt').value) { toast('请填写争议箱号和主张金额', 'err'); return; }
    try {
      const boxes = await get(`/api/customers/${cid}/boxes`);
      const b = boxes.find(x => x.code === code);
      if (!b) throw new Error('该客户名下未找到此箱号（箱号归属不符）');
      await post('/api/finance/disputes', {
        box_id: b.id, claim_yuan: +$('#dAmt').value, note: '客户押金异议',
      });
      toast('争议已登记', 'ok'); render();
    } catch (e) { toast(e.message, 'err'); }
  };
  $('#lCid').onchange = loadLedger;
  await loadLedger();
}
async function loadLedger() {
  const cid = $('#lCid').value;
  const rows = await get('/api/ledger' + (cid ? `?customer_id=${cid}` : ''));
  $('#lTbl').innerHTML = `<table><thead><tr><th>时间</th><th>客户</th><th>箱号</th>
    <th>出库批次</th><th>类型</th><th class="num">金额(正收/负退)</th><th>状态</th>
    <th>说明</th></tr></thead><tbody>
    ${rows.map(r => {
      const [name, cls] = KIND_NAME[r.kind] || [r.kind, 'gray'];
      return `<tr>
        <td class="muted">${r.created_at}</td><td>${esc(r.customer_name)}</td>
        <td><b>${esc(r.box_code)}</b></td>
        <td>${r.batch_code ? `<span class="pill-batch">${esc(r.batch_code)}</span>` : '—'}</td>
        <td><span class="tag ${cls}">${name}</span></td>
        <td class="num money ${r.amount_cents >= 0 ? 'red' : 'green'}">${yuan(Math.abs(r.amount_cents))}
          ${r.amount_cents >= 0 ? '收/扣' : '退/冲'}</td>
        <td>${r.waived ? '<span class="tag gray">已豁免</span>'
          : r.provisional ? '<span class="tag rep">待财务复核</span>'
          : '<span class="tag ok">已生效</span>'}</td>
        <td class="muted">${esc(r.note)}</td></tr>`;
    }).join('')}</tbody></table>`;
}
window.resolveDispute = async id => {
  if (!can('dispute_resolve')) return deny('dispute_resolve');
  const text = prompt('处理结论：') || '核实后处理';
  const refund = +(prompt('批准补退金额（元，0 表示不补退）', '0') || 0);
  try {
    await post(`/api/finance/disputes/${id}/resolve`,
      {resolution: text, approved_refund_yuan: refund});
    toast('争议已处理', 'ok'); render();
  } catch (e) { toast(e.message, 'err'); }
};

boot();
