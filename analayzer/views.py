from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.db.models.deletion import ProtectedError
from django.http import FileResponse, Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import (
    CalibrationBatchForm,
    InstrumentProfileForm,
    RawUploadFormSet,
    SampleBatchForm,
    SampleUploadFormSet,
)
from .models import (
    CalibrationBatch,
    CalibrationCurve,
    CalibrationSegment,
    Compound,
    InstrumentProfile,
    MergedSegment,
    SampleBatch,
    SampleResult,
    SampleResultsExport,
    SampleUpload,
)

# `CalibrationEquivalence` model import and `equivalence` service import are
# no longer needed here — cross-material equivalence checking is disabled,
# see the commented-out views below.
# from .models import CalibrationEquivalence
from .services import excel_export, fitting, processing


def _parse_optional_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@login_required
def batch_list(request):
    batches = CalibrationBatch.objects.select_related("created_by")
    return render(request, "analayzer/batch_list.html", {"batches": batches})


@login_required
def batch_create(request):
    if request.method == "POST":
        batch_form = CalibrationBatchForm(request.POST)
        formset = RawUploadFormSet(request.POST, request.FILES, instance=CalibrationBatch())

        if batch_form.is_valid() and formset.is_valid():
            batch = batch_form.save(commit=False)
            batch.created_by = request.user
            batch.save()

            formset.instance = batch
            uploads = formset.save()

            for upload in uploads:
                processing.parse_and_fit_upload(upload)

            messages.success(request, f'Батч "{batch.name}" загружен и обработан.')
            return redirect("batch-detail", pk=batch.pk)
    else:
        batch_form = CalibrationBatchForm()
        formset = RawUploadFormSet(instance=CalibrationBatch())

    return render(request, "analayzer/batch_form.html", {"batch_form": batch_form, "formset": formset})


@login_required
def batch_detail(request, pk):
    batch = get_object_or_404(CalibrationBatch, pk=pk)
    curves = (
        CalibrationCurve.objects.filter(raw_upload__batch=batch)
        .select_related("compound", "raw_upload")
        .order_by("compound__name", "raw_upload__label")
    )
    return render(
        request,
        "analayzer/batch_detail.html",
        {
            "batch": batch,
            "curves": curves,
            "uploads": batch.uploads.all(),
            "status_counts": batch.status_counts(),
        },
    )


@login_required
@require_POST
def batch_delete(request, pk):
    batch = get_object_or_404(CalibrationBatch, pk=pk)
    name = batch.name
    for upload in batch.uploads.all():
        upload.file.delete(save=False)
    batch.delete()
    messages.success(request, f'Батч "{name}" удалён.')
    return redirect("batch-list")


@login_required
@require_POST
def batch_bulk_approve(request, pk):
    batch = get_object_or_404(CalibrationBatch, pk=pk)
    curve_ids = list(
        CalibrationCurve.objects.filter(raw_upload__batch=batch, status=CalibrationCurve.Status.OK).values_list(
            "id", flat=True
        )
    )
    updated = CalibrationCurve.objects.filter(id__in=curve_ids).update(
        status=CalibrationCurve.Status.APPROVED, reviewed_by=request.user, reviewed_at=timezone.now()
    )
    # Sample resolution only ever looks at APPROVED segments, never at the
    # curve's own status — approving a curve without cascading to its
    # segments would leave every sample stuck at "no_calibration".
    CalibrationSegment.objects.filter(curve_id__in=curve_ids).update(status=CalibrationSegment.Status.APPROVED)
    messages.success(request, f"Подтверждено кривых: {updated}.")
    return redirect("batch-detail", pk=batch.pk)


@login_required
def batch_export(request, pk):
    batch = get_object_or_404(CalibrationBatch, pk=pk)
    curves = (
        CalibrationCurve.objects.filter(raw_upload__batch=batch)
        .select_related("compound", "raw_upload")
        .order_by("raw_upload__label", "compound__name")
    )
    buffer = excel_export.build_calibration_workbook(curves)
    return FileResponse(buffer, as_attachment=True, filename=f"calibration_{batch.pk}.xlsx")


