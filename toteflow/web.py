"""本地 Web 应用：纯标准库实现，无第三方依赖。

角色：warehouse（发箱/验收/维修，只读押金流水）、finance（复核折抵/争议/核销）、
admin（全部，含跨批次返还确认）。
"""
from __future__ import annotations

import html
import json
import secrets
import threading
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path

from . import config, photos, queries, services
from .db import connect, init_db
from .errors import ToteError

# ---------------------------------------------------------------- 会话

_SESSIONS: dict[str, dict] = {}
_LOCK = threading.Lock()

# 工业白 / 深灰 / 清晰绿
COLORS = {
    "bg": "#f5f6f7", "panel": "#ffffff", "ink": "#2b3034", "sub": "#6b7278",
    "line": "#d9dde1", "head": "#363c42", "green": "#1e8e4a", "green_bg": "#e7f5ec",
    "amber": "#b7791f", "amber_bg": "#fdf3e0", "red": "#c0392b", "red_bg": "#fbeae8",
    "blue": "#2b6cb0", "blue_bg": "#e8f1fa",
}


def make_session(user: dict) -> str:
    token = secrets.token_hex(16)
    with _LOCK:
        _SESSIONS[token] = user
    return token


def drop_session(token: str) -> None:
    with _LOCK:
        _SESSIONS.pop(token, None)


def get_user(handler) -> dict | None:
    cookie = SimpleCookie()
    raw = handler.headers.get("Cookie", "")
    if raw:
        cookie.load(raw)
        morsel = cookie.get("tf_session")
        if morsel:
            with _LOCK:
                return _SESSIONS.get(morsel.value)
    return None

# ---------------------------------------------------------------- HTML 辅助


