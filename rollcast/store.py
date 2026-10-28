'''MySQL persistence for rolling-forecast runs.

Follows the project's existing storage rules: the target database must carry
the `cfqa_` prefix (the isolation guard that keeps synthetic results out of
production), writes are idempotent on `run_id`, and every write is read back
and compared before the run is declared committed.
'''
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any

DDL_RUNS = """
CREATE TABLE IF NOT EXISTS cf_rollcast_runs_v1 (
  run_id              VARCHAR(64)  NOT NULL,
  config_fingerprint  VARCHAR(32)  NOT NULL,
  input_sha256        CHAR(64)     NOT NULL,
  status              VARCHAR(24)  NOT NULL,
  unit                VARCHAR(32)  NOT NULL,
  origin_count        INT          NOT NULL DEFAULT 0,
  history_weeks       INT          NOT NULL DEFAULT 0,
  block_used          INT              NULL,
  block_rationale     VARCHAR(255)     NULL,
  initial_capacity    DECIMAL(20,4)    NULL,
  probability_shortfall DECIMAL(12,8)  NULL,
  gap_p95             DECIMAL(20,4)    NULL,
  gap_cvar95          DECIMAL(20,4)    NULL,
  mae                 DECIMAL(20,6)    NULL,
  rmse                DECIMAL(20,6)    NULL,
  terminal_drift      DECIMAL(20,6)    NULL,
  payload             JSON         NOT NULL,
  created_at          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (run_id),
  KEY idx_created (created_at),
  KEY idx_fingerprint (config_fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

DDL_ERRORS = """
CREATE TABLE IF NOT EXISTS cf_rollcast_errors_v1 (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  run_id      VARCHAR(64)  NOT NULL,
  code        VARCHAR(48)  NOT NULL,
  message     VARCHAR(512) NOT NULL,
  path        VARCHAR(128)     NULL,
  stage       VARCHAR(32)  NOT NULL,
  created_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (id),
  KEY idx_run (run_id),
  KEY idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


class StoreError(RuntimeError):
    '''Storage failure. `permanent` marks errors that retrying cannot fix.'''

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def _connect():
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - deployment guard
        raise StoreError('pymysql is not installed; install it or run with --no-persist') from exc

    cfg = {
        'host': os.environ.get('CF_DB_HOST', '127.0.0.1'),
        'port': int(os.environ.get('CF_DB_PORT', '3306')),
        'user': os.environ.get('CF_DB_USER', ''),
        'password': os.environ.get('CF_DB_PASSWORD', ''),
        'database': os.environ.get('CF_DB_NAME', ''),
        'charset': 'utf8mb4',
        'autocommit': False,
    }
    if not cfg['database']:
        raise StoreError('CF_DB_NAME is not set')
    # The project's isolation rule: synthetic results may only land in cfqa_*.
    if not cfg['database'].startswith('cfqa_'):
        raise StoreError(
            f"CF_DB_NAME={cfg['database']!r} must start with 'cfqa_'; "
            'the isolation guard forbids writing synthetic results elsewhere')
    try:
        return pymysql.connect(**cfg)
    except Exception as exc:
        raise StoreError(f'database connection failed: {exc}') from exc


def ensure_schema() -> None:
    '''Create the tables if absent. Safe to call on every run.'''
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(DDL_RUNS)
            cur.execute(DDL_ERRORS)
        conn.commit()
    finally:
        conn.close()


def _errno(exc: Exception) -> int | None:
    '''Extract the MySQL error number.

    pymysql raises `IntegrityError(1062, "Duplicate entry ...")` without
    setting an `errno` attribute, so the number has to come from args[0].
    Reading only the attribute would silently classify every error as
    retryable and turn a duplicate key into three pointless sleeps.
    '''
    value = getattr(exc, 'errno', None)
    if isinstance(value, int):
        return value
    args = getattr(exc, 'args', ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _json_equal(a: Any, b: Any, rel: float = 1e-12, tol: float = 1e-15) -> bool:
    '''Structural comparison that tolerates float representation drift.

    MySQL stores JSON numbers as DOUBLE and re-serialises them, so a value
    written as 0.9127583004940999 comes back as 0.9127583004941. Requiring
    bit-identical floats would make every run fail its own consistency check;
    the meaningful guarantee is that the structure and the values agree to
    well within double precision. Booleans and strings must still match
    exactly, and no key may be added or dropped.
    '''
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=tol)
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return set(a) == set(b) and all(_json_equal(a[k], b[k], rel, tol) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(
            _json_equal(x, y, rel, tol) for x, y in zip(a, b))
    return a == b


def _is_duplicate(exc: Exception) -> bool:
    return _errno(exc) in (1062, 1586)


def _classify(exc: Exception) -> StoreError:
    '''Turn a driver error into a StoreError, flagging the un-retryable ones.'''
    code = _errno(exc)
    if code == 1146:
        return StoreError(
            'table missing: run `--init-db` once with the admin account '
            '(the application account is deliberately not allowed to create tables)',
            permanent=True)
    if code == 1142:
        return StoreError(
            f'permission denied: {exc}. The runtime account only needs SELECT/INSERT; '
            'DDL belongs to the admin account at deploy time',
            permanent=True)
    if code in (1044, 1045, 2003, 2005, 2013):
        return StoreError(f'connection/auth failure: {exc}')
    return StoreError(f'database error: {exc}')


def save_run(record: dict[str, Any]) -> dict[str, Any]:
    '''Insert a run, then read it back and verify byte-for-byte consistency.

    Schema creation is deliberately NOT attempted here. At runtime the service
    runs under an account that only holds SELECT/INSERT; DDL is a one-off
    deploy step (`--init-db`) performed with the admin account. Trying to
    create tables on every run would both violate least privilege and turn a
    permanent misconfiguration into three pointless retries.

    Returns a receipt describing what was stored and whether the read-back
    matched. A mismatch is raised, not logged and ignored.
    '''
    payload = json.dumps(record.get('payload', {}), ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'))
    row = (
        record['run_id'], record['config_fingerprint'], record['input_sha256'],
        record['status'], record['unit'], int(record.get('origin_count', 0)),
        int(record.get('history_weeks', 0)), record.get('block_used'),
        (record.get('block_rationale') or '')[:255], record.get('initial_capacity'),
        record.get('probability_shortfall'), record.get('gap_p95'),
        record.get('gap_cvar95'), record.get('mae'), record.get('rmse'),
        record.get('terminal_drift'), payload,
    )
    expected = json.loads(payload)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    'INSERT INTO cf_rollcast_runs_v1 (run_id, config_fingerprint, input_sha256, '
                    'status, unit, origin_count, history_weeks, block_used, block_rationale, '
                    'initial_capacity, probability_shortfall, gap_p95, gap_cvar95, mae, rmse, '
                    'terminal_drift, payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    row)
            except Exception as exc:
                conn.rollback()
                if _is_duplicate(exc):
                    return {'run_id': record['run_id'], 'status': 'duplicate_ignored',
                            'verified': None}
                raise _classify(exc) from exc

            for item in record.get('errors', []):
                cur.execute(
                    'INSERT INTO cf_rollcast_errors_v1 (run_id, code, message, path, stage) '
                    'VALUES (%s,%s,%s,%s,%s)',
                    (record['run_id'], item.get('code', 'UNKNOWN'),
                     (item.get('message') or '')[:512], (item.get('path') or '')[:128],
                     item.get('stage', 'unknown')[:32]))

            # Verify inside the same transaction, before committing. Reading
            # back only after COMMIT would mean a mismatch is already durable:
            # the row would linger and the retry would hit a duplicate key.
            cur.execute(
                'SELECT payload, status, block_used FROM cf_rollcast_runs_v1 '
                'WHERE run_id=%s', (record['run_id'],))
            got = cur.fetchone()
            if got is None:
                conn.rollback()
                raise StoreError(
                    f'read-back failed: run {record["run_id"]} not found after insert')
            raw = got[0] if isinstance(got[0], str) else got[0].decode('utf-8')
            # Compare the parsed structure, not the serialised bytes: MySQL
            # normalises key order and whitespace, and re-emits numbers through
            # a DOUBLE. _json_equal allows that last, unavoidable drift.
            stored = json.loads(raw)
            payload_ok = _json_equal(stored, expected)
            status_ok = got[1] == record['status']
            if not (payload_ok and status_ok):
                conn.rollback()
                raise StoreError(
                    f'read-back mismatch for run {record["run_id"]}: '
                    f'payload_ok={payload_ok} status_ok={status_ok}')
            stored_block = got[2]

        conn.commit()
        return {
            'run_id': record['run_id'], 'status': 'committed', 'verified': True,
            'payload_bytes': len(payload), 'stored_status': record['status'],
            'stored_block': stored_block,
        }
    except StoreError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise _classify(exc) from exc
    finally:
        conn.close()


def fetch_run(run_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT run_id, status, payload, created_at FROM cf_rollcast_runs_v1 '
                        'WHERE run_id=%s', (run_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    payload = row[2] if isinstance(row[2], str) else row[2].decode('utf-8')
    return {'run_id': row[0], 'status': row[1],
            'payload': json.loads(payload),
            'created_at': row[3].isoformat() if isinstance(row[3], datetime) else str(row[3])}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
