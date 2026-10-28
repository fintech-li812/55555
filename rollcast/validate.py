'''Input contract and gates for the rolling forecast pipeline.

Every rejection carries a stable code so callers can branch on it instead of
parsing prose. Nothing here silently repairs data: an ambiguous value is
reported, not guessed.
'''
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .core import HORIZON

REQUIRED_QUALITY_GATES = ('reconciled', 'unique_records', 'complete', 'fresh', 'unit_consistent')


class ValidationError(Exception):
    def __init__(self, code: str, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path

    def as_dict(self) -> dict[str, Any]:
        return {'code': self.code, 'message': self.message, 'path': self.path}


@dataclass
class ValidationReport:
    ok: bool
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    input_sha256: str = ''
    facts: dict[str, Any] = field(default_factory=dict)

    def fail(self, code: str, message: str, path: str | None = None) -> None:
        self.ok = False
        self.errors.append({'code': code, 'message': message, 'path': path})

    def warn(self, code: str, message: str, path: str | None = None) -> None:
        self.warnings.append({'code': code, 'message': message, 'path': path})


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValidationError('NOT_FINITE', f'{path}: expected a number', path)
    try:
        val = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError('NOT_FINITE', f'{path}: expected a number', path) from exc
    if not math.isfinite(val):
        raise ValidationError('NOT_FINITE', f'{path}: NaN or infinite', path)
    return val


def _money(value: Any, path: str) -> float:
    val = _number(value, path)
    return val


def _vector(value: Any, path: str, rows: int) -> np.ndarray:
    '''Validate a flat list of numbers, e.g. weekly_inflow.'''
    if not isinstance(value, (list, tuple)):
        raise ValidationError('BAD_SHAPE', f'{path}: expected a list of {rows} numbers', path)
    if len(value) != rows:
        raise ValidationError('BAD_SHAPE', f'{path}: expected {rows} entries, got {len(value)}', path)
    out = np.empty(rows, dtype=float)
    for i, cell in enumerate(value):
        out[i] = _number(cell, f'{path}[{i}]')
    if not np.all(np.isfinite(out)):
        raise ValidationError('NOT_FINITE', f'{path}: contains NaN or infinite', path)
    return out


def _matrix(value: Any, path: str, rows: int, cols: int) -> np.ndarray:
    if not isinstance(value, (list, tuple)):
        raise ValidationError('BAD_SHAPE', f'{path}: expected a list of rows', path)
    if len(value) != rows:
        raise ValidationError('BAD_SHAPE', f'{path}: expected {rows} rows, got {len(value)}', path)
    out = np.empty((rows, cols), dtype=float)
    for i, row in enumerate(value):
        if not isinstance(row, (list, tuple)) or len(row) != cols:
            raise ValidationError('BAD_SHAPE', f'{path}[{i}]: expected {cols} columns', path)
        for j, cell in enumerate(row):
            out[i, j] = _number(cell, f'{path}[{i}][{j}]')
    if not np.all(np.isfinite(out)):
        raise ValidationError('NOT_FINITE', f'{path}: contains NaN or infinite', path)
    return out


def validate_payload(payload: dict[str, Any]) -> ValidationReport:
    '''Validate a full run request. Returns a report; never raises for data issues.'''
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    report = ValidationReport(ok=True, input_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest())

    try:
        if not isinstance(payload, dict):
            raise ValidationError('BAD_ROOT', 'payload must be a JSON object')

        unit = payload.get('unit')
        if not isinstance(unit, str) or not unit.strip():
            raise ValidationError('UNIT_MISSING', 'unit: explicit monetary unit required', 'unit')

        gates = payload.get('quality_checks')
        if not isinstance(gates, dict):
            raise ValidationError('GATES_MISSING', 'quality_checks: object required', 'quality_checks')
        for key in REQUIRED_QUALITY_GATES:
            if type(gates.get(key)) is not bool:
                raise ValidationError(
                    'GATE_NOT_BOOL',
                    f'quality_checks.{key}: must be an explicit boolean, not inferred',
                    f'quality_checks.{key}')
        failed = [k for k in REQUIRED_QUALITY_GATES if not gates[k]]
        if failed:
            raise ValidationError('GATE_FAILED', f'quality gates failed: {", ".join(failed)}', 'quality_checks')

        # --- history -------------------------------------------------------
        history = payload.get('historical_forecast_errors')
        if not isinstance(history, list) or len(history) < 26:
            raise ValidationError(
                'HISTORY_TOO_SHORT',
                'historical_forecast_errors: at least 26 chronological weekly rows required',
                'historical_forecast_errors')
        hist = _matrix(history, 'historical_forecast_errors', len(history), 2)
        report.facts['history_weeks'] = int(hist.shape[0])

        # --- current plan --------------------------------------------------
        plan = payload.get('current_plan')
        if not isinstance(plan, dict):
            raise ValidationError('PLAN_MISSING', 'current_plan: object required', 'current_plan')
        inflow = plan.get('weekly_inflow')
        outflow = plan.get('weekly_outflow')
        if not isinstance(inflow, list) or not isinstance(outflow, list):
            raise ValidationError('PLAN_MISSING', 'current_plan: weekly_inflow/weekly_outflow required', 'current_plan')
        in_arr = _vector(inflow, 'current_plan.weekly_inflow', HORIZON)
        out_arr = _vector(outflow, 'current_plan.weekly_outflow', HORIZON)
        if np.any(in_arr < 0) or np.any(out_arr < 0):
            raise ValidationError(
                'PLAN_NEGATIVE', 'weekly inflow/outflow must be nonnegative gross amounts', 'current_plan')

        cash_total = _money(plan.get('cash_total'), 'current_plan.cash_total')
        restricted = _money(plan.get('cash_restricted'), 'current_plan.cash_restricted')
        credit = _money(plan.get('committed_credit_available_now'), 'current_plan.committed_credit_available_now')
        buffer = _money(plan.get('minimum_buffer'), 'current_plan.minimum_buffer')
        if min(cash_total, restricted, credit, buffer) < 0:
            raise ValidationError('CASH_NEGATIVE', 'cash/credit/buffer must be nonnegative', 'current_plan')
        if restricted > cash_total:
            raise ValidationError('CASH_INCONSISTENT', 'cash_restricted cannot exceed cash_total', 'current_plan')

        # --- panel (optional; only needed for backtesting) ------------------
        panel = payload.get('panel')
        origins = 0
        if panel is not None:
            if not isinstance(panel, list) or not panel:
                raise ValidationError('PANEL_BAD', 'panel: nonempty list required when supplied', 'panel')
            last = None
            for i, row in enumerate(panel):
                if not isinstance(row, dict):
                    raise ValidationError('PANEL_BAD', f'panel[{i}]: object required', f'panel[{i}]')
                origin = row.get('origin')
                if last is not None and isinstance(origin, str) and isinstance(last, str) and origin <= last:
                    raise ValidationError(
                        'PANEL_UNSORTED',
                        f'panel[{i}]: origins must be strictly increasing in time', f'panel[{i}]')
                last = origin
                fc = row.get('forecast')
                ac = row.get('actual')
                if not isinstance(fc, list) or not isinstance(ac, list):
                    raise ValidationError('PANEL_BAD', f'panel[{i}]: forecast/actual required', f'panel[{i}]')
                _matrix(fc, f'panel[{i}].forecast', HORIZON, 2)
                _matrix(ac, f'panel[{i}].actual', HORIZON, 2)
            origins = len(panel)

        report.facts.update({
            'unit': unit,
            'origins': origins,
            'cash_available': cash_total - restricted,
            'initial_capacity': cash_total - restricted + credit,
        })

        # --- advisory checks (warn, never block) ----------------------------
        if origins and origins < 26:
            report.warn('FEW_ORIGINS', f'panel has only {origins} origins; backtest may be unstable', 'panel')
        if hist.shape[0] < 52:
            report.warn('SHORT_HISTORY', f'{hist.shape[0]} weeks of history is below the 52-week '
                                         'guidance for reliable seasonality detection',
                        'historical_forecast_errors')
        if credit > 0 and credit > (cash_total - restricted):
            report.warn('CREDIT_DOMINANT', 'committed credit exceeds available cash; the capacity path '
                                           'depends heavily on undrawn facilities', 'current_plan')

    except ValidationError as exc:
        report.fail(exc.code, exc.message, exc.path)

    return report
