# -*- coding: utf-8 -*-
"""责任机制 HTTP 服务（端口 8020）：对外接口 + 钉钉在线确认页。

接口一览：
  GET  /                          责任台账工作台（HTML）
  GET  /api/acc/health            健康检查（含数据库与钉钉通道模式）
  GET  /api/acc/owners            风险分级 × 责任人映射
  POST /api/acc/owners            维护调整责任人映射
  POST /api/acc/submit            提交事项并自动分派
  POST /api/acc/review            step=begin|complete 核实流程
  POST /api/acc/confirm           在线确认/驳回（携带 confirm_id + token）
  POST /api/acc/urge              人工催办
  POST /api/acc/sweep             超时扫描（自动催办/升级）
  GET  /api/acc/ledger            责任台账查询
  GET  /api/acc/history           某事项完整责任链
  GET  /dingtalk/confirm          钉钉确认入口页（接收人点击后在线确认或驳回）
  POST /api/acc/confirm/submit    确认页表单提交

统一返回：{"ok":bool,"code":int,"data":...,"error":{...},"meta":{...}}

【待替换项】
  CF_CONFIRM_BASE      确认入口公网基址（钉钉里点开的链接必须是外网可达地址）
  CF_CONFIRM_SECRET    确认令牌签名密钥（必须替换）
  DINGTALK_*           钉钉凭据（见 v8/dingtalk.py）
  CF_ACC_PORT          服务端口，默认 8020
"""
from __future__ import annotations

import json
import os
import sys
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SRC = os.path.dirname(os.path.abspath(__file__))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from v8 import workflow as WF        # noqa: E402
from v8 import matter as MT          # noqa: E402
from v8 import dingtalk as DT        # noqa: E402
from v8 import db as DB              # noqa: E402

PORT = int(os.environ.get("CF_ACC_PORT", "8020"))
STATE_CN = MT.STATE_CN
LEVEL_LAMP = {"HIGH": "#e5484d", "MID": "#f5a524", "LOW": "#30a46c"}


def _wrap(ok, data=None, code=0, error=None, meta=None):
    return {"ok": ok, "code": code, "data": data,
            "error": {"code": (error or {}).get("code"),
                      "message": (error or {}).get("message"),
                      "detail": (error or {}).get("detail")} if error else None,
            "meta": meta or {"ts": datetime.datetime.now().isoformat(timespec="seconds")}}


def _fail(code, message, detail=None):
    return _wrap(False, None, code, {"code": code, "message": message, "detail": detail})


