"""Agent result guard. No model inference, network, database, or file writes."""
import re
from decimal import Decimal

VERSION = "mfg-agent-result.v1"
STATES = {"success", "missing_inputs", "formula_error", "unavailable"}
CONTEXT = ("tenant_id", "company_id", "period", "source_revision", "run_id")
AMOUNTS = ("opening", "inflow", "outflow", "net", "closing", "actual_closing", "difference")

class ResultError(ValueError):
    pass

def require(ok, reason):
    if not ok:
        raise ResultError(reason)

def money(value):
    require(isinstance(value, str) and re.fullmatch(r"-?(?:0|[1-9][0-9]{0,17})\.[0-9]{2}", value) is not None,
            "amount must be a finite decimal string in yuan, at most 18 integer digits and 2 decimals")
    return Decimal(value)

def validate_result(data, context, *, allow_fixture=False):
    require(isinstance(data, dict) and isinstance(context, dict), "object required")
    require(data.get("schema_version") == VERSION, "unsupported schema version")
    for key in CONTEXT:
        require(isinstance(context.get(key), str) and bool(context[key].strip()), "trusted context missing: " + key)
        require(data.get(key) == context[key], "context mismatch: " + key)
    require(re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", data["period"]) is not None,
            "period must be YYYY-MM with valid month")
    require(data.get("industry") == "manufacturing", "out of scope industry")
    status = data.get("status")
    require(isinstance(status, str) and status in STATES, "unknown status")
    require(isinstance(data.get("issues"), list) and all(isinstance(x, str) and x.strip() for x in data["issues"]),
            "issues must be a list of nonempty strings")
    if status != "success":
        require(bool(data["issues"]), "non-success requires a reason")
        require(data.get("amounts") is None, "non-success cannot expose a completed result")
        return data
    require(data.get("origin") == "qwen_sheet" or (allow_fixture and data.get("origin") in ("offline_fixture", "program_calculation")),
            "production result must come from a bound Qwen sheet")
    if data.get("origin") == "program_calculation":
        require(data.get("data_type") == "SYNTHETIC", "program_v1_requires_explicit_synthetic_label")
    require(data.get("unit") == "CNY", "result must be yuan")
    require(isinstance(data.get("calculation_version"), str) and bool(data["calculation_version"].strip()),
            "missing calculation version")
    require(data.get("coverage") == "full_cashflow", "cannot claim full balance from partial cashflow")
    require(data["issues"] == [], "success cannot hide unresolved input issues")
    source = data.get("source")
    require(isinstance(source, dict) and all(isinstance(source.get(k), str) and source[k].strip()
            for k in ("workbook_ref", "sheet", "range", "snapshot_ref")), "missing source locator")
    amounts = data.get("amounts")
    require(isinstance(amounts, dict) and set(amounts) == set(AMOUNTS), "missing or unexpected amount fields")
    vals = {k: money(amounts[k]) for k in AMOUNTS if k not in ("actual_closing", "difference")}
    require(vals["inflow"] >= 0 and vals["outflow"] >= 0, "gross inflow/outflow must not be negative")
    require(vals["net"] == vals["inflow"] - vals["outflow"], "net does not reconcile")
    require(vals["closing"] == vals["opening"] + vals["net"], "balance does not reconcile")
    if amounts["actual_closing"] is None:
        require(amounts["difference"] is None, "missing actual balance must not produce a difference")
    else:
        actual = money(amounts["actual_closing"])
        difference = money(amounts["difference"])
        require(difference == actual - vals["closing"], "difference is incorrect")
    return data

def render_result(data, context, *, allow_fixture=False):
    validate_result(data, context, allow_fixture=allow_fixture)
    if data["status"] != "success":
        return "本次未取得完整现金流结果：" + "；".join(data["issues"]) + "。没有执行保存或通知。"
    a = data["amounts"]
    text = (f"{data['period']}现金流（元）：流入{a['inflow']}，流出{a['outflow']}，"
            f"净额{a['net']}，计算期末余额{a['closing']}。")
    if a["actual_closing"] is None:
        text += "缺少独立实际期末余额，尚未完成余额核对。"
    else:
        text += f"实际期末余额{a['actual_closing']}，差异{a['difference']}。"
        if Decimal(a["difference"]) != 0:
            text += "差异原因尚待核实。"
    if data.get("origin") == "offline_fixture":
        text = "【合成数据离线演示】" + text
    if data.get("origin") == "program_calculation":
        text = "【合成数据 · 程序本次计算】" + text
    text += f"来源：{data['source']['workbook_ref']} / {data['source']['sheet']} / {data['source']['range']}。"
    return text + "此结果不代表已入库或已通知。"

def unavailable(context):
    return dict(context, schema_version=VERSION, industry="manufacturing", status="unavailable",
                issues=["真实千问读表与算法适配器尚未绑定"], amounts=None)
