'''Rolling walk-forward cash-flow forecasting.

Public entry points:
    load_config          build a run configuration
    validate_payload     check an input payload against the contract
    run_pipeline         run validate -> features -> backtest -> forecast -> persist
'''
from .config import Config, load_config
from .core import (
    HORIZON,
    BacktestResult,
    RollingState,
    Seasonality,
    detect_seasonality,
    resolve_bias,
    select_block_length,
    simulate_paths,
    summarise_paths,
    walk_forward_backtest,
)
from .pipeline import StageError, run_pipeline
from .validate import ValidationError, ValidationReport, validate_payload

__all__ = [
    'HORIZON', 'Config', 'load_config', 'run_pipeline', 'StageError',
    'validate_payload', 'ValidationError', 'ValidationReport',
    'detect_seasonality', 'select_block_length', 'resolve_bias',
    'simulate_paths', 'summarise_paths', 'walk_forward_backtest',
    'RollingState', 'BacktestResult', 'Seasonality',
]
