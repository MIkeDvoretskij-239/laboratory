"""Cross-material calibration equivalence — a user-triggered comparison of
two calibration curves (usually the same compound calibrated in two
different materials/matrices, e.g. H2O vs Plazma). Pure/no Django, testable
standalone from the repo root: `python -m analayzer.services.equivalence`.

Two calibrations for the same compound can differ in intercept (a matrix
offset) while having a near-identical slope — in that case one calibration's
concentration axis can substitute for the other's over some sub-range. This
module only computes the numbers; services.processing wires the result into
a CalibrationEquivalence row for display/persistence.
"""
from __future__ import annotations

# Two lines with near-identical slope but different intercept describe the
# same underlying chemistry with a fixed matrix offset — a background signal
# or extraction-efficiency difference between the two materials that adds/
# subtracts a roughly constant amount, not something that scales with
# concentration. SLOPE_TOLERANCE is how close k_a and k_b must be (relative)
# for that "same slope" reading to hold at all.
SLOPE_TOLERANCE = 0.15

# Once slopes are judged equal, CONC_TOLERANCE is how far apart the two
# lines' predicted response is allowed to be (relative to curve A's
# response) at a given concentration for that concentration to count as
# "safe to substitute one calibration for the other".
CONC_TOLERANCE = 0.15

# How many points to scan across the overlapping concentration range when
# looking for the substitutable sub-range. The two lines' relative
# difference is a smooth function of x, so a moderately fine grid is enough
# to find its threshold crossing without needing calculus.
SCAN_STEPS = 200


def compare_curves(curve_a: dict, curve_b: dict) -> dict:
    """Compares two calibrations (usually the same compound on two
    materials) for equivalence.

    Two ways this can fail to be a simple "yes equivalent / no":
    1. The slopes themselves differ beyond SLOPE_TOLERANCE — the two
       materials genuinely respond differently per unit concentration, not
       just by a fixed offset, so no single substitution range exists.
    2. The slopes agree, but the fixed intercept difference (b_a vs b_b)
       still matters at low concentrations — near x=0 the offset is a large
       fraction of a small response, so the two curves disagree there even
       though they converge at higher concentrations where the signal
       dwarfs the offset. That's exactly the "with which concentrations can
       I use curve A instead of curve B" question — this function scans the
       overlap of both curves' calibrated ranges and reports the sub-range
       where the two lines' predicted response stays within CONC_TOLERANCE
       of each other.
    """
    ka, ba = curve_a.get("k"), curve_a.get("b")
    kb, bb = curve_b.get("k"), curve_b.get("b")

    if ka is None or ba is None or kb is None or bb is None:
        return {
            "is_equivalent": None,
            "valid_conc_min": None,
            "valid_conc_max": None,
            "notes": "У одной из кривых нет фита (k/b не посчитаны) — сравнивать нечего.",
            "details": {},
        }

    if ka == 0 or kb == 0:
        return {
            "is_equivalent": None,
            "valid_conc_min": None,
            "valid_conc_max": None,
            "notes": "У одной из кривых наклон равен нулю — сравнение не имеет смысла.",
            "details": {},
        }

    slope_ratio = kb / ka
    slopes_close = abs(slope_ratio - 1) <= SLOPE_TOLERANCE

    xs_a = [p["std_conc"] for p in curve_a.get("points", []) if p.get("included")]
    xs_b = [p["std_conc"] for p in curve_b.get("points", []) if p.get("included")]

    details = {
        "slope_ratio": slope_ratio,
        "slope_tolerance": SLOPE_TOLERANCE,
        "conc_tolerance": CONC_TOLERANCE,
    }

    if not xs_a or not xs_b:
        return {
            "is_equivalent": slopes_close or None,
            "valid_conc_min": None,
            "valid_conc_max": None,
            "notes": "У одной из кривых нет откалиброванных точек — диапазон определить не из чего.",
            "details": details,
        }

    overlap_lo = max(min(xs_a), min(xs_b))
    overlap_hi = min(max(xs_a), max(xs_b))
    details["overlap_range"] = [overlap_lo, overlap_hi]

    if overlap_hi <= overlap_lo:
        return {
            "is_equivalent": False,
            "valid_conc_min": None,
            "valid_conc_max": None,
            "notes": "Диапазоны концентраций двух калибровок не пересекаются — сравнивать не на чем.",
            "details": details,
        }

    if not slopes_close:
        return {
            "is_equivalent": False,
            "valid_conc_min": None,
            "valid_conc_max": None,
            "notes": (
                f"Наклоны отличаются на {abs(slope_ratio - 1) * 100:.1f}%, что больше допуска "
                f"{SLOPE_TOLERANCE * 100:.0f}% — это разная чувствительность, а не просто сдвиг "
                f"по матрице, менять одну калибровку на другую нельзя ни на каком участке."
            ),
            "details": details,
        }

    step = (overlap_hi - overlap_lo) / SCAN_STEPS
    valid_xs = []
    for i in range(SCAN_STEPS + 1):
        x = overlap_lo + step * i
        response_a = ka * x + ba
        response_b = kb * x + bb
        if response_a == 0:
            continue
        relative_diff = abs(response_a - response_b) / abs(response_a)
        if relative_diff <= CONC_TOLERANCE:
            valid_xs.append(x)

    if not valid_xs:
        return {
            "is_equivalent": False,
            "valid_conc_min": None,
            "valid_conc_max": None,
            "notes": (
                f"Наклоны близки ({abs(slope_ratio - 1) * 100:.1f}% разницы), но сдвиг по "
                f"интерсепту слишком велик — на всём пересечении диапазонов расхождение "
                f"превышает {CONC_TOLERANCE * 100:.0f}%."
            ),
            "details": details,
        }

    valid_conc_min, valid_conc_max = min(valid_xs), max(valid_xs)
    return {
        "is_equivalent": True,
        "valid_conc_min": valid_conc_min,
        "valid_conc_max": valid_conc_max,
        "notes": (
            f"Наклоны совпадают в пределах {abs(slope_ratio - 1) * 100:.1f}%. Калибровку "
            f"«{curve_a.get('label', 'A')}» можно заменить на «{curve_b.get('label', 'B')}» "
            f"(и наоборот) для концентраций {valid_conc_min:.3g}–{valid_conc_max:.3g}."
        ),
        "details": details,
    }


if __name__ == "__main__":
    # Near-identical slope (0.62 vs 0.60), different intercept — a matrix
    # offset that should matter at std_conc=1 but wash out by std_conc=10.
    curve_a = {
        "label": "H2O",
        "k": 0.62,
        "b": 0.01,
        "points": [
            {"std_conc": 1, "response": 0.63, "included": True},
            {"std_conc": 5, "response": 3.11, "included": True},
            {"std_conc": 10, "response": 6.21, "included": True},
        ],
    }
    curve_b = {
        "label": "Plazma",
        "k": 0.60,
        "b": 0.15,
        "points": [
            {"std_conc": 1, "response": 0.75, "included": True},
            {"std_conc": 5, "response": 3.15, "included": True},
            {"std_conc": 10, "response": 6.05, "included": True},
        ],
    }
    print(compare_curves(curve_a, curve_b))
