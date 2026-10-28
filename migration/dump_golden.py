"""Stage 1 - freeze the golden answers.

Reads every registered source through the untouched Python engine and writes one
language-neutral JSON file. That file becomes the judge for the Node port: every
transplanted module is compared against it, field by field.

Read-only against existing code. Nothing here writes into program_v1.
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "mcp-server" / "program_v1" / "src"
sys.path.insert(0, str(SRC))

import eight_sheet_engine as eight  # noqa: E402
import program_engine as pe  # noqa: E402

ORDER = [
    eight.SOURCE_ID,
    "program_demo_v1",
    "program_difference_v1",
    "manufacturing_sample_v1",
]

# The five files whose bytes define "the algorithm". Bound into the golden file so
# a silent edit to any of them invalidates the baseline instead of shifting it.
ALGORITHM_FILES = [
    "eight_sheet_engine.py",
    "account_binding.py",
    "cash_selection.py",
    "source_audit.py",
    "program_engine.py",
]


def one(source_id):
    """Call the engine exactly as production does. Errors are data, not crashes."""
    try:
        if source_id == eight.SOURCE_ID:
            return eight.calculate()
        return pe.calculate_source(source_id)
    except Exception as exc:  # noqa: BLE001 - we want the original text intact
        return {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}


def algorithm_digest():
    parts = {}
    for name in ALGORITHM_FILES:
        parts[name] = hashlib.sha256((SRC / name).read_bytes()).hexdigest()
    return parts


def main():
    sources = {}
    for source_id in ORDER:
        sources[source_id] = json.loads(json.dumps(one(source_id), default=str))

    payload = {
        "schema_version": "golden answers v1",
        "captured_at_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "producer": "python reference engine (read-only dump)",
        "note": "These are the Reference answers. The Node port must match them field by field.",
        "algorithm_sha256_per_file": algorithm_digest(),
        "sources": sources,
    }

    out = ROOT / "migration" / "contract" / "v1" / "golden_python.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print("written:", out)
    print("bytes:", out.stat().st_size)
    print()
    for source_id in ORDER:
        row = sources[source_id]
        result = row.get("result") or {}
        groups = result.get("cashflow_groups") or {}
        recon = result.get("reconciliation_state")
        print(
            f"{source_id:32s} status={row.get('status')} "
            f"reconciliation={recon} can_save={row.get('can_save_source_result')}"
        )
        for name in ("operating", "investing", "financing"):
            g = groups.get(name)
            if g:
                print(f"    {name:10s} in={g.get('inflow')} out={g.get('outflow')} net={g.get('net')}")
        if result.get("amounts"):
            a = result["amounts"]
            print(
                f"    money: opening={a.get('opening')} in={a.get('inflow')} "
                f"out={a.get('outflow')} net={a.get('net')} "
                f"computed_closing={a.get('closing')} actual_closing={a.get('actual_closing')} "
                f"difference={a.get('difference')}"
            )
        print()


if __name__ == "__main__":
    main()