@login_required
def curve_review(request, pk):
    curve = get_object_or_404(
        CalibrationCurve.objects.select_related("compound", "raw_upload", "raw_upload__batch"), pk=pk
    )
    segments = list(curve.segments.all())
    segments_json = json.dumps(
        [{"index": s.index, "conc_min": s.conc_min, "conc_max": s.conc_max} for s in segments]
    )
    return render(
        request,
        "analayzer/curve_review.html",
        {
            "curve": curve,
            "points_json": json.dumps(curve.points),
            "segments": segments,
            "segments_json": segments_json,
        },
    )


@login_required
@require_POST
def curve_update_points(request, pk):
    """Replaces the curve's whole point set — backs the editable table on the
    review screen, which lets a reviewer fix a mis-entered Area/IS Area, add a
    missing standard, or drop a bad row, not just toggle include/exclude."""
    curve = get_object_or_404(CalibrationCurve, pk=pk)
    try:
        payload = json.loads(request.body)
        raw_points = payload["points"]
        if not isinstance(raw_points, list):
            raise TypeError
    except (KeyError, TypeError, json.JSONDecodeError):
        return HttpResponseBadRequest("Некорректный запрос.")

    points = []
    for i, raw in enumerate(raw_points, start=1):
        std_conc = _parse_optional_float(raw.get("std_conc"))
        if std_conc is None:
            return HttpResponseBadRequest(f"Строка {i}: заполните Std.Conc числом.")

        area = _parse_optional_float(raw.get("area"))
        is_area = _parse_optional_float(raw.get("is_area"))
        points.append(
            {
                "name": str(raw.get("name") or "").strip(),
                "std_conc": std_conc,
                "area": area,
                "is_area": is_area,
                "response": fitting.compute_response(area, is_area),
                "included": bool(raw.get("included", True)),
            }
        )

    result = fitting.recompute(points)

    # A manual point edit can only move a curve between needs_review/ok —
    # approve/reject stay a deliberate human action, never auto-overwritten.
    if curve.status not in (CalibrationCurve.Status.APPROVED, CalibrationCurve.Status.REJECTED):
        curve.status = fitting.status_from_fit(result["k"], result["r2"], result["error"])

    curve.points = result["points"]
    curve.k = result["k"]
    curve.b = result["b"]
    curve.r2 = result["r2"]
    curve.fit_error = result["error"] or ""
    curve.save()

    return JsonResponse(
        {
            "k": curve.k,
            "b": curve.b,
            "r2": curve.r2,
            "error": curve.fit_error,
            "status": curve.status,
            "status_display": curve.get_status_display(),
            "points": curve.points,
        }
    )


@login_required
@require_POST
def curve_set_status(request, pk):
    curve = get_object_or_404(CalibrationCurve, pk=pk)
    new_status = request.POST.get("status")
    if new_status not in (CalibrationCurve.Status.APPROVED, CalibrationCurve.Status.REJECTED):
        return HttpResponseBadRequest("Некорректный статус.")

    curve.status = new_status
    curve.reviewed_by = request.user
    curve.reviewed_at = timezone.now()
    curve.save()

    # Same reasoning as batch_bulk_approve: sample resolution reads segment
    # status, not curve status, so approving/rejecting a curve has to carry
    # over to its segments or sample matching silently stays broken.
    curve.segments.update(status=new_status)

    messages.success(request, f"{curve.compound.name}: {curve.get_status_display()}.")

    next_url = request.POST.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(next_url)
    return redirect("batch-detail", pk=curve.raw_upload.batch_id)


# Automatic range-splitting is disabled (segment_curve() is commented out in
# services/processing.py) — this view has no automatic computation to run
# anymore. Kept here, commented, in case auto-splitting is restored later.
# @login_required
# @require_POST
# def curve_recompute_segments(request, pk):
#     curve = get_object_or_404(CalibrationCurve, pk=pk)
#     try:
#         processing.segment_curve(curve)
#     except Exception as exc:
#         messages.error(request, f"Не удалось разбить на диапазоны: {exc}")
#     else:
#         messages.success(request, f"Диапазонов посчитано: {curve.segments.count()}.")
#     return redirect("curve-review", pk=curve.pk)


