'''Run configuration for the rolling forecast pipeline.

Precedence: explicit kwargs > environment (CF_ROLLCAST_*) > file > defaults.
Every knob is printable without secrets so a run can be reproduced from its
own log line.
'''
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    # --- sampling ---------------------------------------------------------
    'draws': 5000,              # Monte Carlo paths per run
    'seed': 42,                 # fixed for reproducibility
    'block_length': 4,          # fallback when no cycle is detected
    'horizon': 13,              # forecast weeks
    # --- seasonality ------------------------------------------------------
    'detect_seasonality': True,
    'max_period': 26,
    'min_strength': 0.30,       # autocorrelation needed to declare a cycle
    # --- bias handling ----------------------------------------------------
    'debias': 'auto',           # 'auto' | True | False
    'alpha': 0.05,              # significance for the structural-bias test
    # --- history ----------------------------------------------------------
    'warmup': 26,               # origins before the first scored one
    'window': None,             # sliding window cap in weeks (None = all history)
    # --- runtime ----------------------------------------------------------
    'max_runtime_seconds': 300,
    'retries': 3,
    'retry_backoff_seconds': 2.0,
    # --- storage ----------------------------------------------------------
    'persist': False,
    'dry_run': False,
}

_BOOL_TRUE = {'1', 'true', 'yes', 'on'}
_BOOL_FALSE = {'0', 'false', 'no', 'off'}


@dataclass
class Config:
    draws: int = DEFAULTS['draws']
    seed: int = DEFAULTS['seed']
    block_length: int = DEFAULTS['block_length']
    horizon: int = DEFAULTS['horizon']
    detect_seasonality: bool = DEFAULTS['detect_seasonality']
    max_period: int = DEFAULTS['max_period']
    min_strength: float = DEFAULTS['min_strength']
    debias: str | bool = DEFAULTS['debias']
    alpha: float = DEFAULTS['alpha']
    warmup: int = DEFAULTS['warmup']
    window: int | None = DEFAULTS['window']
    max_runtime_seconds: int = DEFAULTS['max_runtime_seconds']
    retries: int = DEFAULTS['retries']
    retry_backoff_seconds: float = DEFAULTS['retry_backoff_seconds']
    persist: bool = DEFAULTS['persist']
    dry_run: bool = DEFAULTS['dry_run']

    def __post_init__(self) -> None:
        self.debias = _coerce_debias(self.debias)
        self._validate()

    def _validate(self) -> None:
        if not 1000 <= self.draws <= 100000:
            raise ValueError('draws: integer in [1000,100000] required')
        if not 0 <= self.seed <= 2 ** 32 - 1:
            raise ValueError('seed: integer in [0,2**32-1] required')
        if not 1 <= self.block_length <= 52:
            raise ValueError('block_length: integer in [1,52] required')
        if self.horizon != 13:
            raise ValueError('horizon: only 13 weeks is supported by the v2 contract')
        if not 0 < self.min_strength < 1:
            raise ValueError('min_strength: float in (0,1) required')
        if not 2 <= self.max_period <= 52:
            raise ValueError('max_period: integer in [2,52] required')
        if not 0 < self.alpha < 1:
            raise ValueError('alpha: float in (0,1) required')
        if not 26 <= self.warmup <= 520:
            raise ValueError('warmup: integer in [26,520] required')
        if self.window is not None and self.window < 26:
            raise ValueError('window: >=26 weeks or None required')
        if self.retries < 0:
            raise ValueError('retries: nonnegative integer required')
        if self.max_runtime_seconds < 10:
            raise ValueError('max_runtime_seconds: at least 10 required')

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        '''Short hash of the config so two runs can be compared for equality.'''
        import hashlib
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]


def _coerce(value: Any, target: type) -> Any:
    if target is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        raise ValueError(f'cannot parse boolean from {value!r}')
    if target in (int, float, str):
        return target(value)
    return value


def _coerce_debias(value: Any) -> str | bool:
    text = str(value).strip().lower()
    if text in ('auto',):
        return 'auto'
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    raise ValueError("debias: 'auto', true or false required")


def _type_of(name: str) -> type:
    for f in fields(Config):
        if f.name == name:
            return bool if f.type in ('bool', "str | bool") and name == 'debias' else {
                'int': int, 'float': float, 'bool': bool, 'str': str,
                'int | None': int, 'str | bool': str,
            }.get(str(f.type), str)
    raise KeyError(name)


def load_config(
    path: str | Path | None = None,
    env_prefix: str = 'CF_ROLLCAST_',
    **overrides: Any,
) -> Config:
    '''Build a Config from file, then environment, then explicit overrides.'''
    values: dict[str, Any] = dict(DEFAULTS)

    if path:
        text = Path(path).read_text(encoding='utf-8')
        if Path(path).suffix.lower() in ('.yaml', '.yml'):
            try:
                import yaml  # type: ignore
                data = yaml.safe_load(text) or {}
            except ImportError as exc:
                raise RuntimeError(
                    'YAML config requested but PyYAML is not installed; '
                    'use JSON config or install pyyaml') from exc
        else:
            data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError('config file must contain an object')
        # JSON has no comments; keys starting with "_" are treated as
        # documentation and ignored, so a config file can explain itself.
        settings = {k: v for k, v in data.items() if not k.startswith('_')}
        unknown = set(settings) - set(DEFAULTS)
        if unknown:
            raise ValueError(f'unknown config keys: {sorted(unknown)}')
        values.update(settings)

    for key in DEFAULTS:
        raw = os.environ.get(env_prefix + key.upper())
        if raw is not None and raw != '':
            values[key] = raw

    values.update({k: v for k, v in overrides.items() if v is not None})

    typed: dict[str, Any] = {}
    for key, value in values.items():
        if key == 'debias':
            typed[key] = _coerce_debias(value)
        elif key == 'window':
            typed[key] = None if value in (None, '', 'none', 'null') else int(value)
        elif key == 'detect_seasonality' or key == 'persist' or key == 'dry_run':
            typed[key] = _coerce(value, bool)
        elif key in ('draws', 'seed', 'block_length', 'horizon', 'max_period',
                     'warmup', 'max_runtime_seconds', 'retries'):
            typed[key] = int(value)
        else:
            typed[key] = float(value)
    return Config(**typed)