# =============================================================================
# 页面
# =============================================================================
PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>责任机制工作台</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#0f1115;color:#e6e8ee}
a{color:#7bb0ff}.wrap{max-width:1180px;margin:0 auto;padding:22px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#98a2b3;font-size:13px;margin-bottom:18px}
.card{background:#171a21;border:1px solid #262b36;border-radius:10px;padding:16px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid #262b36;text-align:left}
th{color:#98a2b3;font-weight:600}
.badge{padding:2px 8px;border-radius:10px;font-size:12px;color:#fff}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:14px}
.kv{background:#11141a;border:1px solid #262b36;border-radius:8px;padding:10px}
.kv .n{font-size:22px;font-weight:700}.kv .l{color:#98a2b3;font-size:12px}
button,input,select,textarea{background:#11141a;color:#e6e8ee;border:1px solid #333a47;border-radius:6px;padding:7px 10px;font-size:13px}
button{cursor:pointer;background:#2a303d}button:hover{background:#333b4a}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.mono{font-family:Consolas,monospace;font-size:12px}
.muted{color:#98a2b3}
</style></head><body><div class="wrap">
<h1>责任机制工作台 · 风险分级 → 责任人 → 核实 → 钉钉确认</h1>
<div class="sub">事项提交后按风险等级自动分派核实负责人，核实完成发钉钉确认书，接收人在线确认或驳回（驳回自动改派替补人重新流转），超时自动催办、超限自动升级。</div>
<div class="grid" id="stats"></div>
<div class="card"><h2>责任台账</h2>
<div class="row">
<select id="f_state"><option value="">全部状态</option></select>
<select id="f_level"><option value="">全部等级</option><option>HIGH</option><option>MID</option><option>LOW</option></select>
<input id="f_owner" placeholder="责任人" style="width:140px">
<button onclick="load()">刷新</button>
<button onclick="sweep()">执行超时扫描</button>
</div>
<table><thead><tr><th>事项</th><th>等级</th><th>状态</th><th>轮次</th><th>当前责任人</th><th>核实截止</th><th>确认截止</th><th>催办</th><th>操作</th></tr></thead>
<tbody id="tb"></tbody></table></div>
<div class="card"><h2>提交新事项</h2>
<div class="row">
<input id="t_title" placeholder="事项标题" style="flex:1;min-width:240px">
<select id="t_lamp"><option value="红灯">红灯</option><option value="黄灯" selected>黄灯</option><option value="绿灯">绿灯</option></select>
<input id="t_amount" placeholder="金额" style="width:110px">
<button onclick="submitMatter()">提交并自动分派</button></div></div>
<div class="card"><h2>责任人映射（可维护调整）</h2>
<table><thead><tr><th>等级</th><th>核实负责人</th><th>替补人</th><th>升级对象</th><th>核实时限</th><th>确认时限</th><th>催办间隔</th><th>最多催办</th></tr></thead>
<tbody id="ow"></tbody></table></div>
</div>
<script>
const CN={SUBMITTED:'已提交',ASSIGNED:'已分派',IN_REVIEW:'核实中',PENDING_CONFIRM:'待确认',URGING:'催办中',ESCALATED:'已升级',CONFIRMED:'已确认',REJECTED:'已驳回',CLOSED:'已关闭'};
const LC={HIGH:'#e5484d',MID:'#f5a524',LOW:'#30a46c'};
const api=(p,o)=>fetch(p,o).then(r=>r.json());
async function load(){
  const q=new URLSearchParams();
  if(f_state.value)q.set('state',f_state.value);
  if(f_level.value)q.set('risk_level',f_level.value);
  if(f_owner.value)q.set('owner',f_owner.value);
  const st=await api('/api/acc/ledger?'+q);
  const s=await api('/api/acc/stats');
  const d=s.data||{};
  let g='';
  for(const k of ['total','overdue'])g+=`<div class="kv"><div class="n">${d[k]??0}</div><div class="l">${k==='total'?'事项总数':'当前超时'}</div></div>`;
  for(const [k,v] of Object.entries(d.by_state||{}))g+=`<div class="kv"><div class="n">${v}</div><div class="l">${CN[k]||k}</div></div>`;
  stats.innerHTML=g;
  tb.innerHTML=(st.data&&st.data.items||[]).map(m=>`<tr>
    <td>${m.title||''}<div class="mono muted">${m.matter_id}</div></td>
    <td><span class="badge" style="background:${LC[m.risk_level]}">${m.risk_level_name||m.risk_level}</span></td>
    <td>${CN[m.state]||m.state}${m.overdue?' <span class="badge" style="background:#e5484d">超时'+m.overdue_hours+'h</span>':''}</td>
    <td>${m.round}</td><td>${m.current_owner||''}<div class="muted">${m.owner_role||''}</div></td>
    <td class="mono">${(m.due_at||'').slice(0,16)}</td><td class="mono">${(m.confirm_due_at||'').slice(0,16)}</td>
    <td>${m.urge_count||0}</td>
    <td><button onclick="urge('${m.matter_id}')">催办</button></td></tr>`).join('');
  const om=await api('/api/acc/owners');
  ow.innerHTML=Object.values((om.data||{}).matrix||{}).map(o=>`<tr>
    <td><span class="badge" style="background:${LC[o.level_code]}">${o.level_name}</span></td>
    <td>${o.primary_owner}</td><td>${o.deputy_owner}</td><td>${o.escalate_to}</td>
    <td>${o.review_sla_hours}h</td><td>${o.confirm_sla_hours}h</td><td>${o.urge_interval_hours}h</td><td>${o.max_urge_times}</td></tr>`).join('');
}
async function submitMatter(){
  const r=await api('/api/acc/submit',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:t_title.value,description:'前端提交',lamp:t_lamp.value,amount:Number(t_amount.value||0),created_by:'前端用户'})});
  alert(r.ok?('已提交并分派给：'+r.data.assigned_to.owner):('失败：'+JSON.stringify(r.error)));
  load();
}
async function urge(id){const r=await api('/api/acc/urge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({matter_id:id})});alert(r.ok?'已催办 '+r.data.urge_count+' 次':JSON.stringify(r.error));load();}
async function sweep(){const r=await api('/api/acc/sweep',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});alert(r.ok?('扫描完成：催办'+r.data.urged.length+'，升级'+r.data.escalated.length):JSON.stringify(r.error));load();}
for(const s of ['SUBMITTED','ASSIGNED','IN_REVIEW','PENDING_CONFIRM','URGING','ESCALATED','CONFIRMED','REJECTED','CLOSED'])
  f_state.insertAdjacentHTML('beforeend',`<option value="${s}">${CN[s]}</option>`);
load();
</script></body></html>"""

CONFIRM_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>风险事项确认书</title>
<style>
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,"Microsoft YaHei",sans-serif;background:#0f1115;color:#e6e8ee}
.wrap{max-width:720px;margin:0 auto;padding:26px}
.card{background:#171a21;border:1px solid #262b36;border-radius:10px;padding:18px;margin-bottom:14px}
h1{font-size:18px;margin:0 0 6px}.row{margin:8px 0;font-size:14px}
.k{color:#98a2b3;display:inline-block;width:96px}
.badge{padding:2px 10px;border-radius:10px;color:#fff;font-size:13px}
button{padding:9px 18px;border-radius:6px;border:1px solid #333a47;background:#2a303d;color:#e6e8ee;cursor:pointer;font-size:14px}
button.ok{background:#1f7a4d}button.no{background:#8f2c30}
textarea{width:100%;height:70px;background:#11141a;color:#e6e8ee;border:1px solid #333a47;border-radius:6px;padding:8px}
.mono{font-family:Consolas,monospace;font-size:12px;color:#98a2b3}
</style></head><body><div class="wrap"><div id="app"></div></div>
<script>
const q=new URLSearchParams(location.search);
const cid=q.get('confirm_id'),tk=q.get('token');
const LC={红灯:'#e5484d',黄灯:'#f5a524',绿灯:'#30a46c'};
fetch('/api/acc/confirm/view?confirm_id='+cid+'&token='+tk).then(r=>r.json()).then(r=>{
  if(!r.ok){app.innerHTML=`<div class="card"><h1>无法处理</h1><div class="row">${r.error.message}</div></div>`;return;}
  const d=r.data,c=d.document||{},rv=c.review||{};
  app.innerHTML=`<div class="card">
    <h1>${c.title||'风险事项确认书'}</h1>
    <div class="row"><span class="k">风险等级</span><span class="badge" style="background:${LC[c.lamp]||'#888'}">${c.risk_level_name}（${c.lamp}）</span></div>
    <div class="row"><span class="k">事项编号</span>${c.matter_id}（第 ${c.round} 轮）</div>
    <div class="row"><span class="k">事项描述</span>${c.description||''}</div>
    <div class="row"><span class="k">涉及金额</span>${c.amount??'-'} 元</div>
    <div class="row"><span class="k">当前责任人</span>${c.owner||''}</div>
    <div class="row"><span class="k">接收人</span>${(c.recipients||[]).join('、')}</div>
  </div>
  <div class="card">
    <h1 style="font-size:15px">核实结论</h1>
    <div class="row"><span class="k">核实人</span>${rv.reviewer||''}</div>
    <div class="row"><span class="k">核实时间</span>${rv.review_at||''}</div>
    <div class="row"><span class="k">核实结论</span><b>${rv.conclusion||''}</b></div>
    <div class="row"><span class="k">核实意见</span>${rv.opinion||'（无）'}</div>
  </div>
  <div class="card">
    <div class="row" style="margin-bottom:10px">确认意见（选填）</div>
    <textarea id="cmt" placeholder="请填写确认或驳回意见"></textarea>
    <div class="row" style="margin-top:12px;display:flex;gap:10px">
      <button class="ok" onclick="act('confirm')">确认</button>
      <button class="no" onclick="act('reject')">驳回</button>
    </div>
    <div class="mono" style="margin-top:10px">状态：${d.status}</div>
  </div>`;
});
function act(a){
  fetch('/api/acc/confirm',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({confirm_id:cid,token:tk,action:a,actor:'在线确认人',comment:(document.getElementById('cmt')||{}).value||''})
  }).then(r=>r.json()).then(r=>{
    alert(r.ok?('已'+(a==='confirm'?'确认':'驳回')+(a==='reject'?('，第'+r.data.round+'轮改派：'+r.data.reassigned_to):'')):(r.error.message));
    location.reload();
  });
}
</script></body></html>"""


# =============================================================================
# 处理器
# =============================================================================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        sys.stderr.write("[acc] %s\n" % (a[0] % a[1:]))
        sys.stderr.flush()

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:
            return {"__bad__": str(e)}

    # ---------------- GET ----------------
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        one = lambda k: (q.get(k) or [""])[0]

        if p in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if p == "/dingtalk/confirm":
            return self._send(200, CONFIRM_PAGE, "text/html; charset=utf-8")
        if p == "/api/acc/health":
            return self._json(_wrap(True, {"service": "accountability", "port": PORT,
                                           "db": DB.db_status(),
                                           "dingtalk": WF.dingtalk_status()}))
        if p == "/api/acc/ledger":
            return self._json(_wrap(True, WF.ledger(
                state=one("state") or None, risk_level=one("risk_level") or None,
                owner=one("owner") or None, keyword=one("keyword") or None,
                limit=int(one("limit") or 50))))
        if p == "/api/acc/stats":
            return self._json(_wrap(True, WF.stats()))
        if p == "/api/acc/history":
            mid = one("matter_id")
            if not mid:
                return self._json(_fail(4001, "缺少 matter_id"))
            return self._json(_wrap(True, WF.history(mid)))
        if p == "/api/acc/owners":
            return self._json(_wrap(True, WF.owner_matrix()))
        if p == "/api/acc/confirm/view":
            cid, tk = one("confirm_id"), one("token")
            cf = DT.load_confirm(cid)
            if not cf:
                return self._json(_fail(4001, "确认书不存在：%s" % cid))
            if not DT.verify_token(cid, tk):
                return self._json(_fail(4003, "令牌校验失败，确认链接无效"))
            return self._json(_wrap(True, cf))
        return self._json(_fail(4002, "未知接口：%s" % p), 404)

    # ---------------- POST ----------------
    def do_POST(self):
        p = urlparse(self.path).path
        b = self._body()
        if b.get("__bad__"):
            return self._json(_fail(4000, "请求体不是合法 JSON", b))
        try:
            if p == "/api/acc/submit":
                if not b.get("title"):
                    return self._json(_fail(4001, "缺少 title"))
                return self._json(_wrap(True, WF.submit_matter(
                    title=b["title"], description=b.get("description"),
                    lamp=b.get("lamp"), risk_level=b.get("risk_level"),
                    source=b.get("source", "http"), source_ref=b.get("source_ref"),
                    industry=b.get("industry"), amount=b.get("amount"),
                    created_by=b.get("created_by", "http"))))
            if p == "/api/acc/review":
                if not b.get("matter_id"):
                    return self._json(_fail(4001, "缺少 matter_id"))
                if b.get("step") == "complete":
                    return self._json(_wrap(True, WF.complete_review(
                        b["matter_id"], reviewer=b.get("reviewer"), opinion=b.get("opinion"),
                        conclusion=b.get("conclusion", "属实"),
                        next_action=b.get("next_action", "通过"), evidence=b.get("evidence"))))
                return self._json(_wrap(True, WF.begin_review(b["matter_id"], actor=b.get("reviewer"))))
            if p == "/api/acc/confirm":
                if not b.get("confirm_id"):
                    return self._json(_fail(4001, "缺少 confirm_id"))
                r = WF.resolve_confirm(b["confirm_id"], action=b.get("action", "confirm"),
                                       actor=b.get("actor"), comment=b.get("comment"),
                                       token=b.get("token"),
                                       check_token=b.get("token") is not None)
                return self._json(_wrap(r.get("ok", False), r,
                                        0 if r.get("ok") else 4100,
                                        None if r.get("ok") else
                                        {"code": "BLOCKED", "message": r.get("error")}))
            if p == "/api/acc/urge":
                if not b.get("matter_id"):
                    return self._json(_fail(4001, "缺少 matter_id"))
                return self._json(_wrap(True, WF.urge_now(b["matter_id"], actor=b.get("actor"))))
            if p == "/api/acc/sweep":
                return self._json(_wrap(True, WF.sweep_timeouts(as_of=b.get("as_of"))))
            if p == "/api/acc/owners":
                if b.get("do") == "upsert":
                    return self._json(_wrap(True, WF.owner_upsert(
                        b.get("level"), actor=b.get("actor"),
                        primary_owner=b.get("primary_owner"), deputy_owner=b.get("deputy_owner"),
                        escalate_to=b.get("escalate_to"),
                        review_sla_hours=b.get("review_sla_hours"),
                        confirm_sla_hours=b.get("confirm_sla_hours"),
                        urge_interval_hours=b.get("urge_interval_hours"),
                        max_urge_times=b.get("max_urge_times"))))
                return self._json(_wrap(True, WF.owner_matrix()))
        except Exception as e:                              # noqa: BLE001
            return self._json(_fail(5000, "%s: %s" % (type(e).__name__, e)))
        return self._json(_fail(4002, "未知接口：%s" % p), 404)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write("[acc] 责任机制服务已启动 http://127.0.0.1:%d/\n" % PORT)
    sys.stderr.write("[acc] 钉钉通道：%s\n" % WF.dingtalk_status())
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