@login_required
@require_POST
def curve_segment_create(request, pk):
    curve = get_object_or_404(CalibrationCurve, pk=pk)
    conc_min = _parse_optional_float(request.POST.get("conc_min"))
    conc_max = _parse_optional_float(request.POST.get("conc_max"))

    raw_indices = request.POST.get("point_indices", "").strip()
    point_indices = [int(i) for i in raw_indices.split(",") if i.strip().isdigit()] if raw_indices else None

    # Ranges are always built from explicitly picked points now — no
    # range-only mode that implicitly grabs whatever points fall between
    # conc_min/conc_max.
    if not point_indices:
        messages.error(request, "Отметь хотя бы одну точку галочкой «→ диап.».")
        return redirect("curve-review", pk=curve.pk)
    if conc_min is not None and conc_max is not None and conc_min >= conc_max:
        messages.error(request, "«От» должно быть меньше «до».")
        return redirect("curve-review", pk=curve.pk)

    segment = processing.add_manual_segment(curve, conc_min, conc_max, point_indices=point_indices)
    if segment.fit_error:
        messages.error(request, f"Диапазон добавлен, но фит не удался: {segment.fit_error}")
    else:
        messages.success(
            request,
            f"Добавлен диапазон {segment.conc_min:g}–{segment.conc_max:g}: k={segment.k:.4f}, R²={segment.r2:.4f}.",
        )
    return redirect("curve-review", pk=curve.pk)


@login_required
@require_POST
def curve_segment_delete(request, pk):
    segment = get_object_or_404(CalibrationSegment, pk=pk)
    curve_pk = segment.curve_id
    processing.delete_segment(segment)
    messages.success(request, "Диапазон удалён.")
    return redirect("curve-review", pk=curve_pk)


@login_required
def compound_list(request):
    compounds = (
        Compound.objects.annotate(curve_count=Count("curves"))
        .filter(curve_count__gt=0)
        .order_by("name")
    )
    return render(request, "analayzer/compound_list.html", {"compounds": compounds})


def _curve_series_for_compound(compound):
    """Shared chart-data builder: every calibration curve for `compound`,
    each with its own segments, in the JSON shape the Plotly chart JS
    expects. Used by both the pure-calibration compound page and the
    per-batch sample review page, so the two charts always agree."""
    curves = (
        CalibrationCurve.objects.filter(compound=compound)
        .prefetch_related("segments")
        .select_related("raw_upload", "raw_upload__batch")
        .order_by("-raw_upload__batch__created_at", "raw_upload__label")
    )
    series = [
        {
            "id": curve.pk,
            "label": f"{curve.raw_upload.batch.name} / {curve.raw_upload.label}",
            "status": curve.status,
            "status_display": curve.get_status_display(),
            "points": curve.points,
            "k": curve.k,
            "b": curve.b,
            "r2": curve.r2,
            "fit_error": curve.fit_error,
            "segments": [
                {"index": seg.index, "conc_min": seg.conc_min, "conc_max": seg.conc_max, "k": seg.k, "b": seg.b}
                for seg in sorted(curve.segments.all(), key=lambda s: s.index)
            ],
        }
        for curve in curves
    ]
    return curves, series


def _merged_segments_for_compound(compound):
    return MergedSegment.objects.filter(compound=compound).prefetch_related("curves").order_by("conc_min")


def _merged_series_for_compound(compound):
    return [
        {
            "id": merged.pk,
            "label": merged.label or f"Объединённый #{merged.pk}",
            "conc_min": merged.conc_min,
            "conc_max": merged.conc_max,
            "k": merged.k,
            "b": merged.b,
            "status": merged.status,
            "status_display": merged.get_status_display(),
        }
        for merged in _merged_segments_for_compound(compound)
    ]


