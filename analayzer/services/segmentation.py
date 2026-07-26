"""Range-splitting for calibration curves — the "where do we cut a
calibration into 2+ concentration ranges" decision. Pure/no Django, testable
standalone from the repo root: `python -m analayzer.services.segmentation`.

The instrument's relative error is higher at low concentrations and lower at
high concentrations, so a single straight-line fit across the whole range can
undersell the low end and oversell the high end. find_breakpoints() is the
one function to implement: given the included calibration points (sorted by
std_conc), decide where — if anywhere — the range should be cut.

build_segments() is infra, already implemented: it turns your breakpoints
into concrete segments, each with its own k/b/r2 fit via fitting.linear_fit.
"""
from __future__ import annotations

import numpy as np

from . import fitting

# A segment needs at least this many points to get its own stable fit — also
# caps how deep the recursive split can go on a typical 6-10 point curve.
MIN_POINTS_PER_SEGMENT = 3

# A candidate split is only taken if it cuts the total relative error by at
# least this fraction versus the single whole-range fit. Without this, noise
# alone would justify splitting almost any curve into more and more segments.
IMPROVEMENT_THRESHOLD = 0.5


def _relative_sse(xs: list[float], ys: list[float]) -> float | None:
    """Sum of squared *relative* residuals of a one-line fit, i.e. residuals
    expressed as ((back-computed conc) - (true std_conc)) / std_conc instead
    of raw response units. This is the same %Dev the review screen already
    shows per point — using it (rather than raw response residuals) is what
    makes a low-concentration point that's off by 50% count as much as a
    high-concentration point that's off by 50%, even though its absolute
    response residual is tiny. That relative weighting is exactly what
    surfaces "the low end doesn't fit as well as the high end"."""
    if len(xs) < 2:
        return None

    x = np.array(xs, dtype=float)
    y = np.array(ys, dtype=float)
    k, b = np.polyfit(x, y, 1)
    if k == 0:
        return None

    nonzero = x != 0
    if not nonzero.any():
        return None

    conc_pred = (y[nonzero] - b) / k
    rel_residual = (conc_pred - x[nonzero]) / x[nonzero]
    return float(np.sum(rel_residual**2))


def _best_split(xs: list[float], ys: list[float]) -> tuple[int, float] | None:
    """Scans every candidate cut point (leaving >= MIN_POINTS_PER_SEGMENT on
    each side) and returns the split index + combined relative SSE for the
    best one, or None if no split has enough points to try."""
    n = len(xs)
    best_index = None
    best_sse = None
    for i in range(MIN_POINTS_PER_SEGMENT, n - MIN_POINTS_PER_SEGMENT + 1):
        low_sse = _relative_sse(xs[:i], ys[:i])
        high_sse = _relative_sse(xs[i:], ys[i:])
        if low_sse is None or high_sse is None:
            continue
        total = low_sse + high_sse
        if best_sse is None or total < best_sse:
            best_sse, best_index = total, i
    return None if best_index is None else (best_index, best_sse)


def _find_breakpoints_recursive(xs: list[float], ys: list[float]) -> list[float]:
    if len(xs) < 2 * MIN_POINTS_PER_SEGMENT:
        return []

    whole_sse = _relative_sse(xs, ys)
    if whole_sse is None:
        return []

    found = _best_split(xs, ys)
    if found is None:
        return []
    split_index, split_sse = found

    # Reject the split if it doesn't clearly beat one line across the whole
    # range — a small improvement is more likely noise than a real change in
    # instrument error, and would fragment the curve into unstable segments.
    if split_sse > whole_sse * (1 - IMPROVEMENT_THRESHOLD):
        return []

    breakpoint = (xs[split_index - 1] + xs[split_index]) / 2

    # Recurse on each side so a curve with 3+ real error regimes still gets
    # fully split, not just cut once.
    left = _find_breakpoints_recursive(xs[:split_index], ys[:split_index])
    right = _find_breakpoints_recursive(xs[split_index:], ys[split_index:])
    return left + [breakpoint] + right


