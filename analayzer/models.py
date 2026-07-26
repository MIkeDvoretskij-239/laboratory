from django.conf import settings
from django.db import models
from django.db.models import Count


class InstrumentProfile(models.Model):
    """Describes how to interpret a raw instrument export: the exact ordered
    list of column headers to assign (mirrors the notebook's hardcoded
    `data.columns = [...]` lines). Add a new profile here instead of touching
    code whenever a new instrument/export format shows up."""

    name = models.CharField(max_length=100, unique=True)
    column_names = models.JSONField(
        help_text="Ordered list of column names to assign to the raw file, "
        "left to right — must match the raw file's column count exactly.",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Compound(models.Model):
    """Canonical compound name, e.g. "PFOA" — keeps the same name used across
    different batches/uploads mapping to the same calibration history."""

    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class CalibrationBatch(models.Model):
    """One upload session: a set of raw instrument exports processed together."""

    name = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="calibration_batches"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    @property
    def curves(self):
        return CalibrationCurve.objects.filter(raw_upload__batch=self)

    def status_counts(self) -> dict:
        counts = {choice: 0 for choice, _ in CalibrationCurve.Status.choices}
        for row in self.curves.values("status").annotate(count=Count("id")):
            counts[row["status"]] = row["count"]
        return counts


class RawUpload(models.Model):
    """One raw xlsx file within a batch, e.g. the "Plazma" or "Mike" source."""

    batch = models.ForeignKey(CalibrationBatch, on_delete=models.CASCADE, related_name="uploads")
    label = models.CharField(
        max_length=100,
        help_text='Short name for this calibration source, e.g. "H2O", "Plazma", "Mike".',
    )
    file = models.FileField(upload_to="calibration_uploads/%Y/%m/")
    profile = models.ForeignKey(InstrumentProfile, on_delete=models.PROTECT, related_name="uploads")
    name_filter = models.CharField(
        max_length=100,
        blank=True,
        help_text='Optional: keep only rows whose Name contains this substring, e.g. "(M)" '
        'or "PT77 Cal" — for files that bundle several overlapping calibration sets.',
    )
    parsed_ok = models.BooleanField(default=False)
    error_message = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.batch.name} / {self.label}"


class CalibrationCurve(models.Model):
    """One compound's calibration curve for one raw upload: the point set, the
    fitted k/b/R², and the human review status. Approved curves persist as the
    reusable reference for that compound going forward."""

    class Status(models.TextChoices):
        NEEDS_REVIEW = "needs_review", "Нужна проверка"
        OK = "ok", "Автоматически подобрано"
        APPROVED = "approved", "Подтверждено"
        REJECTED = "rejected", "Отклонено"

    raw_upload = models.ForeignKey(RawUpload, on_delete=models.CASCADE, related_name="curves")
    compound = models.ForeignKey(Compound, on_delete=models.PROTECT, related_name="curves")
    points = models.JSONField(default=list)
    k = models.FloatField(null=True, blank=True)
    b = models.FloatField(null=True, blank=True)
    r2 = models.FloatField(null=True, blank=True)
    fit_error = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEEDS_REVIEW)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_curves"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["compound__name"]
        unique_together = [("raw_upload", "compound")]

    def __str__(self) -> str:
        return f"{self.raw_upload.label} / {self.compound.name}"


class CalibrationSegment(models.Model):
    """One linear piece of a CalibrationCurve's concentration range. A curve
    always has at least one segment (spanning the full range) once
    processing.segment_curve() has run — sample resolution and equivalence
    checks always read from segments, never from the curve's own k/b directly,
    so a single-range curve is just the degenerate one-segment case."""

    class Status(models.TextChoices):
        NEEDS_REVIEW = "needs_review", "Нужна проверка"
        OK = "ok", "Автоматически подобрано"
        APPROVED = "approved", "Подтверждено"
        REJECTED = "rejected", "Отклонено"

    curve = models.ForeignKey(CalibrationCurve, on_delete=models.CASCADE, related_name="segments")
    index = models.PositiveSmallIntegerField()
    conc_min = models.FloatField()
    conc_max = models.FloatField()
    k = models.FloatField(null=True, blank=True)
    b = models.FloatField(null=True, blank=True)
    r2 = models.FloatField(null=True, blank=True)
    fit_error = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEEDS_REVIEW)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["curve", "index"]
        unique_together = [("curve", "index")]

    def __str__(self) -> str:
        return f"{self.curve} [{self.conc_min}–{self.conc_max}]"


