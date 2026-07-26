from django.contrib import admin

from .models import (
    CalibrationBatch,
    CalibrationCurve,
    Compound,
    InstrumentProfile,
    RawUpload,
    SampleResultsExport,
)


@admin.register(InstrumentProfile)
class InstrumentProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "column_count")
    search_fields = ("name",)

    @admin.display(description="Columns")
    def column_count(self, obj):
        return len(obj.column_names)


@admin.register(Compound)
class CompoundAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


class RawUploadInline(admin.TabularInline):
    model = RawUpload
    extra = 0
    fields = ("label", "file", "profile", "name_filter", "parsed_ok", "error_message")
    readonly_fields = ("parsed_ok", "error_message")


@admin.register(CalibrationBatch)
class CalibrationBatchAdmin(admin.ModelAdmin):
    list_display = ("name", "created_by", "created_at")
    list_filter = ("created_by",)
    inlines = [RawUploadInline]


@admin.register(RawUpload)
class RawUploadAdmin(admin.ModelAdmin):
    list_display = ("label", "batch", "profile", "parsed_ok", "uploaded_at")
    list_filter = ("parsed_ok", "profile")


@admin.register(CalibrationCurve)
class CalibrationCurveAdmin(admin.ModelAdmin):
    list_display = ("compound", "raw_upload", "status", "k", "b", "r2", "reviewed_by")
    list_filter = ("status", "raw_upload__batch")
    search_fields = ("compound__name",)
    readonly_fields = ("points",)


@admin.register(SampleResultsExport)
class SampleResultsExportAdmin(admin.ModelAdmin):
    list_display = ("file", "generated_by", "updated_at")
