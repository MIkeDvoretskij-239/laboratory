"""Unknown-sample resolution — given one real measurement's response and
every approved calibration segment available for that compound (across all
materials), decide which segment it belongs to and back-compute its
concentration. Pure/no Django, testable standalone from the repo root:
`python -m analayzer.services.sample_resolution`.
"""
from __future__ import annotations


def resolve(response: float, candidates: list[dict]) -> dict:
    """Picks which calibration segment a sample's response belongs to and
    back-computes its concentration.

    The check is self-consistency: for each candidate segment, back-compute
    conc = (response - b) / k using *that segment's own* k/b, then check
    whether the result actually falls inside *that same segment's* own
    conc_min/conc_max. A segment can only be a real match for this response
    if its own calibration agrees that the resulting concentration belongs
    to its own range — a segment whose line happens to pass near this
    response but at a concentration outside what it was ever calibrated for
    isn't a match, it's an extrapolation.

    Three outcomes:
    - Exactly one segment matches itself → clean "matched".
    - Zero segments match themselves → "out_of_range", but still report the
      least-wrong candidate (smallest distance from its own range) as a
      best-effort number, since a value just past a boundary is still
      informative for a reviewer even if it shouldn't be trusted blindly.
    - More than one segment matches (e.g. two materials' ranges both claim
      this response) → can't be resolved automatically without knowing which
      material the sample is actually in, so this is handed to a human as
      "needs_review" rather than silently guessing; the narrowest matching
      range is reported as the working guess since it's the most specific
      calibration available.

    response: float — the sample's computed response (area / is_area).
    candidates: every APPROVED calibration range on record for this
    sample's compound — both per-material CalibrationSegments and any
    pooled MergedSegments, across all materials/raw_uploads:
        [{"segment_id": int, "kind": "segment"|"merged", "curve_id": int|None,
          "material_label": str, "k": float, "b": float,
          "conc_min": float, "conc_max": float}, ...]
    "kind" distinguishes the two id spaces (a CalibrationSegment and a
    MergedSegment can share the same numeric id) — always echoed back
    alongside matched_segment_id so the caller knows which table it's from.

    Return:
        {
            "matched_segment_id": int | None,
            "matched_kind": "segment" | "merged" | None,
            "computed_conc": float | None,
            "status": "matched" | "out_of_range" | "needs_review" | "no_calibration",
            "details": dict,
        }
    """
    scored = []
    for candidate in candidates:
        k, b = candidate.get("k"), candidate.get("b")
        if not k:
            continue

        conc = (response - b) / k
        conc_min, conc_max = candidate["conc_min"], candidate["conc_max"]

        if conc < conc_min:
            distance = conc_min - conc
        elif conc > conc_max:
            distance = conc - conc_max
        else:
            distance = 0.0

        scored.append(
            {
                "segment_id": candidate["segment_id"],
                "kind": candidate.get("kind", "segment"),
                "curve_id": candidate.get("curve_id"),
                "material_label": candidate.get("material_label"),
                "conc_min": conc_min,
                "conc_max": conc_max,
                "computed_conc": conc,
                "in_range": distance == 0.0,
                "distance": distance,
            }
        )

    if not scored:
        return {
            "matched_segment_id": None,
            "matched_kind": None,
            "computed_conc": None,
            "status": "no_calibration",
            "details": {"reason": "Ни у одного кандидата нет посчитанного наклона (k)."},
        }

    matches = [s for s in scored if s["in_range"]]

    if len(matches) == 1:
        match = matches[0]
        return {
            "matched_segment_id": match["segment_id"],
            "matched_kind": match["kind"],
            "computed_conc": match["computed_conc"],
            "status": "matched",
            "details": {"considered": scored},
        }

    if len(matches) > 1:
        # Several ranges' own bounds all agree with themselves for this
        # response — usually because more than one material's calibration
        # (or a merged range alongside its own source materials) was
        # approved for this compound. Without knowing which material this
        # sample is actually in, guessing silently would be wrong more often
        # than it's right, so this goes to a human; the narrowest range is
        # surfaced as the default guess since a tighter calibrated range is
        # generally the more trustworthy one.
        chosen = min(matches, key=lambda s: s["conc_max"] - s["conc_min"])
        return {
            "matched_segment_id": chosen["segment_id"],
            "matched_kind": chosen["kind"],
            "computed_conc": chosen["computed_conc"],
            "status": "needs_review",
            "details": {
                "reason": "Несколько диапазонов одновременно подходят — нужна проверка человеком.",
                "considered": scored,
            },
        }

    # Nothing matched itself — report the closest miss as a best-effort
    # extrapolated number rather than leaving the reviewer with nothing.
    closest = min(scored, key=lambda s: s["distance"])
    return {
        "matched_segment_id": closest["segment_id"],
        "matched_kind": closest["kind"],
        "computed_conc": closest["computed_conc"],
        "status": "out_of_range",
        "details": {"considered": scored},
    }


if __name__ == "__main__":
    candidates = [
        {
            "segment_id": 1,
            "curve_id": 10,
            "material_label": "H2O",
            "k": 0.62,
            "b": 0.01,
            "conc_min": 0.0,
            "conc_max": 1.0,
        },
        {
            "segment_id": 2,
            "curve_id": 10,
            "material_label": "H2O",
            "k": 0.60,
            "b": 0.05,
            "conc_min": 1.0,
            "conc_max": 10.0,
        },
    ]
    print("matched:      ", resolve(0.63, candidates))  # in segment 1's own range
    print("out of range: ", resolve(50.0, candidates))  # no segment agrees with itself
    overlapping_candidates = [candidates[0], {**candidates[1], "conc_min": 0.0}]
    print("needs review: ", resolve(0.63, overlapping_candidates))  # both ranges now agree
