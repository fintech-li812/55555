"""Read-only dump: 把固化的 xlsx 模板输入解析成语言无关的 JSON fixture。

设计动机：云端（千问 Pages / Node）不应该、也不需要解析 xlsx。
固定模板不是通用导入器——输入在生产侧固化，绑定工作簿 SHA256。
本脚本只在本地开发机跑一次，产物供 Node 侧作为对拍输入。

只读：不修改 program_v1 任何源文件。
"""
from pathlib import Path
import json
import sys

PROGRAM_ROOT = Path(r"D:/CashflowRiskWorkbench/mcp-server/program_v1")
OUT_DIR = Path(r"D:/CashflowRiskWorkbench/migration/contract/v1")

sys.path.insert(0, str(PROGRAM_ROOT / "src"))
import program_engine as pe  # noqa: E402


def main():
    registry = pe.registry()
    sources = {}
    for source_id, filename in pe.SOURCES.items():
        path = PROGRAM_ROOT / "inputs" / filename
        raw = path.read_bytes()
        digest = pe.sha(raw)
        if digest != registry[source_id]["sha256"]:
            raise SystemExit(f"registry mismatch for {source_id}")
        data = pe.read_template(raw)
        sources[source_id] = {
            "logical_name": filename,
            "input_sha256": digest,
            "registry": registry[source_id],
            "data": data,
        }
        print(f"  {source_id}: {filename}  sha256={digest[:16]}...  fields={len(data)}")

    payload = {
        "producer": "python reference engine (read-only dump)",
        "contract_version": "v1",
        "purpose": "language-agnostic fixture输入，供 Node 侧对拍；不含任何真实企业数据",
        "engine_version": pe.VERSION,
        "fields": list(pe.FIELDS),
        "money_fields": list(pe.MONEY_FIELDS),
        "sources": sources,
    }
    out = OUT_DIR / "inputs_program.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nwritten: {out}  ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
