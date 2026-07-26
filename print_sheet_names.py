#!/usr/bin/env python3
import sys
import zipfile
import xml.etree.ElementTree as ET


file_path = "data/parc_results_1-31_kate.xlsx"

with zipfile.ZipFile(file_path) as excel_file:
    workbook_xml = excel_file.read("xl/workbook.xml")

root = ET.fromstring(workbook_xml)
namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

sheet_names = [
    sheet.attrib["name"]
    for sheet in root.find("x:sheets", namespace).findall("x:sheet", namespace)
]

print(sheet_names)