def _segments_by_compound_json(compound_ids):
    """Options for the sample-row segment-override dropdown: every segment
    (per-material and merged) on record for each of `compound_ids`,
    regardless of status, so a reviewer can see and pick from all of them.
    Keyed by compound id since one page (the batch-wide table) can list
    rows for many compounds at once."""
    by_compound: dict[int, list[dict]] = {}

    for segment in (
        CalibrationSegment.objects.filter(curve__compound_id__in=compound_ids)
        .select_related("curve", "curve__raw_upload")
        .order_by("curve__compound_id", "curve__raw_upload__label", "index")
    ):
        by_compound.setdefault(segment.curve.compound_id, []).append(
            {
                "id": segment.pk,
                "kind": "segment",
                "label": (
                    f"{segment.curve.raw_upload.label} #{segment.index} "
                    f"({segment.conc_min:g}–{segment.conc_max:g}) [{segment.get_status_display()}]"
                ),
            }
        )

    for merged in MergedSegment.objects.filter(compound_id__in=compound_ids).order_by("compound_id", "conc_min"):
        by_compound.setdefault(merged.compound_id, []).append(
            {
                "id": merged.pk,
                "kind": "merged",
                "label": (
                    f"{merged.label or 'Объединённый'} ({merged.conc_min:g}–{merged.conc_max:g}) "
                    f"[{merged.get_status_display()}]"
                ),
            }
        )

    return json.dumps(by_compound)


@login_required
def compound_compare(request, pk):
    """Pure calibration view for one compound: every curve on record, across
    materials/batches, plus segment and merged-segment management. Sample
    points live under the Пробы section, scoped to a batch — see
    sample_batch_compound()."""
    compound = get_object_or_404(Compound, pk=pk)
    curves, series = _curve_series_for_compound(compound)
    merged_segments = _merged_segments_for_compound(compound)
    all_compounds = Compound.objects.annotate(curve_count=Count("curves")).filter(curve_count__gt=0).order_by("name")

    return render(
        request,
        "analayzer/compound_compare.html",
        {
            "compound": compound,
            "all_compounds": all_compounds,
            "curves": curves,
            "series_json": json.dumps(series),
            "merged_segments": merged_segments,
            "merged_series_json": json.dumps(_merged_series_for_compound(compound)),
        },
    )


@login_required
@require_POST
def merged_segment_create(request, compound_pk):
    compound = get_object_or_404(Compound, pk=compound_pk)
    conc_min = _parse_optional_float(request.POST.get("conc_min"))
    conc_max = _parse_optional_float(request.POST.get("conc_max"))
    label = request.POST.get("label", "").strip()

    # Each checked point comes in as "curve_id:point_index" — resolve once
    # per distinct curve rather than re-querying per point.
    raw_refs = request.POST.getlist("points")
    curve_ids = set()
    parsed_refs = []
    for raw in raw_refs:
        curve_id_str, _, index_str = raw.partition(":")
        if not (curve_id_str.isdigit() and index_str.isdigit()):
            continue
        curve_ids.add(int(curve_id_str))
        parsed_refs.append((int(curve_id_str), int(index_str)))

    curves_by_id = {c.pk: c for c in CalibrationCurve.objects.filter(pk__in=curve_ids, compound=compound)}
    point_refs = [(curves_by_id[cid], idx) for cid, idx in parsed_refs if cid in curves_by_id]

    distinct_curves = {c.pk for c, _ in point_refs}
    if len(distinct_curves) < 2:
        messages.error(request, "Отметь точки минимум из двух разных материалов.")
        return redirect("compound-compare", pk=compound.pk)
    if conc_min is not None and conc_max is not None and conc_min >= conc_max:
        messages.error(request, "«От» должно быть меньше «до».")
        return redirect("compound-compare", pk=compound.pk)

    merged = processing.create_merged_segment(
        compound, point_refs, conc_min, conc_max, label=label, created_by=request.user
    )
    if merged.fit_error:
        messages.error(request, f"Объединённый диапазон создан, но фит не удался: {merged.fit_error}")
    else:
        messages.success(
            request,
            f"Объединённый диапазон {merged.conc_min:g}–{merged.conc_max:g} из {len(distinct_curves)} "
            f"материалов: k={merged.k:.4f}, R²={merged.r2:.4f}, точек: {merged.point_count}.",
        )
    return redirect("compound-compare", pk=compound.pk)


@login_required
@require_POST
def merged_segment_delete(request, pk):
    merged = get_object_or_404(MergedSegment, pk=pk)
    compound_pk = merged.compound_id
    merged.delete()
    messages.success(request, "Объединённый диапазон удалён.")
    return redirect("compound-compare", pk=compound_pk)


