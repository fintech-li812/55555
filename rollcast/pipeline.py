'''End-to-end rolling-forecast workflow.

Stages run in a fixed order and each one records its own trace entry, so a run
can be replayed from its log alone:

    validate -> features -> backtest -> forecast -> persist

Retrying is limited to transient stages (persistence). Re-running the numerics
on failure would only produce the same deterministic answer twice, so a
numerical error is reported rather than retried.
'''
from __future__ import annotations

import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .config import Config
from .core import (
    HORIZON,
    RollingState,
    detect_seasonality,
    select_block_length,
    simulate_paths,
    summarise_paths,
    walk_forward_backtest,
)
from .validate import ValidationError, validate_payload

LOGGER = logging.getLogger('rollcast')

STAGE_VALIDATE = 'validate'
STAGE_FEATURES = 'features'
STAGE_BACKTEST = 'backtest'
STAGE_FORECAST = 'forecast'
STAGE_PERSIST = 'persist'


@dataclass
class Trace:
    run_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)

    def record(self, stage: str, **fields: Any) -> None:
        entry = {
            'stage': stage,
            'elapsed_ms': round((time.perf_counter() - self.started) * 1000, 2),
        }
        entry.update(fields)
        self.entries.append(entry)
        LOGGER.info(json.dumps({'run_id': self.run_id, **entry}, ensure_ascii=False))


class StageError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f'{stage}: {message}')
        self.stage = stage
        self.message = message


def _retry(attempts: int, backoff: float, stage: str, fn: Callable[[], Any],
           trace: Trace | None = None) -> Any:
    '''Retry a transient operation with linear backoff.

    Errors flagged `permanent` are raised on the first attempt: retrying a
    missing table or a denied permission only delays the operator seeing the
    real cause, and inflates the run's duration with pointless sleeps.
    '''
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below
            last = exc
            if trace is not None:
                trace.record(stage, attempt=attempt, outcome='retry',
                             error=str(exc)[:300])
            if getattr(exc, 'permanent', False):
                raise StageError(stage, str(exc)) from exc
            if attempt < attempts:
                time.sleep(backoff * attempt)
    raise StageError(stage, f'failed after {attempts} attempt(s): {last}')


def _build_features(payload: dict[str, Any]) -> dict[str, Any]:
    '''Derive the modelling inputs. Kept explicit so the stage is auditable.'''
    history = np.asarray(payload['historical_forecast_errors'], dtype=float)
    plan = payload['current_plan']
    inflow = np.asarray(plan['weekly_inflow'], dtype=float)
    outflow = np.asarray(plan['weekly_outflow'], dtype=float)

    state = RollingState()
    state.update(history)
    return {
        'history': history,
        'state': state,
        'inflow': inflow,
        'outflow': outflow,
        'available': float(plan['cash_total']) - float(plan['cash_restricted']),
        'initial_capacity': (float(plan['cash_total']) - float(plan['cash_restricted'])
                             + float(plan['committed_credit_available_now'])),
        'buffer': float(plan['minimum_buffer']),
    }


