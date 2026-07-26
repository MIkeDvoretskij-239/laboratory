"""Bridges the pure parsing/fitting services to the Django models. This is the
only module under services/ that imports models — parsing.py and fitting.py
stay framework-free and independently testable.
"""
from __future__ import annotations

import pandas as pd
from django.core.files.base import ContentFile

from ..models import (
    CalibrationCurve,
    CalibrationSegment,
    Compound,
    MergedSegment,
    SampleResult,
    SampleResultsExport,
)
from . import fitting, parsing, sample_excel_export, sample_resolution

# `segmentation` (automatic range-splitting) is no longer used — see the
# commented-out segment_curve() below.
# from . import segmentation


# Automatic range-splitting is disabled — see the "закомментировал
# автоматическое разделение калибровки на диапазоны" request. Kept here
# (commented) instead of deleted so it can be restored later. Ranges are now
# only ever created by hand via add_manual_segment() / curve_segment_create.
# def segment_curve(curve: CalibrationCurve) -> None:
#     """Runs services.segmentation on a curve's included points and persists
#     the result as CalibrationSegment rows, tagging each point in
#     curve.points with which segment it landed in (or None if excluded /
#     unfitted). Raises whatever segmentation.find_breakpoints() raises —
#     including NotImplementedError before it's written — so callers that
#     shouldn't crash on that (e.g. the auto-run right after upload) must
#     catch it themselves."""
#     indexed_points = [
#         (i, point)
#         for i, point in enumerate(curve.points)
#         if point.get("included") and point.get("response") is not None
#     ]
#     indexed_points.sort(key=lambda pair: pair[1]["std_conc"])
#     original_indices = [i for i, _ in indexed_points]
#     filtered_points = [point for _, point in indexed_points]
#
#     breakpoints = segmentation.find_breakpoints(filtered_points)
#     segments = segmentation.build_segments(filtered_points, breakpoints)
#
#     points = [dict(p) for p in curve.points]
#     for point in points:
#         point["segment"] = None
#
#     curve.segments.all().delete()
#     for index, seg in enumerate(segments):
#         status = fitting.status_from_fit(seg["k"], seg["r2"], seg["error"])
#         CalibrationSegment.objects.create(
#             curve=curve,
#             index=index,
#             conc_min=seg["conc_min"],
#             conc_max=seg["conc_max"],
#             k=seg["k"],
#             b=seg["b"],
#             r2=seg["r2"],
#             fit_error=seg["error"] or "",
#             status=status,
#         )
#         for local_index in seg["point_indices"]:
#             points[original_indices[local_index]]["segment"] = index
#
#     curve.points = points
#     curve.save(update_fields=["points"])


def add_manual_segment(
    curve: CalibrationCurve,
    conc_min: float | None = None,
    conc_max: float | None = None,
    point_indices: list[int] | None = None,
) -> CalibrationSegment:
    """Adds one CalibrationSegment on top of whatever ranges already exist —
    for carving out a range the reviewer wants to isolate.

    point_indices (required) picks exactly which points feed the fit —
    indices into curve.points, regardless of their own "included" flag,
    since picking a point here is itself a deliberate override.
    conc_min/conc_max default to that selection's own std_conc range when
    not given explicitly, but can still be widened/narrowed by hand for
    extrapolation.

    There is deliberately no range-only mode ("every included point whose
    std_conc falls in [conc_min, conc_max]") — a range is always built from
    an explicit set of picked points, never implied from a numeric bound.
    """
    matching_indices = [
        i for i in point_indices if 0 <= i < len(curve.points) and curve.points[i].get("response") is not None
    ]

    fit_points = [
        {"included": True, "response": curve.points[i]["response"], "std_conc": curve.points[i]["std_conc"]}
        for i in matching_indices
    ]

    if conc_min is None or conc_max is None:
        stds = [curve.points[i]["std_conc"] for i in matching_indices]
        conc_min = min(stds) if stds else 0.0
        conc_max = max(stds) if stds else 0.0

    try:
        k, b, r2 = fitting.linear_fit(fit_points)
        error = None
    except fitting.FitError as exc:
        k, b, r2 = None, None, None
        error = str(exc)

    existing_indices = list(curve.segments.values_list("index", flat=True))
    next_index = max(existing_indices) + 1 if existing_indices else 0

    segment = CalibrationSegment.objects.create(
        curve=curve,
        index=next_index,
        conc_min=conc_min,
        conc_max=conc_max,
        k=k,
        b=b,
        r2=r2,
        fit_error=error or "",
        status=fitting.status_from_fit(k, r2, error),
    )

    points = [dict(p) for p in curve.points]
    for i in matching_indices:
        points[i]["segment"] = next_index
    curve.points = points
    curve.save(update_fields=["points"])

    return segment


