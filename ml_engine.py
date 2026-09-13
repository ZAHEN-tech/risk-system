"""
Machine-learning alternative to the rule-based risk score.

Where risk_engine.py encodes a hand-picked weighted formula, this module
trains a logistic regression on *historical outcomes* — i.e. past
enrollments where we later learned whether the student actually failed or
withdrew — and learns its own weighting of the same four signals:
attendance %, assessment average, submission rate, and performance trend.

In a real deployment, `generate_historical_dataset()` would be replaced by
a query against the institution's actual historical records (at least one
full term of completed outcomes). Everything downstream — training,
scoring, and explanation — works unchanged once real data is plugged in.

The model is intentionally simple (logistic regression, 4 features) rather
than a deeper model, because in an early-warning-for-students context a
model that a non-technical registrar or dean can sanity-check and explain
to a parent is worth more than a marginal accuracy gain from a black box.
"""
import os
import json
import numpy as np
from joblib import dump, load
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    roc_auc_score, confusion_matrix
)

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ml_artifacts")
os.makedirs(MODEL_DIR, exist_ok=True)
MODEL_PATH = os.path.join(MODEL_DIR, "logreg_model.joblib")
SCALER_PATH = os.path.join(MODEL_DIR, "scaler.joblib")
METRICS_PATH = os.path.join(MODEL_DIR, "metrics.json")

FEATURES = ["attendance_pct", "assessment_avg", "submission_rate", "trend_penalty"]
FEATURE_LABELS = {
    "attendance_pct": "Attendance",
    "assessment_avg": "Assessment average",
    "submission_rate": "Submission rate",
    "trend_penalty": "Declining trend",
}

ML_BAND_HIGH = 0.60
ML_BAND_MEDIUM = 0.30


def generate_historical_dataset(n=3000, seed=42):
    """Synthetic stand-in for several past terms of completed enrollments with
    known outcomes. The generating process is deliberately NOT the same
    formula as risk_engine.py's rule-based weights (different influence per
    feature, plus an interaction term), so the trained model has to learn
    its own weighting from data rather than just recover the hand-tuned rule."""
    rng = np.random.default_rng(seed)
    attendance = np.clip(rng.normal(78, 18, n), 0, 100)
    assessment = np.clip(rng.normal(62, 20, n) - 0.15 * (100 - attendance), 0, 100)
    submission = np.clip(rng.normal(85, 15, n) - 0.10 * (100 - attendance), 0, 100)
    trend = np.clip(rng.normal(15, 20, n), 0, 100)

    logit = (
        -4.2
        + 0.038 * (100 - attendance)
        + 0.045 * (100 - assessment)
        + 0.018 * (100 - submission)
        + 0.012 * trend
        + 0.0006 * (100 - attendance) * (100 - assessment) / 10  # absent AND struggling compounds
    )
    prob = 1 / (1 + np.exp(-logit))
    outcome = (rng.random(n) < prob).astype(int)

    X = np.column_stack([attendance, assessment, submission, trend])
    return X, outcome


def train(n=3000, seed=42):
    X, y = generate_historical_dataset(n=n, seed=seed)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=seed, stratify=y
    )

    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    model = LogisticRegression(max_iter=1000)
    model.fit(X_train_s, y_train)

    y_pred = model.predict(X_test_s)
    y_proba = model.predict_proba(X_test_s)[:, 1]

    metrics = {
        "trained_on": n,
        "test_size": len(y_test),
        "base_rate": round(float(y.mean()), 3),
        "accuracy": round(float(accuracy_score(y_test, y_pred)), 3),
        "precision": round(float(precision_score(y_test, y_pred)), 3),
        "recall": round(float(recall_score(y_test, y_pred)), 3),
        "f1": round(float(f1_score(y_test, y_pred)), 3),
        "roc_auc": round(float(roc_auc_score(y_test, y_proba)), 3),
        "confusion_matrix": confusion_matrix(y_test, y_pred).tolist(),  # [[TN, FP], [FN, TP]]
        "coefficients": {f: round(float(c), 3) for f, c in zip(FEATURES, model.coef_[0])},
        "intercept": round(float(model.intercept_[0]), 3),
    }

    dump(model, MODEL_PATH)
    dump(scaler, SCALER_PATH)
    with open(METRICS_PATH, "w") as fh:
        json.dump(metrics, fh, indent=2)

    return metrics


def _load():
    if not (os.path.exists(MODEL_PATH) and os.path.exists(SCALER_PATH)):
        train()
    return load(MODEL_PATH), load(SCALER_PATH)


def get_metrics():
    if not os.path.exists(METRICS_PATH):
        train()
    with open(METRICS_PATH) as fh:
        return json.load(fh)


def predict(attendance_pct, assessment_avg, submission_rate, trend_penalty):
    model, scaler = _load()
    x = np.array([[attendance_pct, assessment_avg, submission_rate, trend_penalty]])
    proba = float(model.predict_proba(scaler.transform(x))[0, 1])
    score = round(proba * 100, 1)
    if proba >= ML_BAND_HIGH:
        band = "High"
    elif proba >= ML_BAND_MEDIUM:
        band = "Medium"
    else:
        band = "Low"
    return score, band


def explain(attendance_pct, assessment_avg, submission_rate, trend_penalty, top_n=4):
    """Per-student explanation: each feature's standardized contribution to the log-odds,
    so a counsellor or student can see which signal is driving the score."""
    model, scaler = _load()
    x = np.array([[attendance_pct, assessment_avg, submission_rate, trend_penalty]])
    x_s = scaler.transform(x)[0]
    contributions = x_s * model.coef_[0]
    items = [
        {"feature": FEATURE_LABELS[f], "contribution": round(float(c), 3)}
        for f, c in zip(FEATURES, contributions)
    ]
    items.sort(key=lambda d: abs(d["contribution"]), reverse=True)
    return items[:top_n]
