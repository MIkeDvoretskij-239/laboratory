from django.urls import path

from . import views

urlpatterns = [
    path("", views.batch_list, name="batch-list"),
    path("batches/new/", views.batch_create, name="batch-create"),
    path("batches/<int:pk>/", views.batch_detail, name="batch-detail"),
    path("batches/<int:pk>/approve-ok/", views.batch_bulk_approve, name="batch-bulk-approve"),
    path("batches/<int:pk>/delete/", views.batch_delete, name="batch-delete"),
    path("batches/<int:pk>/export/", views.batch_export, name="batch-export"),
    path("curves/<int:pk>/", views.curve_review, name="curve-review"),
    path("curves/<int:pk>/update-points/", views.curve_update_points, name="curve-update-points"),
    path("curves/<int:pk>/set-status/", views.curve_set_status, name="curve-set-status"),
    # Automatic range-splitting is disabled — see curve_recompute_segments in views.py.
    # path("curves/<int:pk>/recompute-segments/", views.curve_recompute_segments, name="curve-recompute-segments"),
    path("curves/<int:pk>/segments/new/", views.curve_segment_create, name="curve-segment-create"),
    path("segments/<int:pk>/delete/", views.curve_segment_delete, name="curve-segment-delete"),
    path("compounds/", views.compound_list, name="compound-list"),
    path("compounds/<int:pk>/", views.compound_compare, name="compound-compare"),
    path("compounds/<int:compound_pk>/merged-segments/new/", views.merged_segment_create, name="merged-segment-create"),
    path("merged-segments/<int:pk>/delete/", views.merged_segment_delete, name="merged-segment-delete"),
    path("merged-segments/<int:pk>/set-status/", views.merged_segment_set_status, name="merged-segment-set-status"),
    path("profiles/", views.profile_list, name="profile-list"),
    path("profiles/new/", views.profile_create, name="profile-create"),
    path("profiles/<int:pk>/edit/", views.profile_edit, name="profile-edit"),
    path("profiles/<int:pk>/delete/", views.profile_delete, name="profile-delete"),
    # Cross-material equivalence checking is disabled — see views.py.
    # path("equivalence/", views.equivalence_list, name="equivalence-list"),
    # path("equivalence/new/", views.equivalence_new, name="equivalence-new"),
    path("samples/", views.sample_batch_list, name="sample-batch-list"),
    path("samples/new/", views.sample_batch_create, name="sample-batch-create"),
    path(
        "samples/export/generate/",
        views.sample_results_export_generate,
        name="sample-results-export-generate",
    ),
    path(
        "samples/export/download/",
        views.sample_results_export_download,
        name="sample-results-export-download",
    ),
    path(
        "samples/export/delete/",
        views.sample_results_export_delete,
        name="sample-results-export-delete",
    ),
    path("samples/<int:pk>/", views.sample_batch_detail, name="sample-batch-detail"),
    path("samples/<int:pk>/delete/", views.sample_batch_delete, name="sample-batch-delete"),
    path(
        "samples/<int:batch_pk>/compounds/<int:compound_pk>/",
        views.sample_batch_compound,
        name="sample-batch-compound",
    ),
    path("sample-results/<int:pk>/recompute/", views.sample_result_recompute, name="sample-result-recompute"),
    path("sample-results/<int:pk>/update/", views.sample_result_update, name="sample-result-update"),
    path("sample-results/<int:pk>/set-segment/", views.sample_result_set_segment, name="sample-result-set-segment"),
    path("sample-results/<int:pk>/delete/", views.sample_result_delete, name="sample-result-delete"),
    path("sample-results/new/", views.sample_result_create, name="sample-result-create"),
]
