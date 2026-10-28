'''Command-line entry point for the rolling forecast pipeline.

Exit codes:
    0  run completed and the result was written
    2  input blocked by the data gates (nothing was computed)
    3  a stage failed
    4  usage error
'''
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import load_config
from .pipeline import StageError, run_pipeline


def _configure_logging(verbose: bool, json_logs: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if json_logs:
        # The pipeline already emits JSON lines; keep them verbatim.
        handler.setFormatter(logging.Formatter('%(message)s'))
    else:
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    root = logging.getLogger('rollcast')
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False


def _load_payload(path: Path) -> dict:
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        raise SystemExit(f'cannot read input: {exc}')
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f'invalid JSON in {path}: {exc}')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Rolling cash-flow forecast pipeline')
    parser.add_argument('input', type=Path, nargs='?', help='run payload (JSON)')
    parser.add_argument('--init-db', action='store_true',
                        help='create the tables using the admin account, then exit')
    parser.add_argument('--output', type=Path, required=True, help='result file (JSON)')
    parser.add_argument('--config', type=Path, help='config file (JSON or YAML)')
    parser.add_argument('--run-id', help='explicit run id (default: generated)')
    parser.add_argument('--persist', action='store_true', help='write the run to MySQL')
    parser.add_argument('--no-persist', action='store_true', help='override config: skip storage')
    parser.add_argument('--draws', type=int, help='override Monte Carlo paths')
    parser.add_argument('--seed', type=int, help='override random seed')
    parser.add_argument('--block-length', type=int, help='override fallback block length')
    parser.add_argument('--debias', choices=['auto', 'true', 'false'], help='override bias handling')
    parser.add_argument('--warmup', type=int, help='override backtest warmup origins')
    parser.add_argument('--json-logs', action='store_true', help='emit raw JSON log lines')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args(argv)

    _configure_logging(args.verbose, args.json_logs)

    if args.init_db:
        from .store import ensure_schema, StoreError
        try:
            ensure_schema()
        except StoreError as exc:
            print(f'init-db failed: {exc}', file=sys.stderr)
            return 3
        print('schema ready')
        return 0

    if args.input is None:
        parser.error('input is required unless --init-db is given')

    overrides: dict[str, object] = {
        'draws': args.draws, 'seed': args.seed, 'block_length': args.block_length,
        'warmup': args.warmup,
    }
    if args.debias:
        overrides['debias'] = args.debias
    try:
        config = load_config(args.config, **overrides)
    except (ValueError, RuntimeError, KeyError) as exc:
        print(f'config error: {exc}', file=sys.stderr)
        return 4

    payload = _load_payload(args.input)

    persist = None
    if args.no_persist:
        persist = False
    elif args.persist:
        persist = True

    try:
        result = run_pipeline(payload, config, run_id=args.run_id, persist=persist)
    except StageError as exc:
        print(f'stage error: {exc}', file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        print(f'unexpected error: {exc}', file=sys.stderr)
        return 3

    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
            encoding='utf-8')
    except (OSError, ValueError) as exc:
        print(f'cannot write output: {exc}', file=sys.stderr)
        return 3

    status = result.get('status')
    shortfall = (result.get('forecast') or {}).get('probability_shortfall_scenario')
    print(f'{status} -> {args.output}'
          + (f'  (shortfall={shortfall:.4f})' if isinstance(shortfall, float) else ''))
    if status == 'blocked':
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