def run_pipeline(
    payload: dict[str, Any],
    config: Config,
    *,
    run_id: str | None = None,
    persist: bool | None = None,
) -> dict[str, Any]:
    '''Execute the full workflow and return a complete, serialisable result.'''
    run_id = run_id or ('rc-' + uuid.uuid4().hex[:16])
    should_persist = config.persist if persist is None else persist
    trace = Trace(run_id=run_id)
    started = time.perf_counter()

    result: dict[str, Any] = {
        'schema': 'rollcast-run.v1',
        'run_id': run_id,
        'config': config.to_dict(),
        'config_fingerprint': config.fingerprint(),
        'status': 'running',
        'external_action_executed': False,
    }

    # ---- 1. validate -----------------------------------------------------
    report = validate_payload(payload)
    result['input_sha256'] = report.input_sha256
    result['validation'] = {
        'ok': report.ok,
        'errors': report.errors,
        'warnings': report.warnings,
        'facts': report.facts,
    }
    trace.record(STAGE_VALIDATE, ok=report.ok,
                 errors=[e['code'] for e in report.errors],
                 warnings=[w['code'] for w in report.warnings],
                 input_sha256=report.input_sha256[:16])
    if not report.ok:
        result['status'] = 'blocked'
        result['reason'] = 'data gate blocked; repair the input and rerun'
        if should_persist:
            _persist(result, config, trace, report.errors)
        result['trace'] = trace.entries
        result['duration_ms'] = round((time.perf_counter() - started) * 1000, 2)
        return result

    # ---- 2. features -----------------------------------------------------
    try:
        features = _build_features(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise StageError(STAGE_FEATURES, str(exc)) from exc
    trace.record(STAGE_FEATURES, history_weeks=int(features['history'].shape[0]),
                 initial_capacity=round(features['initial_capacity'], 4))

    # ---- 3. walk-forward backtest ---------------------------------------
    backtest: dict[str, Any] | None = None
    panel = payload.get('panel')
    if panel:
        try:
            effective_warmup = min(config.warmup, max(0, len(panel) - 1))
            if effective_warmup < 26:
                backtest = {'status': 'skipped',
                            'reason': f'panel has {len(panel)} origins; at least 27 are needed '
                                      'for a walk-forward evaluation with warmup 26'}
            else:
                bt = walk_forward_backtest(
                    features['history'], panel,
                    warmup=effective_warmup, draws=config.draws,
                    configured_block=config.block_length, seed=config.seed,
                    debias=config.debias,
                    max_period=config.max_period if config.detect_seasonality else 1,
                    min_strength=(config.min_strength if config.detect_seasonality else 1.1),
                    window=config.window, alpha=config.alpha)
                backtest = {'status': 'completed', **bt.summary(),
                            'sample': bt.decisions[:5]}
        except (ValueError, TypeError) as exc:
            raise StageError(STAGE_BACKTEST, str(exc)) from exc
    else:
        backtest = {'status': 'not_run',
                    'reason': 'no panel supplied; supply origins to score accuracy'}
    result['backtest'] = backtest
    trace.record(STAGE_BACKTEST, status=backtest['status'],
                 mae=backtest.get('mae'), rmse=backtest.get('rmse'))

    # ---- 4. forecast -----------------------------------------------------
    history = features['history']
    if config.detect_seasonality:
        seasonality = detect_seasonality(history, config.max_period, config.min_strength)
        block, rationale = select_block_length(seasonality, config.block_length)
        notes = [s.note for s in seasonality]
    else:
        block, rationale = config.block_length, 'seasonality detection disabled by config'
        notes = ['detection disabled']

    try:
        sim = simulate_paths(
            features['inflow'], features['outflow'], history,
            initial_capacity=features['initial_capacity'],
            draws=config.draws, block=block, seed=config.seed,
            debias=config.debias, alpha=config.alpha)
    except (ValueError, TypeError) as exc:
        raise StageError(STAGE_FORECAST, str(exc)) from exc

    stats = summarise_paths(sim['paths'], features['buffer'])
    mean_path = sim['paths'].mean(axis=0)
    planned_cash = features['available'] + np.r_[0.0, np.cumsum(features['inflow'] - features['outflow'])]

    result['forecast'] = {
        'unit': payload['unit'],
        'horizon': HORIZON,
        'block_used': block,
        'block_rationale': rationale,
        'seasonality': notes,
        'bias': sim['bias_decision'],
        'initial_capacity': round(features['initial_capacity'], 2),
        'mean_capacity_path': [round(float(v), 2) for v in mean_path],
        'planned_cash_path': [round(float(v), 2) for v in planned_cash],
        **{k: (round(v, 6) if isinstance(v, float) and abs(v) < 1e4 else
               round(v, 2) if isinstance(v, float) else v)
           for k, v in stats.items()},
        'monte_carlo': {'draws': config.draws, 'seed': config.seed,
                        'standard_error': round(stats['monte_carlo_standard_error'], 6)},
    }
    result['status'] = 'calculated'
    trace.record(STAGE_FORECAST, block=block, draws=config.draws,
                 p_shortfall=round(stats['probability_shortfall_scenario'], 5),
                 gap_p95=round(stats['gap_p95'], 2))

    # ---- 5. persist ------------------------------------------------------
    if should_persist:
        _persist(result, config, trace, report.errors)

    result['trace'] = trace.entries
    result['duration_ms'] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _persist(result: dict[str, Any], config: Config, trace: Trace,
             validation_errors: list[dict[str, Any]]) -> None:
    from . import store  # imported lazily: persistence is optional

    record = {
        'run_id': result['run_id'],
        'config_fingerprint': result['config_fingerprint'],
        'input_sha256': result['input_sha256'],
        'status': result['status'],
        'unit': result.get('forecast', {}).get('unit', 'unknown'),
        'origin_count': int((result.get('backtest') or {}).get('origins') or 0),
        'history_weeks': int((result.get('validation', {}).get('facts') or {}).get('history_weeks') or 0),
        'block_used': (result.get('forecast') or {}).get('block_used'),
        'block_rationale': (result.get('forecast') or {}).get('block_rationale'),
        'initial_capacity': (result.get('forecast') or {}).get('initial_capacity'),
        'probability_shortfall': (result.get('forecast') or {}).get('probability_shortfall_scenario'),
        'gap_p95': (result.get('forecast') or {}).get('gap_p95'),
        'gap_cvar95': (result.get('forecast') or {}).get('gap_cvar95'),
        'mae': (result.get('backtest') or {}).get('mae'),
        'rmse': (result.get('backtest') or {}).get('rmse'),
        'terminal_drift': (result.get('backtest') or {}).get('mean_terminal_drift'),
        'payload': result,
        'errors': [dict(e, stage=STAGE_VALIDATE) for e in validation_errors],
    }

    def _write() -> dict[str, Any]:
        return store.save_run(record)

    try:
        receipt = _retry(config.retries, config.retry_backoff_seconds, STAGE_PERSIST,
                         _write, trace)
        result['persistence'] = receipt
        trace.record(STAGE_PERSIST, outcome='ok', **{k: v for k, v in receipt.items()
                                                     if k in ('status', 'verified', 'payload_bytes')})
    except StageError as exc:
        # Persistence failure must not lose the computed result: it is returned
        # with an explicit marker so the caller can replay it.
        result['persistence'] = {'status': 'failed', 'error': str(exc)}
        trace.record(STAGE_PERSIST, outcome='failed', error=str(exc)[:300])
