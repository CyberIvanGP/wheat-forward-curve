"""Small OOXML value export using only Python's standard library.

Runtime export must work without Codex, Node, Excel or authoring services.
JSON is the precision-preserving output; Excel numbers have Excel's precision.
"""

from datetime import date
from decimal import Decimal
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZipFile, ZIP_DEFLATED

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def export_today_curve(result, path):
    sheet = Element("worksheet", xmlns=NS)
    views = SubElement(sheet, "sheetViews")
    view = SubElement(views, "sheetView", workbookViewId="0", showGridLines="0")
    SubElement(view, "pane", ySplit="5", topLeftCell="A6", activePane="bottomLeft", state="frozen")
    columns = SubElement(sheet, "cols")
    for i, width in enumerate((13, 18, 23, 29, 20), 1):
        SubElement(columns, "col", min=str(i), max=str(i), width=str(width), customWidth="1")
    data = SubElement(sheet, "sheetData")

    def row(number, values, header=False):
        element = SubElement(data, "row", r=str(number), ht="23", customHeight="1")
        for i, value in enumerate(values):
            ref = f"{chr(65 + i)}{number}"
            if value is None:
                continue
            if isinstance(value, date):
                cell = SubElement(element, "c", r=ref, s="3")
                SubElement(cell, "v").text = str((value - date(1899, 12, 30)).days)
            elif isinstance(value, (Decimal, int)):
                cell = SubElement(element, "c", r=ref, s="2" if isinstance(value, Decimal) else "0")
                SubElement(cell, "v").text = str(value)
            else:
                cell = SubElement(element, "c", r=ref, t="inlineStr", s="1" if header else "0")
                SubElement(SubElement(cell, "is"), "t").text = str(value)

    row(2, ["Кривая пшеницы: " + result["calculation_date"]])
    row(3, ["Цена: руб./т. JSON сохраняет полную расчётную точность."])
    row(5, ["Срок", "Экспирация", "Цена, руб./т", "Источник цены", "Дней переноса"], True)
    for n, point in enumerate(result["curve"], 6):
        row(n, [point["tenor"], date.fromisoformat(point["maturity_date"]), point["price"],
                point["source"], point["fallback_streak"]])
    SubElement(sheet, "autoFilter", ref=f"A5:E{5 + len(result['curve'])}")
    styles = '''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2"><numFmt numFmtId="164" formatCode="#,##0.00"/><numFmt numFmtId="165" formatCode="yyyy-mm-dd"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Arial"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF24354B"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" indent="1"/></xf><xf numFmtId="165" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    parts = {
        "[Content_Types].xml": '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>''',
        "_rels/.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''',
        "xl/workbook.xml": '''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Сегодняшняя кривая" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''',
        "xl/styles.xml": styles,
        "xl/worksheets/sheet1.xml": tostring(sheet, encoding="utf-8", xml_declaration=True),
    }
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
