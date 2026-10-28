# -*- coding: utf-8 -*-
"""责任机制 · 模块三：钉钉确认书与催办通道。

【重要】本模块是「通道适配层」，不做任何业务判断，不影响金额与风险判定。

两种运行模式（由环境变量自动切换，返回中如实标注 mode，绝不伪造送达）：
  · real  ：配置了 DINGTALK_WEBHOOK（或 APP_KEY/APP_SECRET）→ 真实调用钉钉开放平台
            - 自定义机器人 webhook：https://oapi.dingtalk.com/robot/send?access_token=XXX
            - 工作通知需先取 access_token：https://api.dingtalk.com/v1.0/oauth2/accessToken
  · dry-run：未配置 → 生成确认书内容并落库，返回模拟 message_id（前缀 dryrun-）。
            确认入口仍可用（本地回调地址），因此「在线确认/驳回 → 状态实时回写」
            这条链路在 dry-run 下依然完整可验证。

【待替换项】
  DINGTALK_WEBHOOK        自定义机器人 webhook（含 access_token）
  DINGTALK_APP_KEY        企业内部应用 AppKey（工作通知用）
  DINGTALK_APP_SECRET     企业内部应用 AppSecret
  DINGTALK_AGENT_ID       应用 AgentId（工作通知必填）
  CF_CONFIRM_BASE         确认入口的公网基址，如 https://risk.example.com（必须外网可达，
                          否则钉钉里点开的是无效链接）
  CF_CONFIRM_SECRET       确认令牌签名密钥（必须替换为随机长串）
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import uuid

from . import db as _db

# ---- 环境变量读取（与 db._cfg 同源）----
_cfg = _db._cfg

DINGTALK_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
DINGTALK_NOTIFY_URL = "https://api.dingtalk.com/v1.0/im/v1messages/robot/send"


def mode() -> dict:
    """返回当前通道模式与可用能力。"""
    webhook = _cfg("DINGTALK_WEBHOOK", "")
    app_key = _cfg("DINGTALK_APP_KEY", "")
    app_secret = _cfg("DINGTALK_APP_SECRET", "")
    base = _cfg("CF_CONFIRM_BASE", "http://127.0.0.1:8020")
    if webhook:
        return {"mode": "real", "channel": "webhook", "base": base,
                "note": "已配置自定义机器人 webhook，将真实发送"}
    if app_key and app_secret:
        return {"mode": "real", "channel": "worknotice", "base": base,
                "note": "已配置企业内部应用，将发送工作通知"}
    return {"mode": "dry-run", "channel": "none", "base": base,
            "note": "未配置钉钉凭据：确认书仅落库并生成模拟 message_id，不会真实送达钉钉"}


def _confirm_base() -> str:
    return _cfg("CF_CONFIRM_BASE", "http://127.0.0.1:8020").rstrip("/")


def _secret() -> str:
    """【待替换项】默认密钥仅为本机演示，生产必须在环境变量中设置强随机值。"""
    return _cfg("CF_CONFIRM_SECRET", "DEV-ONLY-CHANGE-ME-cf-confirm-secret")


def sign_token(confirm_id: str) -> str:
    """为确认书生成一次性签名令牌（HMAC-SHA256 前 32 位）。"""
    return hmac.new(_secret().encode("utf-8"), confirm_id.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def verify_token(confirm_id: str, token: str) -> bool:
    try:
        return hmac.compare_digest(sign_token(confirm_id), str(token or ""))
    except Exception:
        return False


def confirm_url(confirm_id: str) -> str:
    return "%s/dingtalk/confirm?confirm_id=%s&token=%s" % (
        _confirm_base(), confirm_id, sign_token(confirm_id))


def build_confirm_doc(matter: dict, review: dict, recipients: list) -> dict:
    """构造确认书正文：风险等级 / 事项描述 / 核实结论 / 确认入口。"""
    return {
        "title": "【风险事项确认书】%s" % matter.get("title"),
        "risk_level": matter.get("risk_level"),
        "risk_level_name": matter.get("risk_level_name"),
        "lamp": matter.get("lamp"),
        "matter_id": matter.get("matter_id"),
        "description": matter.get("description"),
        "amount": matter.get("amount"),
        "industry": matter.get("industry"),
        "source": matter.get("source"),
        "source_ref": matter.get("source_ref"),
        "round": matter.get("round"),
        "review": {
            "reviewer": review.get("reviewer"),
            "review_at": review.get("review_at"),
            "opinion": review.get("opinion"),
            "conclusion": review.get("conclusion"),
            "next_action": review.get("next_action"),
        },
        "owner": matter.get("current_owner"),
        "recipients": recipients,
        "actions": ["确认", "驳回"],
    }


def _post_json(url: str, payload: dict, headers: dict = None, timeout: int = 10):
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
        headers=dict({"Content-Type": "application/json; charset=utf-8"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _send_markdown(webhook: str, title: str, text: str):
    """自定义机器人 markdown 消息（加签可选，此处用 access_token 方式）。"""
    return _post_json(webhook, {"msgtype": "markdown",
                                "markdown": {"title": title, "text": text}})


def _access_token():
    """企业内部应用取 access_token（工作通知用）。"""
    r = _post_json(DINGTALK_TOKEN_URL,
                   {"appKey": _cfg("DINGTALK_APP_KEY"),
                    "appSecret": _cfg("DINGTALK_APP_SECRET")})
    return r.get("accessToken")


def _send_worknotice(token: str, user_ids: list, markdown: dict):
    payload = {"robotCode": _cfg("DINGTALK_APP_KEY"),
               "userIds": user_ids,
               "msgKey": "sampleMarkdown",
               "msgParam": json.dumps(markdown, ensure_ascii=False)}
    return _post_json(DINGTALK_NOTIFY_URL, payload,
                      headers={"x-acs-dingtalk-access-token": token})


def _render_text(doc: dict, url: str, extra: str = "") -> str:
    return (
        "### %s\n\n"
        "- 风险等级：**%s（%s）**\n"
        "- 事项编号：%s（第 %s 轮）\n"
        "- 事项描述：%s\n"
        "- 涉及金额：%s 元\n"
        "- 核实人：%s（%s）\n"
        "- 核实结论：**%s**\n"
        "- 核实意见：%s\n\n"
        "%s\n"
        "➡️ [点击在线确认或驳回](%s)\n"
    ) % (doc["title"], doc["risk_level_name"], doc["lamp"], doc["matter_id"], doc["round"],
         doc["description"], doc["amount"], doc["review"]["reviewer"],
         doc["review"]["review_at"], doc["review"]["conclusion"],
         doc["review"]["opinion"] or "（无）", extra, url)


def send_confirm(matter: dict, review: dict, recipients: list = None) -> dict:
    """发送钉钉确认书，落库并返回 confirm_id / 确认入口。"""
    m = mode()
    cid = "CFM-" + uuid.uuid4().hex[:12]
    now = _dt.datetime.now()
    cfg_owner = (recipients or [])
    if not cfg_owner:
        cfg_owner = [x for x in (matter.get("current_owner"),) if x]
    doc = build_confirm_doc(matter, review, cfg_owner)
    url = confirm_url(cid)
    doc["confirm_url"] = url
    text = _render_text(doc, url)

    message_id, sent, error = None, False, None
    if m["mode"] == "real":
        try:
            if m["channel"] == "webhook":
                r = _send_markdown(_cfg("DINGTALK_WEBHOOK"), doc["title"], text)
                message_id = r.get("msgId") or r.get("messageId") or ("webhook-%s" % cid)
            else:
                tk = _access_token()
                ids = [x for x in (_cfg("DINGTALK_TO_USERIDS", "").split(",")) if x]
                r = _send_worknotice(tk, ids, {"title": doc["title"], "text": text})
                message_id = r.get("processQueryKey") or ("worknotice-%s" % cid)
            sent = True
        except Exception as e:
            error = "%s: %s" % (type(e).__name__, e)
            message_id = None
    else:
        message_id = "dryrun-" + cid

    rec = {"confirm_id": cid, "matter_id": matter.get("matter_id"),
           "round": int(matter.get("round", 1)), "channel": "dingtalk",
           "recipients": cfg_owner, "message_id": message_id,
           "sent_at": now.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (now.microsecond // 1000),
           "status": "pending" if sent or m["mode"] == "dry-run" else "failed",
           "confirm_url": url, "token": sign_token(cid),
           "expire_at": (now + _dt.timedelta(hours=int(
               _cfg("CF_CONFIRM_TTL_HOURS", "72") or 72))).strftime("%Y-%m-%d %H:%M:%S"),
           "confirmed_by": None, "confirmed_at": None, "comment": None,
           "payload": doc, "mode": m["mode"], "error": error}
    persisted, perr = _persist_confirm(rec)
    return {"confirm_id": cid, "mode": m["mode"], "sent": sent, "error": error,
            "message_id": message_id, "confirm_url": url, "recipients": cfg_owner,
            "document": doc, "text": text, "persisted": persisted, "persist_error": perr,
            "note": m["note"]}


def send_urge(matter: dict, confirm_id: str, seq: int, reason: str = "confirm_timeout") -> dict:
    """发送催办提醒（第 seq 次）。"""
    m = mode()
    uid = "URG-" + uuid.uuid4().hex[:12]
    now = _dt.datetime.now()
    owner = matter.get("current_owner")
    text = ("### ⏰ 催办提醒（第 %d 次）\n\n"
            "- 事项：%s（%s）\n"
            "- 风险等级：%s（%s）\n"
            "- 当前责任人：%s\n"
            "- 触发原因：%s\n"
            "- 原确认截止：%s\n\n"
            "请尽快完成确认或驳回，超时将自动升级。\n") % (
        seq, matter.get("title"), matter.get("matter_id"),
        matter.get("risk_level_name"), matter.get("lamp"), owner,
        reason, matter.get("confirm_due_at"))
    url = confirm_url(confirm_id) if confirm_id else ""
    if url:
        text += "\n➡️ [立即处理](%s)\n" % url

    message_id, sent, error = None, False, None
    if m["mode"] == "real":
        try:
            if m["channel"] == "webhook":
                r = _send_markdown(_cfg("DINGTALK_WEBHOOK"), "催办提醒", text)
                message_id = r.get("msgId") or ("webhook-%s" % uid)
            else:
                tk = _access_token()
                ids = [x for x in (_cfg("DINGTALK_TO_USERIDS", "").split(",")) if x]
                r = _send_worknotice(tk, ids, {"title": "催办提醒", "text": text})
                message_id = r.get("processQueryKey") or ("worknotice-%s" % uid)
            sent = True
        except Exception as e:
            error = "%s: %s" % (type(e).__name__, e)
    else:
        message_id = "dryrun-" + uid
        sent = True

    rec = {"urge_id": uid, "matter_id": matter.get("matter_id"), "confirm_id": confirm_id,
           "round": int(matter.get("round", 1)), "seq": int(seq), "channel": "dingtalk",
           "recipients": [owner] if owner else [], "message_id": message_id,
           "sent_at": now.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (now.microsecond // 1000),
           "trigger_reason": reason, "status": "sent" if sent else "failed",
           "payload": {"text": text}, "mode": m["mode"], "error": error}
    persisted, perr = _persist_urge(rec)
    return {"urge_id": uid, "seq": seq, "mode": m["mode"], "sent": sent,
            "message_id": message_id, "error": error, "recipients": rec["recipients"],
            "text": text, "persisted": persisted, "persist_error": perr}


# ---------------------------------------------------------------------------
# 落库（失败回落内存，并返回 persisted=False）
# ---------------------------------------------------------------------------
_MEM_CONFIRM, _MEM_URGE = [], []


def _persist_confirm(r: dict):
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO %s (confirm_id,matter_id,round,channel,recipients_json,"
                    " message_id,sent_at,status,confirm_url,token,expire_at,payload_json)"
                    " VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)"
                    % _db.T_CONFIRM,
                    (r["confirm_id"], r["matter_id"], r["round"], r["channel"],
                     _db.jdumps(r["recipients"]), r["message_id"], r["sent_at"],
                     r["status"], r["confirm_url"], r["token"], r["expire_at"],
                     _db.jdumps(r["payload"])))
            c.commit()
            return True, None
        finally:
            c.close()
    except Exception as e:
        _MEM_CONFIRM.append(r)
        return False, "%s: %s" % (type(e).__name__, e)


def _persist_urge(r: dict):
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO %s (urge_id,matter_id,confirm_id,round,seq,channel,"
                    " recipients_json,message_id,sent_at,trigger_reason,status,payload_json)"
                    " VALUES (%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s,%%s)"
                    % _db.T_URGE,
                    (r["urge_id"], r["matter_id"], r["confirm_id"], r["round"], r["seq"],
                     r["channel"], _db.jdumps(r["recipients"]), r["message_id"],
                     r["sent_at"], r["trigger_reason"], r["status"], _db.jdumps(r["payload"])))
            c.commit()
            return True, None
        finally:
            c.close()
    except Exception as e:
        _MEM_URGE.append(r)
        return False, "%s: %s" % (type(e).__name__, e)


def update_confirm_status(confirm_id: str, status: str, by: str = None,
                          comment: str = None) -> dict:
    """确认/驳回后实时回写确认书状态。"""
    _t = _dt.datetime.now()
    now = _t.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (_t.microsecond // 1000)
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "UPDATE %s SET status=%%s, confirmed_by=%%s, confirmed_at=%%s,"
                    " comment=%%s WHERE confirm_id=%%s" % _db.T_CONFIRM,
                    (status, by, now, comment, confirm_id))
            c.commit()
            return {"ok": True, "confirm_id": confirm_id, "status": status,
                    "confirmed_by": by, "confirmed_at": now, "persisted": True}
        finally:
            c.close()
    except Exception as e:
        for r in _MEM_CONFIRM:
            if r["confirm_id"] == confirm_id:
                r.update({"status": status, "confirmed_by": by,
                          "confirmed_at": now, "comment": comment})
        return {"ok": True, "confirm_id": confirm_id, "status": status, "persisted": False,
                "error": "%s: %s" % (type(e).__name__, e)}


def load_confirm(confirm_id: str):
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT confirm_id,matter_id,round,status,token,expire_at,payload_json,"
                    " confirmed_by,confirmed_at,comment FROM %s WHERE confirm_id=%%s"
                    % _db.T_CONFIRM, (confirm_id,))
                row = cur.fetchone()
            if not row:
                return None
            d = dict(zip(["confirm_id", "matter_id", "round", "status", "token",
                          "expire_at", "payload", "confirmed_by", "confirmed_at",
                          "comment"], row))
            try:
                d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
            except Exception:
                d["payload"] = {}
            d["expire_at"] = str(d["expire_at"]) if d["expire_at"] else None
            d["confirmed_at"] = str(d["confirmed_at"]) if d["confirmed_at"] else None
            return d
        finally:
            c.close()
    except Exception:
        for r in _MEM_CONFIRM:
            if r["confirm_id"] == confirm_id:
                return r
        return None


def list_confirms(matter_id: str = None, status: str = None, limit: int = 20) -> dict:
    where, args = [], []
    if matter_id:
        where.append("matter_id=%s"); args.append(matter_id)
    if status:
        where.append("status=%s"); args.append(status)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        c = _db.connect()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT confirm_id,matter_id,round,channel,status,sent_at,confirmed_by,"
                    " confirmed_at,comment,confirm_url,message_id FROM %s%s"
                    " ORDER BY created_at DESC LIMIT %%s" % (_db.T_CONFIRM, w), args + [limit])
                rows = cur.fetchall()
            keys = ["confirm_id", "matter_id", "round", "channel", "status", "sent_at",
                    "confirmed_by", "confirmed_at", "comment", "confirm_url", "message_id"]
            items = []
            for r in rows:
                d = dict(zip(keys, r))
                for k in ("sent_at", "confirmed_at"):
                    if d.get(k) is not None:
                        d[k] = str(d[k])
                items.append(d)
            return {"source": "mysql", "items": items}
        finally:
            c.close()
    except Exception:
        return {"source": "memory", "items": _MEM_CONFIRM[-int(limit):]}