def h(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def money(v) -> str:
    return f"{float(v or 0):,.2f}"


def badge(text, color="green") -> str:
    bg = COLORS.get(f"{color}_bg", COLORS["green_bg"])
    fg = COLORS.get(color, COLORS["green"])
    return (f'<span class="badge" style="background:{bg};color:{fg}">'
            f'{h(text)}</span>')


STATE_BADGE = {
    "out": lambda: badge("在外", "amber"),
    "stock": lambda: badge("在库", "green"),
    "repair": lambda: badge("待修", "blue"),
    "scrapped": lambda: badge("已报废", "red"),
    "lost": lambda: badge("已核销", "red"),
}


def state_badge(s):
    return STATE_BADGE.get(s, lambda: badge(s, "sub"))()


SHELL_CSS = """
* { box-sizing: border-box; }
body { margin:0; font-family: "Microsoft YaHei","PingFang SC",system-ui,sans-serif;
       background:%(bg)s; color:%(ink)s; font-size:14px; }
header { background:%(head)s; color:#fff; padding:0 24px; display:flex;
         align-items:center; gap:20px; height:52px; }
header .brand { font-weight:700; font-size:16px; letter-spacing:1px; }
header nav a { color:#cfd4d8; text-decoration:none; margin-right:18px;
               padding:4px 2px; border-bottom:2px solid transparent; }
header nav a.on, header nav a:hover { color:#fff; border-bottom-color:%(green)s; }
header .who { margin-left:auto; font-size:13px; color:#cfd4d8; }
header .who b { color:#fff; }
.wrap { max-width:1180px; margin:22px auto; padding:0 20px; }
.card { background:%(panel)s; border:1px solid %(line)s; border-radius:8px;
        margin-bottom:18px; }
.card h2 { margin:0; padding:14px 18px; font-size:15px; border-bottom:1px solid %(line)s;
           display:flex; align-items:center; gap:10px; }
.card .body { padding:16px 18px; }
table { width:100%%; border-collapse:collapse; }
th, td { text-align:left; padding:9px 10px; border-bottom:1px solid %(line)s;
         vertical-align:middle; }
th { background:#f0f2f4; font-weight:600; color:%(sub)s; font-size:12.5px;
     white-space:nowrap; }
tr:last-child td { border-bottom:none; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.badge { display:inline-block; padding:2px 9px; border-radius:11px; font-size:12px;
         font-weight:600; white-space:nowrap; }
.kpi { display:flex; gap:14px; flex-wrap:wrap; }
.kpi .box { flex:1; min-width:130px; background:#fafbfc; border:1px solid %(line)s;
            border-radius:8px; padding:12px 14px; }
.kpi .box .v { font-size:24px; font-weight:700; margin-top:2px; }
.kpi .box .k { color:%(sub)s; font-size:12.5px; }
.kpi .box.green .v { color:%(green)s; }
.kpi .box.amber .v { color:%(amber)s; }
.kpi .box.blue .v { color:%(blue)s; }
.kpi .box.red .v { color:%(red)s; }
form.inline { display:inline; }
input[type=text], input[type=password], input[type=number], select {
  padding:7px 10px; border:1px solid #b8bfc6; border-radius:5px; font-size:14px;
  background:#fff; color:%(ink)s; }
input:focus, select:focus { outline:2px solid %(green)s; outline-offset:-1px; }
label.chk { display:inline-flex; gap:5px; align-items:center; margin-right:14px;
            cursor:pointer; }
button, .btn { background:%(head)s; color:#fff; border:none; border-radius:5px;
  padding:8px 18px; font-size:14px; cursor:pointer; text-decoration:none;
  display:inline-block; }
button.green, .btn.green { background:%(green)s; }
button.ghost { background:#fff; color:%(ink)s; border:1px solid #b8bfc6; padding:6px 12px; }
button.sm { padding:4px 10px; font-size:12.5px; }
.flash { padding:11px 16px; border-radius:6px; margin-bottom:16px; font-size:13.5px; }
.flash.ok { background:%(green_bg)s; color:%(green)s; border:1px solid #bfe3cc; }
.flash.err { background:%(red_bg)s; color:%(red)s; border:1px solid #ecc6c1; }
.flash.warn { background:%(amber_bg)s; color:%(amber)s; border:1px solid #eed9ac; }
.muted { color:%(sub)s; }
.right { text-align:right; }
.center { text-align:center; }
.deal { border:2px solid %(green)s; background:%(green_bg)s; border-radius:8px;
        padding:14px 18px; margin-top:14px; }
.deal .big { font-size:22px; font-weight:700; color:%(green)s; }
.deal table th, .deal table td { border-bottom:none; padding:4px 8px; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
details.boxes summary { cursor:pointer; font-weight:600; padding:6px 0; }
a { color:%(blue)s; }
.tag { font-size:11px; color:%(sub)s; border:1px solid %(line)s; border-radius:4px;
       padding:1px 6px; }
.photo { max-width:120px; max-height:90px; border:1px solid %(line)s; border-radius:4px; }
""" % COLORS


def shell(user, active, body, flash=None) -> bytes:
    role = user["role"]
    nav = [
        ("/accounts", "客户箱账", True),
        ("/dispatch", "发箱扫描", role in ("warehouse", "admin")),
        ("/returns", "返箱验收", True),
        ("/repair", "维修池", True),
        ("/overdue", "逾期清单", True),
        ("/ledger", "押金流水", True),
    ]
    links = "".join(
        f'<a href="{u}" class="{"on" if u == active else ""}">{n}</a>'
        for u, n, ok in nav if ok)
    flash_html = f'<div class="flash {flash[0]}">{h(flash[1])}</div>' if flash else ""
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>周转箱押金管理</title><style>{SHELL_CSS}</style></head>
<body>
<header>
  <span class="brand">🔄 周转箱押金管理</span>
  <nav>{links}</nav>
  <span class="who"><b>{h(user['name'])}</b>（{config.ROLE_NAMES.get(role, role)}）
    <a href="/logout" style="color:#cfd4d8">退出</a></span>
</header>
<div class="wrap">{flash_html}{body}</div></body></html>""".encode("utf-8")

# ---------------------------------------------------------------- 简易 multipart 解析


def parse_form(handler) -> tuple[dict[str, str], list[dict]]:
    """返回 (普通字段, 文件列表[{name,filename,data}])。兼容 urlencoded。"""
    ctype = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length) if length else b""
    files: list[dict] = []
    fields: dict[str, str] = {}
    if ctype.startswith("multipart/form-data"):
        boundary = ctype.split("boundary=", 1)[1].strip().encode()
        for part in raw.split(b"--" + boundary):
            if not part or part in (b"--", b"--\r\n", b"--\n"):
                continue
            head, _, body = part.partition(b"\r\n\r\n")
            if not _:
                continue
            body = body.rstrip(b"\r\n")
            disp = ""
            for line in head.decode("utf-8", "replace").split("\r\n"):
                if line.lower().startswith("content-disposition"):
                    disp = line
            name = filename = None
            for seg in disp.split(";"):
                seg = seg.strip()
                if seg.startswith("name="):
                    name = seg[5:].strip('"')
                elif seg.startswith("filename="):
                    filename = seg[9:].strip('"')
            if name is None:
                continue
            if filename is not None:
                if filename and body:
                    files.append({"name": name, "filename": filename, "data": body})
            else:
                fields[name] = body.decode("utf-8", "replace")
    else:
        for k, v in urllib.parse.parse_qsl(raw.decode("utf-8", "replace")):
            fields[k] = v
    return fields, files

# ---------------------------------------------------------------- 页面渲染


def page_accounts(conn) -> str:
    rows = queries.all_accounts(conn)
    trs = []
    for r in rows:
        trs.append(f"""<tr>
  <td><a href="/account?cid={h(r['cid'])}">{h(r['cid'])}</a></td>
  <td>{h(r['name'])}</td>
  <td class="num">{r['total']}</td>
  <td class="num"><b style="color:{COLORS['amber']}">{r['out']}</b></td>
  <td class="num" style="color:{COLORS['green']}">{r['stock']}</td>
  <td class="num" style="color:{COLORS['blue']}">{r['repair']}</td>
  <td class="num" style="color:{COLORS['red']}">{r['scrap']}</td>
  <td class="num">¥{money(r['balance'])}</td>
  <td><a class="btn sm" href="/account?cid={h(r['cid'])}">展开箱号</a></td>
</tr>""")
    return f"""
<div class="card"><h2>客户箱账（按客户汇总，点击展开到箱号）</h2>
<div class="body"><table>
<thead><tr><th>客户编号</th><th>客户名称</th><th class="num">累计发箱</th>
<th class="num">在外（应返）</th><th class="num">已返在库</th><th class="num">待修</th>
<th class="num">报废/核销</th><th class="num">押金余额</th><th></th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="9" class="center muted">暂无数据</td></tr>'}
</tbody></table></div></div>"""


def page_account(conn, cid: str) -> str | None:
    data = queries.customer_account(conn, cid)
    if not data:
        return None
    s = data["summary"]
    kpi = f"""
<div class="kpi" style="margin-bottom:18px">
  <div class="box amber"><div class="k">应返（在外）</div><div class="v">{s['out']}</div></div>
  <div class="box green"><div class="k">实返（在库）</div><div class="v">{s['returned']}</div></div>
  <div class="box blue"><div class="k">待修</div><div class="v">{s['repair']}</div></div>
  <div class="box red"><div class="k">报废/核销</div><div class="v">{s['scrapped']}</div></div>
  <div class="box green"><div class="k">押金余额（元）</div><div class="v">¥{money(s['balance'])}</div></div>
</div>"""
    rows = []
    for b in data["boxes"]:
        rows.append(f"""<tr>
<td><a href="/box?no={h(b['box_no'])}">{h(b['box_no'])}</a></td>
<td>{h(b['box_type'])}</td><td>{h(b['batch_id'])}</td>
<td class="num">¥{money(b['deposit'])}</td>
<td>{state_badge(b['state'])}</td>
<td>{h(b.get('return_reason') or '')}</td>
<td>{h((b['dispatched_at'] or '')[:16])}</td>
<td class="num">{'¥' + money(b['last_fee']) if b.get('last_fee') else ''}</td>
</tr>""")
    return kpi + f"""
<div class="card"><h2>{h(data['customer']['name'])}（{h(cid)}）箱号明细
<span class="tag">押金收 ¥{money(s['charged'])} ｜ 退 ¥{money(s['refunded'])} ｜
扣 ¥{money(s['deducted'])} ｜ 待审净额 ¥{money(s['pending_net'])}</span></h2>
<div class="body"><details class="boxes" open><summary>共 {s['total']} 箱</summary>
<table><thead><tr><th>箱号</th><th>箱型</th><th>出库批次</th>
<th class="num">押金</th><th>状态</th><th>验收备注</th><th>出库时间</th>
<th class="num">维修费</th></tr></thead><tbody>
{''.join(rows) or '<tr><td colspan="8" class="center muted">暂无箱号</td></tr>'}
</tbody></table></details></div></div>"""


def page_box(conn, box_no: str) -> str | None:
    b = queries.box_detail(conn, box_no)
    if not b:
        return None
    ev_rows = []
    for e in queries.box_events(conn, box_no):
        p = json.loads(e["payload"])
        ev_rows.append(f"<tr><td>{h(e['at'])}</td><td>{badge(e['event'],'blue')}</td>"
                       f"<td>{h(e['actor'])}</td><td class='muted'>{h(json.dumps(p, ensure_ascii=False))}</td></tr>")
    imgs = "".join(
        f'<a href="/photo?no={h(box_no)}&f={h(p["filename"])}" target="_blank">'
        f'<img class="photo" src="/photo?no={h(box_no)}&f={h(p["filename"])}"></a>'
        for p in photos.list_for(conn, box_no))
    owner = conn.execute("SELECT name FROM customers WHERE cid=?",
                         (b["holder_cid"],)).fetchone() if b["holder_cid"] else None
    return f"""
<div class="card"><h2>箱号 {h(box_no)} {state_badge(b['state'])}</h2>
<div class="body"><table>
<tr><th>箱型</th><td>{h(b['box_type'])}</td><th>押金</th><td class="num">¥{money(b['deposit'])}</td></tr>
<tr><th>出库批次</th><td>{h(b['batch_id'])}</td>
<th>当前持有</th><td>{h(owner['name'] if owner else '—（在库/已处置）')}</td></tr>
<tr><th>出库时间</th><td>{h(b['dispatched_at'])}</td>
<th>应返日期</th><td>{h(b['due_at'])}</td></tr></table>
{'<p style="margin-top:12px"><b>破损照片：</b><br>' + imgs + '</p>' if imgs else ''}
</div></div>
<div class="card"><h2>逐箱事件</h2><div class="body"><table>
<thead><tr><th>时间</th><th>事件</th><th>操作人</th><th>明细</th></tr></thead>
<tbody>{''.join(ev_rows)}</tbody></table></div></div>"""


def _customer_options(conn, cid=None):
    return "".join(f'<option value="{h(c["cid"])}" {"selected" if c["cid"]==cid else ""}>'
                   f'{h(c["cid"])} {h(c["name"])}</option>'
                   for c in queries.customers(conn))


def _batch_options(conn, bid=None):
    return "".join(f'<option value="{h(b["bid"])}" {"selected" if b["bid"]==bid else ""}>'
                   f'{h(b["bid"])} {h(b["note"])}</option>'
                   for b in queries.batches(conn))


def page_dispatch(conn, user) -> str:
    if user["role"] not in ("warehouse", "admin"):
        return '<div class="flash err">仅仓库可执行发箱扫描</div>'
    return f"""
<div class="card"><h2>发箱扫描</h2><div class="body">
<form method="post" action="/dispatch">
<table style="width:auto">
<tr><th>箱号</th><td><input name="box_no" required placeholder="如 T-A-0001" autofocus></td></tr>
<tr><th>箱型</th><td><select name="box_type">{
''.join(f'<option value="{h(t)}">{h(t)}（押金 ¥{money(d)}）</option>'
        for t, d in config.DEPOSIT_BY_TYPE.items())}</select></td></tr>
<tr><th>出库批次</th><td><select name="batch_id">{_batch_options(conn)}</select></td></tr>
<tr><th>分配客户</th><td><select name="cid">{_customer_options(conn)}</select></td></tr>
<tr><th>返还期限(天)</th><td><input name="due_days" type="number" value="{config.OVERDUE_DAYS}"></td></tr>
</table><p><button class="green">确认发箱并记账押金</button></p>
</form></div></div>"""


def page_returns(conn, user, form=None) -> str:
    form = form or {}
    board = queries.return_board(conn)
    brows = "".join(
        f"""<tr><td>{h(r['cid'])}</td><td>{h(r['name'])}</td>
<td class="num"><b style="color:{COLORS['amber']}">{r['due']}</b></td>
<td class="num" style="color:{COLORS['green']}"><b>{r['back']}</b></td>
<td class="num" style="color:{COLORS['blue']}">{r['repair']}</td>
<td class="num" style="color:{COLORS['red']}">{r['scrap']}</td></tr>"""
        for r in board)

    recent = conn.execute(
        """SELECT rt.*, c.name FROM returns rt
           LEFT JOIN customers c ON c.cid=rt.actual_cid
           ORDER BY rt.id DESC LIMIT 8""").fetchall()
    dec_badge = {"ACCEPT": ("合格入库", "green"), "REPAIR": ("送修", "blue"),
                 "SCRAP": ("报废", "red"), "REJECT": ("拒收", "red")}
    rrows = ""
    for r in recent:
        txt, col = dec_badge.get(r["decision"], (r["decision"], "sub"))
        cross = badge("跨批次", "amber") if r["cross_batch"] else ""
        rrows += (f"<tr><td>{h(r['box_no'])}</td><td>{h(r['name'] or '')}</td>"
                  f"<td>{badge(txt, col)} {cross}</td>"
                  f"<td>{'✔' if r['body_ok'] else '✘箱体'} "
                  f"{'✔' if r['lid_ok'] else '✘封盖'} "
                  f"{'✔' if r['label_ok'] else '✘标签'}</td>"
                  f"<td class='num'>¥{money(r['repair_fee'])}</td>"
                  f"<td class='muted'>{h(r['reason'])}</td></tr>")

    can = user["role"] in ("warehouse", "admin")
    scan = ""
    if can:
        cid_sel = form.get("cid", "")
        batch_sel = form.get("return_batch_id", "")
        scan = f"""
<div class="card"><h2>返箱扫描与验收</h2><div class="body">
<form method="post" action="/returns/accept" enctype="multipart/form-data" id="rf">
<div class="grid2">
  <div><b>返还客户</b><br><select name="cid" id="f_cid">{_customer_options(conn, cid_sel)}</select></div>
  <div><b>返还批次（混批时与箱号出库批次不同）</b><br>
    <select name="return_batch_id" id="f_batch">
      <option value="">（不校验批次）</option>{_batch_options(conn, batch_sel)}</select></div>
</div>
<p style="margin-top:12px"><b>扫描箱号</b>
  <input name="box_no" id="f_box" required style="width:220px" placeholder="扫描/输入箱号" value="{h(form.get('box_no',''))}">
  <button type="button" class="ghost" onclick="preview()">预检归属/批次</button>
  <span id="pv"></span></p>
<p><b>三项质检：</b>
  <label class="chk"><input type="checkbox" name="body_ok" value="1" checked>箱体合格</label>
  <label class="chk"><input type="checkbox" name="lid_ok" value="1" checked>封盖合格</label>
  <label class="chk"><input type="checkbox" name="label_ok" value="1" checked>标签合格</label></p>
<div class="grid2">
  <div><b>预估维修费（元，超 ¥{config.REPAIR_FEE_CAP:.0f} 判报废）</b><br>
    <input name="repair_fee" type="number" step="0.01" min="0" value="0"></div>
  <div><b>破损照片（本地保存）</b><br><input type="file" name="photo" accept="image/*"></div>
</div>
<p><b>故障描述</b><br><input name="fault" style="width:100%"></p>
<p><label class="chk" style="color:{COLORS['red']}"><input type="checkbox" name="scrap" value="1">
严重破损无修复价值，直接报废（押金全额折抵）</label></p>
<div id="cross_confirm" style="display:none" class="flash warn">
  <label class="chk"><input type="checkbox" name="allow_cross_batch" value="1">
  该箱为<b>跨批次返还</b>，我确认按实际出库批次入账（仅主管可确认）</label></div>
<p><button class="green">提交验收并结算押金</button></p>
</form>
<script>
async function preview() {{
  const box = document.getElementById('f_box').value.trim();
  const cid = document.getElementById('f_cid').value;
  const bid = document.getElementById('f_batch').value;
  if (!box) return;
  const r = await fetch('/returns/preview', {{method:'POST',
    headers:{{'Content-Type':'application/x-www-form-urlencoded'}},
    body:'box_no='+encodeURIComponent(box)+'&cid='+encodeURIComponent(cid)+
         '&return_batch_id='+encodeURIComponent(bid)}});
  const d = await r.json();
  const el = document.getElementById('pv');
  if (!r.ok) {{ el.innerHTML='<span class="badge" style="background:{COLORS['red_bg']};color:{COLORS['red']}">'+d.error+'</span>';
    document.getElementById('cross_confirm').style.display='none'; return; }}
  let html = '<span class="badge">归属正确 · 押金 ¥'+d.deposit.toFixed(2)+' · 出库批次 '+d.batch_id+'</span>';
  if (d.cross_batch) {{
    document.getElementById('cross_confirm').style.display='block';
    html += ' <span class="badge" style="background:{COLORS['amber_bg']};color:{COLORS['amber']}">跨批次，需主管确认</span>';
  }}
  el.innerHTML = html;
}}
</script></div></div>"""
    else:
        scan = '<div class="flash warn">仓库/主管角色可执行返箱验收</div>'

    return f"""
<div class="card"><h2>返箱看板（应返 / 实返 / 待修 同屏）</h2><div class="body">
<table><thead><tr><th>客户</th><th>名称</th><th class="num">应返</th>
<th class="num">实返在库</th><th class="num">待修</th><th class="num">报废/核销</th>
</tr></thead><tbody>{brows}</tbody></table></div></div>
{scan}
<div class="card"><h2>最近验收记录（折抵金额见押金流水）</h2><div class="body">
<table><thead><tr><th>箱号</th><th>归属客户</th><th>验收结论</th>
<th>箱体/封盖/标签</th><th class="num">维修费</th><th>备注</th></tr></thead>
<tbody>{rrows or '<tr><td colspan="6" class="center muted">暂无</td></tr>'}</tbody></table>
</div></div>"""


def deal_panel(result: dict) -> str:
    """折抵金额紧挨验收结论。"""
    dec = {"ACCEPT": ("合格入库 · 全额退押金", "green"),
           "REPAIR": ("送修 · 维修费折抵押金", "blue"),
           "SCRAP": ("报废 · 押金全额折抵", "red")}[result["decision"]]
    return f"""
<div class="deal"><b>验收结论：</b>{badge(dec[0], dec[1])}
<table class="num" style="margin-top:8px">
<tr><th class="num">箱号押金</th><td class="num">¥{money(result['deposit'])}</td></tr>
<tr><th class="num">维修费折抵</th><td class="num">− ¥{money(result['deduct'])}</td></tr>
<tr><th class="num">本次退还</th><td class="num">+ ¥{money(result['refund'])}</td></tr>
<tr><th class="num">客户净退押金</th><td class="num"><span class="big">¥{money(result['net_refund'])}</span></td></tr>
</table><p class="muted" style="margin-bottom:0">{h(result['reason'])}
｜退还/扣减流水状态为「待审」，需财务在押金流水页复核后生效</p></div>"""


def page_repair(conn, user) -> str:
    rows = queries.repair_pool(conn)
    can = user["role"] in ("warehouse", "admin")
    trs = []
    for r in rows:
        photo_n = len(json.loads(r["photo_ids"] or "[]"))
        trs.append(f"""<tr>
<td><a href="/box?no={h(r['box_no'])}">{h(r['box_no'])}</a></td>
<td>{h(r['box_type'])}</td><td>{h(r['holder_name'] or '—')}</td>
<td class="num">¥{money(r['deposit'])}</td>
<td class="num">¥{money(r['fee'])}</td><td>{h(r['fault'])}</td>
<td>{photo_n} 张</td><td>{h(r['opened_at'][:16])}</td>
<td>{('''<form class="inline" method="post" action="/repair/finish">
  <input type="hidden" name="box_no" value="'''+h(r['box_no'])+'''">
  <input name="actual_fee" type="number" step="0.01" value="'''+money(r['fee'])+'''"
    style="width:80px">
  <button name="outcome" value="repaired" class="green sm">修完入库并退余额</button>
  <button name="outcome" value="scrapped" class="sm">报废</button></form>''') if can else badge('待仓库处理','blue')}</td>
</tr>""")
    return f"""
<div class="card"><h2>维修池（破损箱送修，修完重新入库；费用结清押金尾款）</h2>
<div class="body"><table><thead><tr><th>箱号</th><th>箱型</th><th>原归属</th>
<th class="num">押金</th><th class="num">预估费</th><th>故障</th><th>照片</th>
<th>入池时间</th><th>处置</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="9" class="center muted">维修池为空</td></tr>'}
</tbody></table></div></div>"""


def page_overdue(conn, user) -> str:
    rows = queries.overdue_boxes(conn)
    can = user["role"] in ("finance", "admin")
    trs = []
    for r in rows:
        trs.append(f"""<tr>
<td><a href="/box?no={h(r['box_no'])}">{h(r['box_no'])}</a></td>
<td>{h(r['holder_name'] or '—')}</td><td>{h(r['batch_id'])}</td>
<td>{h(r['due_at'][:16])}</td>
<td class="num"><b style="color:{COLORS['red']}">{r['overdue_days']} 天</b></td>
<td class="num">¥{money(r['deposit'])}</td>
<td>{('''<form class="inline" method="post" action="/overdue/loss">
  <input type="hidden" name="box_no" value="'''+h(r['box_no'])+'''">
  <input name="note" placeholder="核销说明" style="width:180px">
  <button class="sm">长期未返·押金扣抵核销</button></form>''') if can else ''}</td>
</tr>""")
    return f"""
<div class="card"><h2>逾期清单（超过应返日 {config.OVERDUE_DAYS} 天未返还）</h2>
<div class="body"><table><thead><tr><th>箱号</th><th>持有客户</th><th>出库批次</th>
<th>应返日期</th><th class="num">逾期</th><th class="num">押金</th><th>处置</th>
</tr></thead><tbody>{''.join(trs) or '<tr><td colspan="7" class="center muted">无逾期箱 🎉</td></tr>'}
</tbody></table>{'<p class="muted">核销由财务/主管执行。</p>' if rows and not can else ''}
</div></div>"""


def page_ledger(conn, user, status_filter: str = "") -> str:
    fin = user["role"] in ("finance", "admin")
    rows = queries.ledger(conn, status=status_filter or None)
    type_cn = {"CHARGE": "收取押金", "REFUND": "退还押金",
               "DEDUCT_REPAIR": "维修费扣抵", "DEDUCT_LOSS": "损失扣抵"}
    st_badge = {"confirmed": ("已确认", "green"), "pending": ("待审", "amber"),
                "held": ("冻结", "red"), "rejected": ("已驳回", "red")}
    trs = []
    for r in rows:
        tcn = type_cn.get(r["tx_type"], r["tx_type"])
        st = st_badge.get(r["status"], (r["status"], "sub"))
        sign = "+" if r["tx_type"] == "CHARGE" else "−"
        actions = ""
        if fin and r["status"] == "pending":
            actions = (f'<form class="inline" method="post" action="/ledger/review">'
                       f'<input type="hidden" name="id" value="{r["id"]}">'
                       f'<button name="approve" value="1" class="green sm">通过</button> '
                       f'<input name="note" placeholder="驳回原因(可选)" style="width:130px">'
                       f' <button name="approve" value="0" class="sm">驳回挂争议</button></form>')
        trs.append(f"""<tr>
<td>{r['id']}</td><td><a href="/box?no={h(r['box_no'])}">{h(r['box_no'])}</a></td>
<td>{h(r['cid'])}</td><td>{h(r['batch_id'] or '')}</td><td>{h(tcn)}</td>
<td class="num">{sign} ¥{money(r['amount'])}</td><td>{badge(st[0], st[1])}</td>
<td class="muted" style="max-width:280px">{h(r['reason'])}</td>
<td>{h(r['receipt_no'] or '')}</td><td>{h(r['created_by'])}</td>
<td>{h(r['reviewed_by'] or '—')}</td><td>{actions}</td>
</tr>""")
    disp = queries.disputes(conn)
    drows = "".join(
        f"<tr><td>{d['id']}</td><td><a href='/box?no={h(d['box_no'])}'>{h(d['box_no'])}</a></td>"
        f"<td>{h(d['cid'])}</td><td class='muted'>{h(d['note'])}</td>"
        f"<td>{h(d['opened_by'])}</td><td>{h(d['opened_at'][:16])}</td></tr>"
        for d in disp)
    return f"""
<div class="card"><h2>押金流水（财务可复核折抵；仓库只读实物无关金额）
  <form class="inline" style="margin-left:auto">
    <select name="status" onchange="this.form.submit()">
      <option value="">全部状态</option>
      <option value="pending" {'selected' if status_filter=='pending' else ''}>待审</option>
      <option value="confirmed" {'selected' if status_filter=='confirmed' else ''}>已确认</option>
      <option value="rejected" {'selected' if status_filter=='rejected' else ''}>已驳回</option>
    </select></form></h2>
<div class="body"><table><thead><tr><th>#</th><th>箱号</th><th>客户</th>
<th>批次</th><th>类型</th><th class="num">金额</th><th>状态</th><th>事由</th>
<th>回执号</th><th>制单</th><th>复核人</th><th>复核</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="12" class="center muted">暂无流水</td></tr>'}
</tbody></table></div></div>
<div class="card"><h2>未决押金争议（冻结该箱抵扣）</h2><div class="body">
<table><thead><tr><th>#</th><th>箱号</th><th>客户</th><th>说明</th>
<th>提出人</th><th>时间</th></tr></thead>
<tbody>{drows or '<tr><td colspan="6" class="center muted">无未决争议</td></tr>'}
</tbody></table></div></div>"""

# ---------------------------------------------------------------- Handler


class App:
    def __init__(self):
        config.ensure_dirs()
        init_db(connect(config.DB_PATH))

    def handler(self):
        app = self

        class Handler(__import__("http.server", fromlist=["BaseHTTPRequestHandler"])
                      .BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, body: bytes, ctype="text/html; charset=utf-8",
                      status=200, extra=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if extra:
                    for k, v in extra:
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self, path, cookie=None):
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", path)
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _render(self, content, user, flash=None, status=200):
                self._send(shell(user, self.path.split("?")[0], content, flash),
                           status=status)

            def _err(self, msg, user, back="/returns", status=400):
                content = (f'<div class="flash err">{h(msg)}</div>'
                           f'<p><a class="btn" href="{back}">返回</a></p>')
                self._render(content, user, status=status)

            # ---- GET ----
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
                user = get_user(self)
                if path == "/login":
                    if user:
                        return self._redirect("/accounts")
                    return self._send(LOGIN_HTML)
                if path == "/logout":
                    cookie = SimpleCookie()
                    cookie["tf_session"] = ""
                    cookie["tf_session"]["max-age"] = 0
                    cookie["tf_session"]["path"] = "/"
                    self._redirect("/login", cookie.output(header="").strip())
                    return
                if not user:
                    return self._redirect("/login")

                conn = connect(config.DB_PATH)
                try:
                    if path == "/":
                        return self._redirect("/accounts")
                    if path == "/accounts":
                        return self._render(page_accounts(conn), user)
                    if path == "/account":
                        content = page_account(conn, qs.get("cid", [""])[0])
                        return (self._render(content, user) if content
                                else self._err("客户不存在", user, "/accounts", 404))
                    if path == "/box":
                        content = page_box(conn, qs.get("no", [""])[0])
                        return (self._render(content, user) if content
                                else self._err("箱号不存在", user, "/accounts", 404))
                    if path == "/dispatch":
                        return self._render(page_dispatch(conn, user), user)
                    if path == "/returns":
                        return self._render(page_returns(conn, user), user)
                    if path == "/repair":
                        return self._render(page_repair(conn, user), user)
                    if path == "/overdue":
                        return self._render(page_overdue(conn, user), user)
                    if path == "/ledger":
                        return self._render(
                            page_ledger(conn, user, qs.get("status", [""])[0]), user)
                    if path == "/photo":
                        no, fn = qs.get("no", [""])[0], qs.get("f", [""])[0]
                        p = photos.path_for(no, fn)
                        if not p.is_file() or not str(p.resolve()).startswith(
                                str(config.PHOTO_DIR.resolve())):
                            return self._send(b"not found", status=404,
                                              ctype="text/plain")
                        ctype = "image/png" if fn.endswith(".png") else "image/jpeg"
                        return self._send(p.read_bytes(), ctype=ctype)
                    return self._send(b"not found", status=404, ctype="text/plain")
                finally:
                    conn.close()

            # ---- POST ----
            def do_POST(self):
                path = urllib.parse.urlparse(self.path).path
                if path == "/login":
                    form, _ = parse_form(self)
                    rec = config.ACCOUNTS.get(form.get("user", "").strip())
                    if not rec or rec[0] != form.get("pwd", ""):
                        body = LOGIN_HTML.replace(
                            b"</form>",
                            '<div class="flash err">账号或密码错误</div></form>'.encode())
                        return self._send(body, status=401)
                    token = make_session(
                        {"login": form["user"].strip(), "role": rec[1],
                         "name": rec[2]})
                    self._redirect(
                        "/accounts",
                        f"tf_session={token}; Path=/; HttpOnly")
                    return
                user = get_user(self)
                if not user:
                    return self._redirect("/login")
                form, files = parse_form(self)
                conn = connect(config.DB_PATH)
                try:
                    actor = {"name": user["name"], "role": user["role"]}
                    try:
                        if path == "/dispatch":
                            services.dispatch(
                                conn, box_no=form["box_no"].strip().upper(),
                                box_type=form["box_type"],
                                batch_id=form["batch_id"], cid=form["cid"],
                                actor=actor, due_days=int(form.get("due_days") or 30))
                            return self._redirect("/returns?m=ok")

                        if path == "/returns/preview":
                            try:
                                pre = services.preview_return(
                                    conn, box_no=form["box_no"].strip().upper(),
                                    cid=form["cid"],
                                    return_batch_id=form.get("return_batch_id") or None)
                                self._send(json.dumps(
                                    {"ok": True, "deposit": pre["deposit"],
                                     "batch_id": pre["batch_id"],
                                     "cross_batch": pre["cross_batch"]},
                                    ensure_ascii=False).encode(),
                                    ctype="application/json")
                            except ToteError as e:
                                self._send(json.dumps(
                                    {"ok": False, "error": str(e)},
                                    ensure_ascii=False).encode(),
                                    ctype="application/json", status=e.status)
                            return

                        if path == "/returns/accept":
                            photo_ids = []
                            for f in files:
                                pid, _ = photos.save(
                                    form["box_no"].strip().upper(), "damage",
                                    f["filename"], f["data"])
                                photo_ids.append(pid)
                            result = services.accept_return(
                                conn,
                                box_no=form["box_no"].strip().upper(),
                                cid=form["cid"],
                                return_batch_id=form.get("return_batch_id") or None,
                                body_ok="body_ok" in form,
                                lid_ok="lid_ok" in form,
                                label_ok="label_ok" in form,
                                actor=actor,
                                allow_cross_batch="allow_cross_batch" in form,
                                repair_fee=float(form.get("repair_fee") or 0),
                                scrap="scrap" in form,
                                fault=form.get("fault", ""),
                                photo_ids=photo_ids)
                            content = (deal_panel(result) +
                                       '<p><a class="btn" href="/returns">继续扫描</a> '
                                       '<a class="btn ghost" href="/ledger">查看押金流水</a></p>')
                            return self._render(content, user)

                        if path == "/repair/finish":
                            services.finish_repair(
                                conn, box_no=form["box_no"],
                                outcome=form["outcome"],
                                actual_fee=float(form.get("actual_fee") or 0),
                                actor=actor)
                            return self._redirect("/repair")

                        if path == "/overdue/loss":
                            services.declare_loss(
                                conn, box_no=form["box_no"], actor=actor,
                                note=form.get("note", ""))
                            return self._redirect("/ledger?status=pending")

                        if path == "/ledger/review":
                            services.review_ledger(
                                conn, ledger_id=int(form["id"]),
                                approve=form.get("approve") == "1",
                                actor=actor, note=form.get("note", ""))
                            return self._redirect("/ledger?status=pending")

                        return self._send(b"not found", status=404,
                                          ctype="text/plain")
                    except ToteError as e:
                        back = {"/dispatch": "/dispatch",
                                "/repair/finish": "/repair",
                                "/overdue/loss": "/overdue",
                                "/ledger/review": "/ledger"}.get(path, "/returns")
                        return self._err(str(e), user, back, status=e.status)
                finally:
                    conn.close()

        return Handler


LOGIN_HTML = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>登录 · 周转箱押金管理</title><style>{SHELL_CSS}</style></head>
<body style="background:{COLORS['bg']}">
<div style="max-width:380px;margin:90px auto">
<div class="card"><h2>🔄 周转箱押金管理</h2><div class="body">
<form method="post" action="/login">
<p><b>账号</b><br><input name="user" required style="width:100%" autofocus></p>
<p><b>密码</b><br><input name="pwd" type="password" required style="width:100%"></p>
<p><button class="green" style="width:100%">登录</button></p>
<p class="muted" style="font-size:12.5px">演示账号：<br>
仓库 wh / wh123 ｜ 财务 fin / fin123 ｜ 主管 admin / admin123</p>
</form></div></div></div></body></html>""".encode("utf-8")