@login_required
@require_POST
def merged_segment_set_status(request, pk):
    merged = get_object_or_404(MergedSegment, pk=pk)
    new_status = request.POST.get("status")
    if new_status not in (MergedSegment.Status.APPROVED, MergedSegment.Status.REJECTED):
        return HttpResponseBadRequest("Некорректный статус.")
    merged.status = new_status
    merged.save(update_fields=["status"])
    messages.success(request, f"{merged}: {merged.get_status_display()}.")
    return redirect("compound-compare", pk=merged.compound_id)


@login_required
def profile_list(request):
    profiles = InstrumentProfile.objects.annotate(upload_count=Count("uploads")).order_by("name")
    return render(request, "analayzer/profile_list.html", {"profiles": profiles})


@login_required
def profile_create(request):
    if request.method == "POST":
        form = InstrumentProfileForm(request.POST)
        if form.is_valid():
            profile = form.save()
            messages.success(request, f'Профиль "{profile.name}" создан.')
            return redirect("profile-list")
    else:
        form = InstrumentProfileForm()
    return render(request, "analayzer/profile_form.html", {"form": form, "is_new": True})


@login_required
def profile_edit(request, pk):
    profile = get_object_or_404(InstrumentProfile, pk=pk)
    if request.method == "POST":
        form = InstrumentProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, f'Профиль "{profile.name}" сохранён.')
            return redirect("profile-list")
    else:
        form = InstrumentProfileForm(instance=profile)
    return render(request, "analayzer/profile_form.html", {"form": form, "is_new": False, "profile": profile})


@login_required
@require_POST
def profile_delete(request, pk):
    profile = get_object_or_404(InstrumentProfile, pk=pk)
    name = profile.name
    try:
        profile.delete()
    except ProtectedError:
        messages.error(
            request,
            f'Профиль "{name}" используется в загруженных файлах — его нельзя удалить, '
            "пока на него ссылается хотя бы один файл.",
        )
        return redirect("profile-list")
    messages.success(request, f'Профиль "{name}" удалён.')
    return redirect("profile-list")


# Cross-material equivalence checking is disabled — see the "убрать
# эквивалентность материалов" request. Kept here, commented, in case it's
# restored later.
# def _curve_payload_for_equivalence(curve, segment):
#     """Builds the plain dict equivalence.compare_curves() expects, either for
#     the whole curve or for one of its segments if the reviewer picked one."""
#     points = curve.points
#     if segment is not None:
#         points = [p for p in points if p.get("segment") == segment.index]
#         k, b = segment.k, segment.b
#     else:
#         k, b = curve.k, curve.b
#     return {
#         "label": f"{curve.compound.name} / {curve.raw_upload.label}"
#         + (f" [#{segment.index}]" if segment is not None else ""),
#         "k": k,
#         "b": b,
#         "points": points,
#     }
#
#
# @login_required
# def equivalence_new(request):
#     curves = CalibrationCurve.objects.select_related("compound", "raw_upload", "raw_upload__batch").order_by(
#         "compound__name", "raw_upload__label"
#     )
#     curve_options = [
#         {
#             "id": curve.pk,
#             "label": f"{curve.compound.name} — {curve.raw_upload.label} ({curve.raw_upload.batch.name})",
#         }
#         for curve in curves
#     ]
#
#     segments_by_curve: dict[int, list[dict]] = {}
#     for segment in CalibrationSegment.objects.order_by("curve_id", "index"):
#         segments_by_curve.setdefault(segment.curve_id, []).append(
#             {"id": segment.pk, "label": f"#{segment.index}: {segment.conc_min:g}–{segment.conc_max:g}"}
#         )
#
#     if request.method == "POST":
#         curve_a = get_object_or_404(CalibrationCurve, pk=request.POST.get("curve_a"))
#         curve_b = get_object_or_404(CalibrationCurve, pk=request.POST.get("curve_b"))
#         segment_a_id = request.POST.get("segment_a") or None
#         segment_b_id = request.POST.get("segment_b") or None
#         segment_a = get_object_or_404(CalibrationSegment, pk=segment_a_id) if segment_a_id else None
#         segment_b = get_object_or_404(CalibrationSegment, pk=segment_b_id) if segment_b_id else None
#
#         try:
#             outcome = equivalence.compare_curves(
#                 _curve_payload_for_equivalence(curve_a, segment_a),
#                 _curve_payload_for_equivalence(curve_b, segment_b),
#             )
#         except Exception as exc:
#             messages.error(request, f"Не удалось сравнить калибровки: {exc}")
#             return redirect(f"{request.path}?a={curve_a.pk}&b={curve_b.pk}")
#
#         CalibrationEquivalence.objects.create(
#             curve_a=curve_a,
#             curve_b=curve_b,
#             segment_a=segment_a,
#             segment_b=segment_b,
#             is_equivalent=outcome.get("is_equivalent"),
#             valid_conc_min=outcome.get("valid_conc_min"),
#             valid_conc_max=outcome.get("valid_conc_max"),
#             notes=outcome.get("notes", ""),
#             details=outcome.get("details") or {},
#             created_by=request.user,
#         )
#         messages.success(request, "Сравнение сохранено.")
#         return redirect("equivalence-list")
#
#     return render(
#         request,
#         "analayzer/equivalence_form.html",
#         {
#             "curve_options_json": json.dumps(curve_options),
#             "segments_json": json.dumps(segments_by_curve),
#             "initial_a": request.GET.get("a", ""),
#             "initial_b": request.GET.get("b", ""),
#         },
#     )
#
#
# @login_required
# def equivalence_list(request):
#     comparisons = CalibrationEquivalence.objects.select_related(
#         "curve_a__compound",
#         "curve_a__raw_upload",
#         "curve_b__compound",
#         "curve_b__raw_upload",
#         "segment_a",
#         "segment_b",
#         "created_by",
#     )
#     return render(request, "analayzer/equivalence_list.html", {"comparisons": comparisons})