def delete_segment(segment: CalibrationSegment) -> None:
    """Deletes one segment and un-tags whichever of its curve's points
    pointed to it, without touching the curve's other segments."""
    curve = segment.curve
    points = [dict(p) for p in curve.points]
    for point in points:
        if point.get("segment") == segment.index:
            point["segment"] = None
    curve.points = points
    curve.save(update_fields=["points"])
    segment.delete()


def create_merged_segment(
    compound,
    point_refs: list[tuple],
    conc_min: float | None = None,
    conc_max: float | None = None,
    label: str = "",
    created_by=None,
):
    """Pools an explicit hand-picked set of points from different curves
    (usually different materials for the same compound) into one combined
    fit — a single, more data-dense range to resolve real samples against
    instead of picking one material's often-sparser segment.

    point_refs: list of (CalibrationCurve, point_index) pairs — exactly
    which points to pool, picked by the reviewer rather than auto-selected
    by a numeric range, so an outlier can be left out even if it falls
    inside the boundary. `curves` on the resulting MergedSegment is derived
    as the distinct set of curves referenced here.

    conc_min/conc_max default to the selection's own std_conc range when
    not given explicitly, but can be widened by hand — e.g. to also cover
    real samples slightly below/above the lowest/highest calibration point."""
    pooled_points = []
    curves_used: dict[int, object] = {}
    for curve, index in point_refs:
        point = curve.points[index]
        if point.get("response") is None or point.get("std_conc") is None:
            continue
        pooled_points.append(
            {"included": True, "response": point["response"], "std_conc": point["std_conc"]}
        )
        curves_used[curve.pk] = curve

    if conc_min is None or conc_max is None:
        stds = [p["std_conc"] for p in pooled_points]
        conc_min = min(stds) if stds else 0.0
        conc_max = max(stds) if stds else 0.0

    try:
        k, b, r2 = fitting.linear_fit(pooled_points)
        error = None
    except fitting.FitError as exc:
        k, b, r2 = None, None, None
        error = str(exc)

    merged = MergedSegment.objects.create(
        compound=compound,
        label=label,
        conc_min=conc_min,
        conc_max=conc_max,
        k=k,
        b=b,
        r2=r2,
        point_count=len(pooled_points),
        fit_error=error or "",
        status=fitting.status_from_fit(k, r2, error),
        created_by=created_by,
    )
    merged.curves.set(curves_used.values())
    return merged


def resolve_sample_result(result: SampleResult) -> None:
    """Resolves one SampleResult against every APPROVED calibration range on
    record for its compound (across all materials) and persists the
    outcome. Leaves the result at NEEDS_REVIEW untouched if there's nothing
    to resolve against yet — no compound match, no response, or no approved
    ranges for that compound. Candidates come from two sources: per-material
    CalibrationSegments and any pooled MergedSegments — sample_resolution
    treats both the same way, just tagged by "kind" so this function knows
    which FK to write the match back to."""
    if result.compound_id is None or result.response is None:
        return

    candidates = [
        {
            "segment_id": segment.id,
            "kind": "segment",
            "curve_id": segment.curve_id,
            "material_label": segment.curve.raw_upload.label,
            "k": segment.k,
            "b": segment.b,
            "conc_min": segment.conc_min,
            "conc_max": segment.conc_max,
        }
        for segment in CalibrationSegment.objects.filter(
            status=CalibrationSegment.Status.APPROVED,
            curve__compound_id=result.compound_id,
            k__isnull=False,
            b__isnull=False,
        ).select_related("curve", "curve__raw_upload")
    ] + [
        {
            "segment_id": merged.id,
            "kind": "merged",
            "curve_id": None,
            "material_label": merged.label or "объединённый",
            "k": merged.k,
            "b": merged.b,
            "conc_min": merged.conc_min,
            "conc_max": merged.conc_max,
        }
        for merged in MergedSegment.objects.filter(
            status=MergedSegment.Status.APPROVED,
            compound_id=result.compound_id,
            k__isnull=False,
            b__isnull=False,
        )
    ]

    if not candidates:
        result.status = SampleResult.Status.NO_CALIBRATION
        result.matched_segment = None
        result.matched_merged_segment = None
        result.computed_conc = None
        result.save(update_fields=["status", "matched_segment", "matched_merged_segment", "computed_conc"])
        return

    outcome = sample_resolution.resolve(result.response, candidates)

    matched_id = outcome.get("matched_segment_id")
    matched_kind = outcome.get("matched_kind")
    result.matched_segment_id = matched_id if matched_kind == "segment" else None
    result.matched_merged_segment_id = matched_id if matched_kind == "merged" else None
    result.computed_conc = outcome.get("computed_conc")
    result.status = outcome.get("status") or SampleResult.Status.NEEDS_REVIEW
    result.details = outcome.get("details") or {}
    result.save(
        update_fields=["matched_segment", "matched_merged_segment", "computed_conc", "status", "details"]
    )


