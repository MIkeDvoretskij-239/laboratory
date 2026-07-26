"""Excel export ported from save_calibration_to_excel() in calibration.ipynb.

Takes CalibrationCurve rows already loaded from the DB instead of the
notebook's in-memory dict-of-dicts, but produces the same shape of workbook:
a Summary sheet plus one sheet per (source label, compound) pair.
"""
from __future__ import annotations

import re
from io import BytesIO

import numpy as np
import pandas as pd

POINT_COLUMN_LABELS = {
    "name": "Name",
    "std_conc": "Std.Conc",
    "area": "Area",
    "is_area": "IS Area",
    "response": "Response",
    "included": "Included",
    "conc": "Conc",
    "dev_pct": "Dev%",
}


def safe_excel_name(name: str, used_names: set[str]) -> str:
    clean_name = re.sub(r'[:\\/?*\[\]]', "_", str(name)).strip() or "Sheet"
    base_name = clean_name[:31]
    sheet_name = base_name
    counter = 1
    while sheet_name in used_names:
        suffix = f"_{counter}"
        sheet_name = f"{base_name[: 31 - len(suffix)]}{suffix}"
        counter += 1
    used_names.add(sheet_name)
    return sheet_name


def build_calibration_workbook(curves) -> BytesIO:
    """curves: iterable of CalibrationCurve, ideally .select_related("raw_upload", "compound")."""
    used_sheet_names = {"Summary"}
    summary_rows = []
    sheet_entries = []

    for curve in curves:
        label = curve.raw_upload.label
        compound_name = curve.compound.name
        sheet_name = safe_excel_name(f"{label}_{compound_name}", used_sheet_names)

        summary_rows.append(
            {
                "Table": label,
                "Compound": compound_name,
                "Sheet": sheet_name,
                "k": curve.k if curve.k is not None else np.nan,
                "b": curve.b if curve.b is not None else np.nan,
                "R2": curve.r2 if curve.r2 is not None else np.nan,
                "Status": curve.get_status_display(),
            }
        )
        sheet_entries.append((sheet_name, label, compound_name, curve))

    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Summary", index=False)

        for sheet_name, label, compound_name, curve in sheet_entries:
            table = pd.DataFrame(curve.points)
            if not table.empty:
                table = table.rename(columns=POINT_COLUMN_LABELS)
            table.to_excel(writer, sheet_name=sheet_name, index=False)

            worksheet = writer.sheets[sheet_name]
            worksheet["J2"], worksheet["K2"] = "Table", label
            worksheet["J3"], worksheet["K3"] = "Compound", compound_name
            worksheet["J4"], worksheet["K4"] = "k", curve.k
            worksheet["J5"], worksheet["K5"] = "b", curve.b
            worksheet["J6"], worksheet["K6"] = "R2", curve.r2

            for col in "ABCDEFG":
                worksheet.column_dimensions[col].width = 13

    buffer.seek(0)
    return buffer