@login_required
def sample_batch_list(request):
    batches = SampleBatch.objects.select_related("created_by")
    export = SampleResultsExport.objects.select_related("generated_by").first()
    return render(request, "analayzer/sample_batch_list.html", {"batches": batches, "export": export})


@login_required
@require_POST
def sample_results_export_generate(request):
    processing.generate_sample_results_export(request.user)
    messages.success(request, "Файл с результатами проб сформирован.")
    return redirect("sample-batch-list")


@login_required
def sample_results_export_download(request):
    export = SampleResultsExport.objects.first()
    if export is None:
        raise Http404("Файл с результатами проб ещё не создан.")
    return FileResponse(export.file.open("rb"), as_attachment=True, filename="sample_results.xlsx")


@login_required
@require_POST
def sample_results_export_delete(request):
    processing.delete_sample_results_export()
    messages.success(request, "Файл с результатами проб удалён.")
    return redirect("sample-batch-list")


@login_required
def sample_batch_create(request):
    if request.method == "POST":
        batch_form = SampleBatchForm(request.POST)
        formset = SampleUploadFormSet(request.POST, request.FILES, instance=SampleBatch())

        if batch_form.is_valid() and formset.is_valid():
            batch = batch_form.save(commit=False)
            batch.created_by = request.user
            batch.save()

            formset.instance = batch
            uploads = formset.save()

            for upload in uploads:
                processing.parse_and_fit_sample_upload(upload)

            messages.success(request, f'Партия проб "{batch.name}" загружена и обработана.')
            return redirect("sample-batch-detail", pk=batch.pk)
    else:
        batch_form = SampleBatchForm()
        formset = SampleUploadFormSet(instance=SampleBatch())

    return render(
        request, "analayzer/sample_batch_form.html", {"batch_form": batch_form, "formset": formset}
    )


@login_required
def sample_batch_detail(request, pk):
    batch = get_object_or_404(SampleBatch, pk=pk)
    results = (
        SampleResult.objects.filter(upload__batch=batch)
        .select_related(
            "compound",
            "upload",
            "matched_segment",
            "matched_segment__curve",
            "matched_segment__curve__raw_upload",
            "matched_merged_segment",
        )
        .order_by("upload__id", "compound__name", "name")
    )

    compound_ids = {r.compound_id for r in results if r.compound_id}
    batch_compounds = Compound.objects.filter(pk__in=compound_ids).order_by("name")

    return render(
        request,
        "analayzer/sample_batch_detail.html",
        {
            "batch": batch,
            "results": results,
            "uploads": batch.uploads.all(),
            "status_counts": batch.status_counts(),
            "segments_by_compound_json": _segments_by_compound_json(compound_ids),
            "compounds": Compound.objects.order_by("name"),
            "batch_compounds": batch_compounds,
        },
    )