class CalibrationEquivalence(models.Model):
    """A user-triggered comparison between two calibration curves (usually the
    same compound on two different materials), produced by
    services.equivalence.compare_curves(). Segment FKs are optional — a
    comparison can be done at the whole-curve level or between two specific
    segments picked by the reviewer."""

    curve_a = models.ForeignKey(CalibrationCurve, on_delete=models.CASCADE, related_name="equivalence_as_a")
    curve_b = models.ForeignKey(CalibrationCurve, on_delete=models.CASCADE, related_name="equivalence_as_b")
    segment_a = models.ForeignKey(
        CalibrationSegment, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    segment_b = models.ForeignKey(
        CalibrationSegment, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    is_equivalent = models.BooleanField(null=True, blank=True)
    valid_conc_min = models.FloatField(null=True, blank=True)
    valid_conc_max = models.FloatField(null=True, blank=True)
    notes = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="calibration_equivalences"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.curve_a} ↔ {self.curve_b}"


class MergedSegment(models.Model):
    """A calibration range pooled from two or more materials' curves for the
    same compound — e.g. Kate + Plazma + Mike's points in 0.01–0.5 combined
    into a single fit, when they agree closely enough that one shared line
    is a more robust approximation for real samples than any single
    material's own (often sparser) segment. Behaves as just another
    candidate in sample resolution, alongside per-curve CalibrationSegments."""

    class Status(models.TextChoices):
        NEEDS_REVIEW = "needs_review", "Нужна проверка"
        APPROVED = "approved", "Подтверждено"
        REJECTED = "rejected", "Отклонено"

    compound = models.ForeignKey(Compound, on_delete=models.CASCADE, related_name="merged_segments")
    label = models.CharField(max_length=200, blank=True)
    curves = models.ManyToManyField(CalibrationCurve, related_name="merged_segments")
    conc_min = models.FloatField()
    conc_max = models.FloatField()
    k = models.FloatField(null=True, blank=True)
    b = models.FloatField(null=True, blank=True)
    r2 = models.FloatField(null=True, blank=True)
    point_count = models.PositiveIntegerField(default=0)
    fit_error = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEEDS_REVIEW)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="merged_segments"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["compound", "conc_min"]

    def __str__(self) -> str:
        return self.label or f"{self.compound.name} [{self.conc_min}–{self.conc_max}] (объединённый)"


class SampleBatch(models.Model):
    """One upload session for unknown/real samples — mirrors CalibrationBatch."""

    name = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="sample_batches"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    @property
    def results(self):
        return SampleResult.objects.filter(upload__batch=self)

    def status_counts(self) -> dict:
        counts = {choice: 0 for choice, _ in SampleResult.Status.choices}
        for row in self.results.values("status").annotate(count=Count("id")):
            counts[row["status"]] = row["count"]
        return counts


class SampleUpload(models.Model):
    """One raw xlsx file of unknown samples within a SampleBatch — mirrors RawUpload."""

    batch = models.ForeignKey(SampleBatch, on_delete=models.CASCADE, related_name="uploads")
    label = models.CharField(
        max_length=100,
        blank=True,
        help_text="Необязательно: контекст пробы, например материал матрицы.",
    )
    file = models.FileField(upload_to="sample_uploads/%Y/%m/")
    profile = models.ForeignKey(InstrumentProfile, on_delete=models.PROTECT, related_name="sample_uploads")
    name_filter = models.CharField(max_length=100, blank=True)
    parsed_ok = models.BooleanField(default=False)
    error_message = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.batch.name} / {self.label or self.file.name}"


class SampleResult(models.Model):
    """One unknown sample row: raw response plus the resolved calibration
    segment and computed concentration, produced by
    services.sample_resolution.resolve()."""

    class Status(models.TextChoices):
        NEEDS_REVIEW = "needs_review", "Нужна проверка"
        MATCHED = "matched", "Определено"
        OUT_OF_RANGE = "out_of_range", "Вне диапазона калибровок"
        NO_CALIBRATION = "no_calibration", "Нет подходящей калибровки"

    upload = models.ForeignKey(SampleUpload, on_delete=models.CASCADE, related_name="results")
    compound = models.ForeignKey(
        Compound, on_delete=models.SET_NULL, null=True, blank=True, related_name="sample_results"
    )
    raw_compound_name = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=200, blank=True)
    area = models.FloatField(null=True, blank=True)
    is_area = models.FloatField(null=True, blank=True)
    response = models.FloatField(null=True, blank=True)
    matched_segment = models.ForeignKey(
        CalibrationSegment, on_delete=models.SET_NULL, null=True, blank=True, related_name="sample_results"
    )
    matched_merged_segment = models.ForeignKey(
        "MergedSegment", on_delete=models.SET_NULL, null=True, blank=True, related_name="sample_results"
    )
    computed_conc = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEEDS_REVIEW)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["upload", "compound__name", "name"]

    def __str__(self) -> str:
        return f"{self.upload} / {self.raw_compound_name or self.compound}"


class SampleResultsExport(models.Model):
    """The one persisted, server-side Excel export of every SampleResult
    across every SampleBatch, split into one sheet per compound — see
    services.sample_excel_export. Deliberately a stored file (unlike
    CalibrationBatch's generate-on-download export) so it can be created,
    overwritten, or deleted as its own record, independent of the
    underlying SampleResult rows. Only ever zero or one row exists — every
    (re)generate replaces this row's file in place."""

    file = models.FileField(upload_to="sample_exports/")
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="sample_results_exports"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.file.name
