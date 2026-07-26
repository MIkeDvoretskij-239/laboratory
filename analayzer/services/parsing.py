"""Parsing logic ported from calibration.ipynb.

Pure pandas/numpy — no Django imports, so it stays independently testable and
reusable outside the web app.
"""
from __future__ import annotations

import re

import pandas as pd

NEEDED_COLUMNS = ["№", "Name", "Std.Conc", "Area", "IS Area", "Response"]


class ParsingError(Exception):
    """Raised when a raw export doesn't match the selected instrument profile."""


def apply_column_profile(df: pd.DataFrame, column_names: list[str]) -> pd.DataFrame:
    """Assigns canonical column names to a raw export and drops everything but
    NEEDED_COLUMNS, mirroring the notebook's `data.columns = [...]` + drop step."""
    if len(df.columns) != len(column_names):
        raise ParsingError(
            f"Ожидалось {len(column_names)} колонок в файле, а получено "
            f"{len(df.columns)}. Проверь, что выбран правильный профиль инструмента."
        )

    df = df.copy()
    df.columns = column_names
    drop_cols = [c for c in df.columns if c not in NEEDED_COLUMNS]
    df = df.drop(columns=drop_cols).dropna(how="all").reset_index(drop=True)
    return df


def filter_compound_tables(
    tables: dict[str, pd.DataFrame], substring: str
) -> dict[str, pd.DataFrame]:
    """Generalizes the notebook's ad-hoc mike_new/new_new filters, e.g. keeping
    only rows whose Name contains "(M)" or "PT77 Cal" when a single raw file
    bundles several overlapping calibration sets. Must run on the already
    compound-split tables (like the notebook does) — filtering the flat sheet
    first would strip out the "Compound ..." marker rows and break the split."""
    if not substring:
        return tables

    mask_re = re.escape(substring)
    filtered = {}
    for compound_name, table in tables.items():
        mask = table["Name"].astype(str).str.contains(mask_re, na=False)
        filtered[compound_name] = table[mask].reset_index(drop=True)
    return filtered


def split_into_compound_tables(
    df: pd.DataFrame, require_std_conc: bool = True
) -> dict[str, pd.DataFrame]:
    """Ported from devide_for_subtables(): splits one flat sheet into a table per
    compound, using "Compound <name>" marker rows to delimit each block.

    require_std_conc=False is for unknown-sample files, which share the
    calibration files' column layout but have no Std.Conc to fill in — every
    row is kept instead of being dropped for a missing Std.Conc, and rows
    stay in their original order instead of being sorted by it."""
    # Anchored at the start on purpose: the instrument's own report title
    # ("Quantify Compound Summary Report") contains the word "Compound" too,
    # and an unanchored contains() match turns that title row into a bogus
    # extra compound block.
    compound_mask = df["№"].astype(str).str.match(r"Compound\s", na=False)
    compound_indexes = df.index[compound_mask].tolist()

    if not compound_indexes:
        raise ParsingError(
            'В файле не найдено ни одной строки-маркера "Compound ...". '
            "Проверь профиль инструмента и содержимое файла."
        )

    tables: dict[str, pd.DataFrame] = {}

    for i, start in enumerate(compound_indexes):
        end = compound_indexes[i + 1] if i + 1 < len(compound_indexes) else len(df)
        compound_name = str(df.loc[start, "№"]).split()[-1]

        subtable = df.iloc[start:end].copy()
        subtable = subtable[subtable["Name"] != "Name"]
        subtable = subtable.drop(columns=["№"]).reset_index(drop=True)

        if "Std.Conc" not in subtable.columns:
            subtable["Std.Conc"] = pd.NA
        if require_std_conc:
            subtable = subtable.dropna(how="all", subset=["Std.Conc"]).reset_index(drop=True)

        subtable["Area"] = pd.to_numeric(subtable["Area"], errors="coerce")
        subtable["IS Area"] = pd.to_numeric(subtable["IS Area"], errors="coerce")
        subtable["Response"] = subtable["Area"] / subtable["IS Area"]

        if require_std_conc:
            subtable = subtable.sort_values(by="Std.Conc", ascending=True).reset_index(drop=True)

        if compound_name in tables:
            tables[compound_name] = pd.concat(
                [tables[compound_name], subtable], ignore_index=True
            )
        else:
            tables[compound_name] = subtable

    return tables


def points_from_table(table: pd.DataFrame) -> list[dict]:
    """Converts a compound subtable into the JSON point list stored on
    CalibrationCurve.points, with an initial outlier guess pre-applied."""
    from . import fitting  # local import avoids a module-load cycle

    table = table.copy()
    table["Std.Conc"] = pd.to_numeric(table["Std.Conc"], errors="coerce")
    table["Response"] = pd.to_numeric(table["Response"], errors="coerce")
    table = table.dropna(subset=["Std.Conc", "Response"]).reset_index(drop=True)

    included = fitting.auto_exclude_outliers(table["Response"].tolist())

    points = []
    for idx, row in table.iterrows():
        area = row.get("Area")
        is_area = row.get("IS Area")
        name = row.get("Name")
        points.append(
            {
                "name": str(name) if pd.notna(name) else "",
                "std_conc": float(row["Std.Conc"]),
                "area": float(area) if pd.notna(area) else None,
                "is_area": float(is_area) if pd.notna(is_area) else None,
                "response": float(row["Response"]),
                "included": bool(included[idx]),
            }
        )
    return points


def points_from_sample_table(table: pd.DataFrame) -> list[dict]:
    """Converts an unknown-sample subtable into plain per-row dicts. Unlike
    points_from_table(), Std.Conc is optional/typically absent — these rows
    are exactly what we're trying to determine the concentration of, so
    there's no outlier detection and no Std.Conc dependency."""
    table = table.copy()
    table["Area"] = pd.to_numeric(table["Area"], errors="coerce")
    table["IS Area"] = pd.to_numeric(table["IS Area"], errors="coerce")
    table["Response"] = pd.to_numeric(table["Response"], errors="coerce")
    table = table.dropna(subset=["Response"]).reset_index(drop=True)

    rows = []
    for _, row in table.iterrows():
        area = row.get("Area")
        is_area = row.get("IS Area")
        name = row.get("Name")
        rows.append(
            {
                "name": str(name) if pd.notna(name) else "",
                "area": float(area) if pd.notna(area) else None,
                "is_area": float(is_area) if pd.notna(is_area) else None,
                "response": float(row["Response"]),
            }
        )
    return rows