@login_required
def sample_batch_compound(request, batch_pk, compound_pk):
    """The Пробы-side workspace: calibration graph for one compound
    (curves + segments + merged segments) with this batch's real sample
    points overlaid, plus the editable table for just those rows — scoped
    to one SampleBatch, not the compound globally, since a batch is a
    specific measurement run and its points shouldn't mix with another
    batch's when reviewing."""
    batch = get_object_or_404(SampleBatch, pk=batch_pk)
    compound = get_object_or_404(Compound, pk=compound_pk)

    batch_compound_ids = (
        SampleResult.objects.filter(upload__batch=batch, compound__isnull=False)
        .values_list("compound_id", flat=True)
        .distinct()
    )
    batch_compounds = Compound.objects.filter(pk__in=batch_compound_ids).order_by("name")

    curves, series = _curve_series_for_compound(compound)

    sample_results = (
        SampleResult.objects.filter(upload__batch=batch, compound=compound)
        .select_related(
            "upload",
            "matched_segment",
            "matched_segment__curve",
            "matched_segment__curve__raw_upload",
            "matched_merged_segment",
        )
        .order_by("name")
    )
    samples = [
        {
            "id": r.pk,
            "name": r.name,
            "response": r.response,
            "computed_conc": r.computed_conc,
            "status": r.status,
            "status_display": r.get_status_display(),
            "material_label": (
                r.matched_segment.curve.raw_upload.label
                if r.matched_segment
                else (r.matched_merged_segment.label or "объединённый") if r.matched_merged_segment else None
            ),
        }
        for r in sample_results
    ]

    return render(
        request,
        "analayzer/sample_batch_compound.html",
        {
            "batch": batch,
            "compound": compound,
            "batch_compounds": batch_compounds,
            "curves": curves,
            "series_json": json.dumps(series),
            "merged_series_json": json.dumps(_merged_series_for_compound(compound)),
            "samples_json": json.dumps(samples),
            "sample_results": sample_results,
            "segments_by_compound_json": _segments_by_compound_json([compound.pk]),
            "sample_uploads": batch.uploads.all(),
        },
    )


@login_required
@require_POST
def sample_batch_delete(request, pk):
    batch = get_object_or_404(SampleBatch, pk=pk)
    name = batch.name
    for upload in batch.uploads.all():
        upload.file.delete(save=False)
    batch.delete()
    messages.success(request, f'Партия проб "{name}" удалена.')
    return redirect("sample-batch-list")


@login_required
@require_POST
def sample_result_recompute(request, pk):
    result = get_object_or_404(SampleResult, pk=pk)
    try:
        processing.resolve_sample_result(result)
    except Exception as exc:
        messages.error(request, f"Не удалось пересчитать пробу: {exc}")
    else:
        messages.success(request, "Проба пересчитана.")
    return redirect("sample-batch-detail", pk=result.upload.batch_id)


def _sample_result_payload(result: SampleResult, error: str | None = None) -> dict:
    """Shared JSON shape for every AJAX endpoint that edits a SampleResult —
    keeps the sample review table's row-refresh logic identical no matter
    which action (edit values, override segment, auto-resolve) triggered it.

    matched_segment_key is "segment:<id>" or "merged:<id>" (or "" if
    unmatched) — a CalibrationSegment and a MergedSegment can share the same
    numeric id, so the dropdown value and this key always carry the kind
    alongside the id rather than the bare id alone."""
    if result.matched_segment:
        s = result.matched_segment
        key = f"segment:{s.pk}"
        label = f"{s.curve.raw_upload.label} #{s.index} ({s.conc_min:g}–{s.conc_max:g})"
    elif result.matched_merged_segment:
        m = result.matched_merged_segment
        key = f"merged:{m.pk}"
        label = f"{m.label or 'Объединённый'} ({m.conc_min:g}–{m.conc_max:g})"
    else:
        key = ""
        label = None

    return {
        "id": result.pk,
        "name": result.name,
        "area": result.area,
        "is_area": result.is_area,
        "response": result.response,
        "computed_conc": result.computed_conc,
        "status": result.status,
        "status_display": result.get_status_display(),
        "matched_segment_key": key,
        "matched_segment_label": label,
        "manual_override": bool(result.details.get("manual_override")),
        "error": error,
    }