def parse_and_fit_upload(raw_upload) -> None:
    """Parses one RawUpload's file, splits it by compound, auto-fits each
    curve, and stores a CalibrationCurve per compound. Never raises —
    failures are recorded on the RawUpload/CalibrationCurve for the reviewer
    to see instead of crashing the whole batch."""
    try:
        df = pd.read_excel(raw_upload.file.path)
        df = parsing.apply_column_profile(df, raw_upload.profile.column_names)
        tables = parsing.split_into_compound_tables(df)
        tables = parsing.filter_compound_tables(tables, raw_upload.name_filter)
    except Exception as exc:
        raw_upload.parsed_ok = False
        raw_upload.error_message = str(exc)
        raw_upload.save(update_fields=["parsed_ok", "error_message"])
        return

    raw_upload.parsed_ok = True
    raw_upload.error_message = ""
    raw_upload.save(update_fields=["parsed_ok", "error_message"])

    for compound_name, table in tables.items():
        compound, _ = Compound.objects.get_or_create(name=compound_name)

        try:
            points = parsing.points_from_table(table)
            result = fitting.recompute(points)
        except Exception as exc:
            CalibrationCurve.objects.update_or_create(
                raw_upload=raw_upload,
                compound=compound,
                defaults={
                    "points": [],
                    "k": None,
                    "b": None,
                    "r2": None,
                    "fit_error": str(exc),
                    "status": CalibrationCurve.Status.NEEDS_REVIEW,
                },
            )
            continue

        status = fitting.status_from_fit(result["k"], result["r2"], result["error"])
        curve, _ = CalibrationCurve.objects.update_or_create(
            raw_upload=raw_upload,
            compound=compound,
            defaults={
                "points": result["points"],
                "k": result["k"],
                "b": result["b"],
                "r2": result["r2"],
                "fit_error": result["error"] or "",
                "status": status,
            },
        )

        # Automatic segmentation disabled — ranges are only created manually
        # now (see the commented-out segment_curve() above). A freshly
        # uploaded curve simply has no ranges until a reviewer adds one by
        # hand on the review screen.


def parse_and_fit_sample_upload(sample_upload) -> None:
    """Parses one SampleUpload's file, splits it by compound, and resolves
    every row against the approved calibration segments for that compound.
    Mirrors parse_and_fit_upload()'s never-raise contract — a bad file or an
    unresolvable row is recorded, not raised."""
    try:
        df = pd.read_excel(sample_upload.file.path)
        df = parsing.apply_column_profile(df, sample_upload.profile.column_names)
        tables = parsing.split_into_compound_tables(df, require_std_conc=False)
        tables = parsing.filter_compound_tables(tables, sample_upload.name_filter)
    except Exception as exc:
        sample_upload.parsed_ok = False
        sample_upload.error_message = str(exc)
        sample_upload.save(update_fields=["parsed_ok", "error_message"])
        return

    sample_upload.parsed_ok = True
    sample_upload.error_message = ""
    sample_upload.save(update_fields=["parsed_ok", "error_message"])

    for compound_name, table in tables.items():
        # Internal-standard channels ("PFBA-IS", or occasionally split
        # oddly into just "IS" when the source file has a stray space) are
        # QC channels used to normalize the *other* compounds' response —
        # they're never themselves a target analyte with a calibration to
        # resolve against, so there's nothing useful to report here.
        if compound_name == "IS" or compound_name.replace(" ", "").endswith("-IS"):
            continue

        compound = Compound.objects.filter(name=compound_name).first()

        try:
            rows = parsing.points_from_sample_table(table)
        except Exception as exc:
            SampleResult.objects.create(
                upload=sample_upload,
                compound=compound,
                raw_compound_name=compound_name,
                details={"parse_error": str(exc)},
            )
            continue

        for row in rows:
            result = SampleResult.objects.create(
                upload=sample_upload,
                compound=compound,
                raw_compound_name=compound_name,
                name=row["name"],
                area=row["area"],
                is_area=row["is_area"],
                response=row["response"],
            )
            try:
                resolve_sample_result(result)
            except Exception as exc:
                result.details = {**result.details, "resolve_error": str(exc)}
                result.save(update_fields=["details"])


def generate_sample_results_export(user) -> SampleResultsExport:
    """(Re)builds the one persisted Excel export of every sample result
    across every SampleBatch and writes it to the single SampleResultsExport
    row — creating that row the first time, replacing its file in place
    (old bytes deleted from storage first) on every later call."""
    results = SampleResult.objects.select_related("compound").order_by("compound__name", "name")

    buffer = sample_excel_export.build_sample_results_workbook(results)

    export = SampleResultsExport.objects.first()
    if export is None:
        export = SampleResultsExport()
    else:
        export.file.delete(save=False)
    export.file.save("sample_results.xlsx", ContentFile(buffer.read()), save=False)
    export.generated_by = user
    export.save()
    return export


def delete_sample_results_export() -> None:
    """Deletes the persisted export file and its record, if one exists —
    the underlying SampleResult rows are untouched."""
    export = SampleResultsExport.objects.first()
    if export is not None:
        export.file.delete(save=False)
        export.delete()
