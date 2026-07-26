"""Calibration curve fitting, ported from equation_of_a_straight_line() /
calculations_deviations_and_concentrations() in calibration.ipynb.

Works on the plain point-dict representation stored in CalibrationCurve.points
(JSON: std_conc, area, is_area, response, included, ...) so it stays reusable
both for the initial auto-fit on upload and for the interactive re-fit
triggered from the review screen when a user toggles a point.
"""
from __future__ import annotations

import numpy as np

R2_OK_THRESHOLD = 0.98


class FitError(Exception):
    """Raised when a calibration line can't be fit from the included points."""


def auto_exclude_outliers(responses: list[float]) -> list[bool]:
    """Ported heuristic from the notebook: standards are expected to have a
    monotonically increasing response, so any point whose response drops more
    than ~3% versus the previous (sorted by Std.Conc) point breaks the run and
    gets provisionally excluded. This is only an initial suggestion — the
    reviewer can override any point on the review screen."""
    if len(responses) < 2:
        return [True] * len(responses)

    keep_indexes: list[int] = []
    for index, value in enumerate(responses):
        if index == 0:
            continue
        index_previous = index - 1
        value_previous = responses[index_previous]
        if not value:
            continue

        ratio = value_previous / value
        if ratio <= 1.03:
            keep_indexes.append(index_previous)
            keep_indexes.append(index)
        elif index_previous in keep_indexes:
            keep_indexes.remove(index_previous)
            keep_indexes.append(index)
        else:
            keep_indexes.append(index)

    keep = set(keep_indexes)
    return [i in keep for i in range(len(responses))]


def compute_response(area: float | None, is_area: float | None) -> float | None:
    """response = area / is_area — None if either value is missing (e.g. a
    manually added point whose Area/IS Area hasn't been filled in yet) or
    is_area is zero."""
    if area is None or not is_area:
        return None
    return area / is_area


def linear_fit(points: list[dict]) -> tuple[float, float, float | None]:
    """Fits response = k * std_conc + b on points with included=True and a
    computed response. Raises FitError (instead of the notebook's swallowed
    exceptions) so the caller can surface a concrete reason to the reviewer."""
    included = [p for p in points if p.get("included") and p.get("response") is not None]
    if len(included) < 2:
        raise FitError("Недостаточно точек для калибровки (нужно минимум 2 включённых точки).")

    x = np.array([p["std_conc"] for p in included], dtype=float)
    y = np.array([p["response"] for p in included], dtype=float)

    k, b = np.polyfit(x, y, 1)
    if k == 0:
        raise FitError("Наклон калибровочной прямой равен нулю — проверьте точки.")

    y_pred = k * x + b
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot != 0 else None

    return float(k), float(b), r2


def recompute(points: list[dict]) -> dict:
    """Recomputes k/b/r2 from the current include flags and refreshes each
    point's conc/dev_pct. Never raises — failures are reported in the "error"
    key so views can render them instead of crashing."""
    try:
        k, b, r2 = linear_fit(points)
        error = None
    except FitError as exc:
        k, b, r2 = None, None, None
        error = str(exc)

    updated_points = []
    for point in points:
        point = dict(point)
        if k is not None and point.get("response") is not None and point["std_conc"]:
            conc = (point["response"] - b) / k
            point["conc"] = conc
            point["dev_pct"] = conc / point["std_conc"] * 100
        else:
            point["conc"] = None
            point["dev_pct"] = None
        updated_points.append(point)

    return {
        "points": updated_points,
        "k": k,
        "b": b,
        "r2": r2,
        "error": error,
    }


def status_from_fit(k: float | None, r2: float | None, error: str | None) -> str:
    """Auto-classification used right after parsing: 'ok' curves can be
    bulk-approved, everything else needs a human look on the review screen."""
    if error or k is None:
        return "needs_review"
    if r2 is not None and r2 < R2_OK_THRESHOLD:
        return "needs_review"
    return "ok"