@login_required
@require_POST
def sample_result_update(request, pk):
    """Backs the editable name/area/is_area fields on the sample review
    table — mirrors curve_update_points()'s contract (edit raw values, get
    back the recomputed result as JSON) but for one SampleResult row at a
    time, since samples don't share a single JSON point blob the way a
    curve's points do."""
    result = get_object_or_404(SampleResult, pk=pk)
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Некорректный запрос.")

    result.name = str(payload.get("name") or "").strip()
    result.area = _parse_optional_float(payload.get("area"))
    result.is_area = _parse_optional_float(payload.get("is_area"))
    result.response = fitting.compute_response(result.area, result.is_area)
    result.save(update_fields=["name", "area", "is_area", "response"])

    # Editing the raw values invalidates any manual segment pick — resolve
    # again from scratch rather than silently keeping a stale override.
    result.details = {k: v for k, v in result.details.items() if k != "manual_override"}
    result.save(update_fields=["details"])

    try:
        processing.resolve_sample_result(result)
        error = None
    except Exception as exc:
        error = str(exc)

    result.refresh_from_db()
    return JsonResponse(_sample_result_payload(result, error))


@login_required
@require_POST
def sample_result_set_segment(request, pk):
    """Lets a reviewer manually pick which calibration range a sample
    belongs to — for the cases resolve_sample_result() can't decide on its
    own (needs_review) or gets wrong. segment_key="" means "back to auto":
    drop the override and let resolve_sample_result() decide again.
    segment_key is otherwise "segment:<id>" or "merged:<id>" — see
    _sample_result_payload for why the kind travels with the id."""
    result = get_object_or_404(SampleResult, pk=pk)
    segment_key = request.POST.get("segment_key") or ""

    if not segment_key:
        result.details = {k: v for k, v in result.details.items() if k != "manual_override"}
        result.save(update_fields=["details"])
        try:
            processing.resolve_sample_result(result)
            error = None
        except Exception as exc:
            error = str(exc)
        result.refresh_from_db()
        return JsonResponse(_sample_result_payload(result, error))

    kind, _, raw_id = segment_key.partition(":")
    if kind == "segment":
        chosen = get_object_or_404(CalibrationSegment, pk=raw_id)
        result.matched_segment = chosen
        result.matched_merged_segment = None
    elif kind == "merged":
        chosen = get_object_or_404(MergedSegment, pk=raw_id)
        result.matched_segment = None
        result.matched_merged_segment = chosen
    else:
        return HttpResponseBadRequest("Некорректный диапазон.")

    if result.response is not None and chosen.k:
        conc = (result.response - chosen.b) / chosen.k
        result.computed_conc = conc
        result.status = (
            SampleResult.Status.MATCHED
            if chosen.conc_min <= conc <= chosen.conc_max
            else SampleResult.Status.OUT_OF_RANGE
        )
    else:
        result.computed_conc = None
        result.status = SampleResult.Status.NEEDS_REVIEW
    result.details = {**result.details, "manual_override": True}
    result.save(
        update_fields=["matched_segment", "matched_merged_segment", "computed_conc", "status", "details"]
    )

    return JsonResponse(_sample_result_payload(result))


@login_required
@require_POST
def sample_result_delete(request, pk):
    result = get_object_or_404(SampleResult, pk=pk)
    result.delete()
    return JsonResponse({"deleted": True})


@login_required
@require_POST
def sample_result_create(request):
    upload = get_object_or_404(SampleUpload, pk=request.POST.get("upload"))
    compound_id = request.POST.get("compound") or None
    compound = get_object_or_404(Compound, pk=compound_id) if compound_id else None

    SampleResult.objects.create(
        upload=upload,
        compound=compound,
        raw_compound_name=compound.name if compound else "",
    )
    messages.success(request, "Точка добавлена — заполни Area / IS Area в таблице.")

    next_url = request.POST.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(next_url)
    return redirect("sample-batch-detail", pk=upload.batch_id)
