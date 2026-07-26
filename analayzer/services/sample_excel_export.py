"""Builds the persisted, server-side Excel export of every sample result
across every SampleBatch — one sheet per compound (вещество), covering
every batch at once. Bridges to SampleResultsExport in services/processing.py,
which is what actually writes the result of build_sample_results_workbook()
to disk.
"""
from __future__ import annotations

from io import BytesIO

import pandas as pd

from .excel_export import safe_excel_name


def build_sample_results_workbook(results) -> BytesIO:
    """results: iterable of SampleResult, ideally .select_related(
    "compound", "upload", "upload__batch", "matched_segment__curve__raw_upload",
    "matched_merged_segment")."""
    by_compound: dict[str, list] = {}
    for result in results:
        compound_name = result.compound.name if result.compound_id else (result.raw_compound_name or "Без вещества")
        by_compound.setdefault(compound_name, []).append(result)

    used_sheet_names = {"Summary"}
    summary_rows = []
    sheet_entries = []
    for compound_name in sorted(by_compound):
        rows = by_compound[compound_name]
        sheet_name = safe_excel_name(compound_name, used_sheet_names)
        summary_rows.append({"Вещество": compound_name, "Sheet": sheet_name, "Строк": len(rows)})
        sheet_entries.append((sheet_name, rows))

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Summary", index=False)

        for sheet_name, rows in sheet_entries:
            table = pd.DataFrame(
                [
                    {
                        "Name": r.name,
                        "Area": r.area,
                        "IS Area": r.is_area,
                        "Response": r.response,
                        "Conc": r.computed_conc,
                        "Статус": r.get_status_display(),
                    }
                    for r in rows
                ]
            )
            table.to_excel(writer, sheet_name=sheet_name, index=False)

            worksheet = writer.sheets[sheet_name]
            for col in "ABCDEF":
                worksheet.column_dimensions[col].width = 16

    buffer.seek(0)
    return buffer