def find_breakpoints(points: list[dict]) -> list[float]:
    """Finds where a calibration's concentration range should be cut so each
    piece gets its own line, because the instrument's *relative* error is
    higher at low concentrations and lower at high ones — a single
    whole-range fit gets pulled toward the high-concentration points (which
    dominate the raw least-squares error) and systematically undersells the
    low end.

    Approach: try every possible single cut point, fit each side separately,
    and score each candidate by the combined *relative* squared error (see
    _relative_sse) rather than raw R² — relative error is what actually
    matters here, since a low-concentration point being off by 50% and a
    high-concentration point being off by 50% should count the same, even
    though their raw response residuals are wildly different magnitudes.
    Keep the best cut only if it meaningfully beats a single line
    (IMPROVEMENT_THRESHOLD); recurse into each half so more than one real
    cut point gets found on richer datasets.

    points: calibration points for one compound, already filtered to
    included=True and response is not None, sorted ascending by std_conc.
    Each point dict has at least: std_conc (float), response (float).
    """
    xs = [p["std_conc"] for p in points]
    ys = [p["response"] for p in points]
    return _find_breakpoints_recursive(xs, ys)


def build_segments(points: list[dict], breakpoints: list[float]) -> list[dict]:
    """Infra helper (already implemented) — turns find_breakpoints()'s output
    into concrete segment dicts, each with its own linear fit. `points` must
    be the same included/response-filtered, std_conc-sorted list you passed
    to find_breakpoints().

    Returns a list of dicts, one per non-empty segment, in ascending
    std_conc order:
        {"conc_min": float, "conc_max": float, "k": float|None, "b": float|None,
         "r2": float|None, "error": str|None, "point_indices": [int, ...]}
    point_indices are indices into the `points` list passed in.
    """
    if not points:
        return [
            {
                "conc_min": 0.0,
                "conc_max": 0.0,
                "k": None,
                "b": None,
                "r2": None,
                "error": "Нет точек для фита.",
                "point_indices": [],
            }
        ]

    bounds = sorted(breakpoints)
    buckets: list[list[int]] = [[] for _ in range(len(bounds) + 1)]

    for i, point in enumerate(points):
        conc = point["std_conc"]
        bucket_index = 0
        while bucket_index < len(bounds) and conc > bounds[bucket_index]:
            bucket_index += 1
        buckets[bucket_index].append(i)

    segments = []
    for bucket in buckets:
        if not bucket:
            continue
        bucket_points = [points[i] for i in bucket]
        conc_min = min(p["std_conc"] for p in bucket_points)
        conc_max = max(p["std_conc"] for p in bucket_points)
        try:
            k, b, r2 = fitting.linear_fit(
                [{"included": True, "response": p["response"], "std_conc": p["std_conc"]} for p in bucket_points]
            )
            error = None
        except fitting.FitError as exc:
            k, b, r2 = None, None, None
            error = str(exc)
        segments.append(
            {
                "conc_min": conc_min,
                "conc_max": conc_max,
                "k": k,
                "b": b,
                "r2": r2,
                "error": error,
                "point_indices": bucket,
            }
        )
    return segments


if __name__ == "__main__":
    # Two true regimes glued together: response is more scattered relative
    # to std_conc at the low end (0.05-0.5) than at the high end (1-10) —
    # exactly the "low points noisier than high points" situation this
    # module exists for. A single whole-range fit gets R²=0.957; splitting
    # at the point this script finds should push both halves above 0.998.
    toy_points = [
        {"std_conc": 0.05, "response": 0.017},
        {"std_conc": 0.1, "response": 0.035},
        {"std_conc": 0.2, "response": 0.075},
        {"std_conc": 0.5, "response": 0.21},
        {"std_conc": 1.0, "response": 0.58},
        {"std_conc": 2.0, "response": 1.28},
        {"std_conc": 5.0, "response": 3.05},
        {"std_conc": 10.0, "response": 6.02},
    ]
    bps = find_breakpoints(toy_points)
    print("breakpoints:", bps)
    for seg in build_segments(toy_points, bps):
        print(seg)
