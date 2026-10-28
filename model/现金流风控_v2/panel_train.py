'''Leakage-controlled reference training; run only when a real dated panel exists.'''
from datetime import date
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, brier_score_loss,
                             confusion_matrix, roc_auc_score)
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from risk_model import array, number


def _logit(p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p)).reshape(-1, 1)


def train_panel(rows, feature_names, calibration_start, test_start,
                *, target, representative_calibration=False, estimator='logistic',
                false_negative_cost=9., false_positive_cost=1.,
                require_unseen_companies=True):
    '''Rows: company_id, as_of, available_at, label_end, label, features.

    Features must have been known at as_of. Row available_at is the LATEST
    availability date across its features. Dates are ISO YYYY-MM-DD.
    target must explicitly say future_cash_shortfall or future_ST.
    Evaluation is forward in time; unseen companies are enforced by default.
    No automatic deployment; returns (predictor, report).
    '''
    if target not in ('future_cash_shortfall', 'future_ST'):
        raise ValueError('explicit target required; neither target means fraud')
    if not feature_names or len(set(feature_names)) != len(feature_names):
        raise ValueError('unique explicit feature names required')
    c0, t0 = date.fromisoformat(calibration_start), date.fromisoformat(test_start)
    if c0 >= t0:
        raise ValueError('calibration must precede test')
    groups = [[], [], []]
    seen = set()
    dropped = 0
    for row in rows:
        asof = date.fromisoformat(row['as_of'])
        available = date.fromisoformat(row['available_at'])
        end = date.fromisoformat(row['label_end'])
        key = (row['company_id'], asof)
        if key in seen or not row['company_id']:
            raise ValueError('duplicate or empty company/as_of key')
        seen.add(key)
        if available > asof or end <= asof:
            raise ValueError('feature look-ahead or nonfuture target')
        label = number(row['label'])
        if label not in (0, 1):
            raise ValueError('binary labels required')
        split = 0 if asof < c0 else (1 if asof < t0 else 2)
        # Purge labels that mature inside the following split.
        cutoff = c0 if split == 0 else t0
        if split < 2 and end >= cutoff:
            dropped += 1
            continue
        x = array([row['features'][f] for f in feature_names], 'features')
        groups[split].append((row['company_id'], x, label, asof))
    if require_unseen_companies:
        ids = [set(r[0] for r in group) for group in groups]
        if ids[0] & ids[1] or ids[0] & ids[2] or ids[1] & ids[2]:
            raise ValueError('company overlap: supply disjoint cohorts or explicitly evaluate existing companies')
    for group in groups:
        if min(sum(r[2] == v for r in group) for v in (0, 1)) < 5:
            raise ValueError('each split needs >=5 positives and >=5 negatives; this is a mechanical minimum only')
    fncost, fpcost = number(false_negative_cost), number(false_positive_cost)
    if fncost <= 0 or fpcost <= 0:
        raise ValueError('positive error costs required')
    xy = [(np.vstack([r[1] for r in g]), np.array([r[2] for r in g])) for g in groups]
    if estimator == 'logistic':
        model = make_pipeline(SimpleImputer(strategy='median'), StandardScaler(),
                              LogisticRegression(C=1., max_iter=2000, random_state=42))
    elif estimator == 'rf':
        model = RandomForestClassifier(n_estimators=200, max_depth=4,
                                       min_samples_leaf=5, n_jobs=1, random_state=42)
    else:
        raise ValueError('estimator must be logistic or rf')
    model.fit(*xy[0])
    # Independent chronological sigmoid calibration. No OOB shortcuts.
    cal_raw = model.predict_proba(xy[1][0])[:, 1]
    calibrator = LogisticRegression(C=1., random_state=42).fit(_logit(cal_raw), xy[1][1])
    cal_p = calibrator.predict_proba(_logit(cal_raw))[:, 1]
    candidates = np.unique(np.r_[0, cal_p, 1])
    losses = [fncost * np.sum((cal_p < th) & (xy[1][1] == 1)) +
              fpcost * np.sum((cal_p >= th) & (xy[1][1] == 0)) for th in candidates]
    threshold = float(candidates[np.argmin(losses)])

    def predict(features):
        xx = array(features, 'features', 2)
        return calibrator.predict_proba(_logit(model.predict_proba(xx)[:, 1]))[:, 1]

    test_p = predict(xy[2][0])
    test_y = xy[2][1]
    cm = confusion_matrix(test_y, test_p >= threshold, labels=[0, 1])
    report = {'target': target, 'estimator': estimator,
              'probability_interpretation': ('representative-population calibrated candidate'
                  if representative_calibration else 'sample-specific calibrated score; not population probability'),
              'counts_train_calibration_test': [len(g) for g in groups],
              'purged_rows': dropped, 'feature_names': feature_names,
              'company_scope': 'unseen' if require_unseen_companies else 'existing companies allowed',
              'threshold_frozen_before_test': threshold,
              'roc_auc': float(roc_auc_score(test_y, test_p)),
              'average_precision': float(average_precision_score(test_y, test_p)),
              'brier': float(brier_score_loss(test_y, test_p)),
              'confusion_matrix_tn_fp_fn_tp': cm.ravel().tolist(),
              'test_prevalence': float(test_y.mean()),
              'status': 'candidate only; independent review and rolling real-data tests required'}
    return predict, report
