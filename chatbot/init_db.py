#!/usr/bin/env python3
"""Build a queryable SQLite database from sample_data.xlsx."""

from __future__ import annotations

import os
import posixpath
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import ZipFile


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS}
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
TABLE_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
EXCEL_EPOCH = datetime(1899, 12, 30)
BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(45, 48))
PRESENTATION_SHEETS = {"Dashboard"}
APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DASHBOARD_DB_PATH", APP_DIR / "chatbot_data.db"))


def find_workspace() -> Path:
    data_dir = os.environ.get("DASHBOARD_DATA_DIR", "").strip()
    if data_dir:
        candidate = Path(data_dir)
        if (candidate / "sample_data.xlsx").is_file():
            return candidate
    for parent in (APP_DIR, *APP_DIR.parents):
        if (parent / "sample_data.xlsx").is_file():
            return parent
    raise FileNotFoundError("sample_data.xlsx was not found in the workspace hierarchy")


def column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if not letters:
        raise ValueError(f"Invalid cell reference: {reference}")
    result = 0
    for letter in letters.group(0):
        result = result * 26 + ord(letter) - ord("A") + 1
    return result


def is_date_format(code: str) -> bool:
    cleaned = re.sub(r'"[^"]*"|\\.|\[[^]]*\]', "", code.casefold())
    return bool(re.search(r"[yd]", cleaned) and re.search(r"[mdhs]", cleaned))


def date_style_ids(archive: ZipFile) -> set[int]:
    if "xl/styles.xml" not in archive.namelist():
        return set()
    root = ET.fromstring(archive.read("xl/styles.xml"))
    custom_formats = {
        int(node.attrib["numFmtId"]): node.attrib.get("formatCode", "")
        for node in root.findall("m:numFmts/m:numFmt", NS)
    }
    result: set[int] = set()
    cell_formats = root.find("m:cellXfs", NS)
    if cell_formats is None:
        return result
    for index, node in enumerate(cell_formats):
        format_id = int(node.attrib.get("numFmtId", "0"))
        if format_id in BUILTIN_DATE_FORMATS or is_date_format(custom_formats.get(format_id, "")):
            result.add(index)
    return result


def excel_datetime(value: int | float) -> str:
    parsed = EXCEL_EPOCH + timedelta(days=float(value))
    if parsed.time() == datetime.min.time():
        return parsed.strftime("%Y-%m-%d")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def read_workbook(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read structured workbook sheets without requiring an Excel package."""
    with ZipFile(path) as archive:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("m:si", NS):
                shared_strings.append("".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t")))

        date_styles = date_style_ids(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"] for item in relationships}
        sheets: dict[str, list[dict[str, Any]]] = {}

        sheet_nodes = workbook.find("m:sheets", NS)
        if sheet_nodes is None:
            return sheets
        for sheet in sheet_nodes:
            name = sheet.attrib["name"]
            if name in PRESENTATION_SHEETS:
                continue
            relationship_id = sheet.attrib[f"{{{REL_NS}}}id"]
            target = targets[relationship_id]
            target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(f"xl/{target}")
            root = ET.fromstring(archive.read(target))
            raw_rows: list[dict[int, Any]] = []

            for row_node in root.findall(".//m:sheetData/m:row", NS):
                row: dict[int, Any] = {}
                for cell in row_node.findall("m:c", NS):
                    index = column_number(cell.attrib["r"])
                    cell_type = cell.attrib.get("t")
                    value_node = cell.find("m:v", NS)
                    if cell_type == "inlineStr":
                        inline = cell.find("m:is", NS)
                        value = "".join(node.text or "" for node in inline.iter(f"{{{MAIN_NS}}}t")) if inline is not None else ""
                    elif value_node is None:
                        value = None
                    elif cell_type == "s":
                        value = shared_strings[int(value_node.text or 0)]
                    elif cell_type == "b":
                        value = 1 if value_node.text == "1" else 0
                    else:
                        raw_value = value_node.text or ""
                        try:
                            numeric = float(raw_value)
                            style_id = int(cell.attrib.get("s", "0"))
                            if style_id in date_styles:
                                value = excel_datetime(numeric)
                            else:
                                value = int(numeric) if numeric.is_integer() else numeric
                        except (ValueError, OverflowError):
                            value = raw_value
                    row[index] = value
                raw_rows.append(row)

            if not raw_rows:
                sheets[name] = []
                continue
            headers = {index: value.strip() for index, value in raw_rows[0].items() if isinstance(value, str) and value.strip()}
            records = []
            for raw_row in raw_rows[1:]:
                record = {header: raw_row.get(index) for index, header in headers.items()}
                if any(value not in (None, "") for value in record.values()):
                    records.append(record)
            sheets[name] = records
        return sheets


def sql_identifier(label: str, fallback: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_") or fallback
    if value[0].isdigit():
        value = f"_{value}"
    return value


def unique_identifiers(labels: list[str], fallback: str) -> list[str]:
    used: set[str] = set()
    result = []
    for index, label in enumerate(labels, start=1):
        base = sql_identifier(label, f"{fallback}_{index}")
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        result.append(candidate)
    return result


def sqlite_type(values: list[Any]) -> str:
    present = [value for value in values if value is not None]
    if present and all(isinstance(value, int) and not isinstance(value, bool) for value in present):
        return "INTEGER"
    if present and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
        return "REAL"
    return "TEXT"


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def column_letters(index: int) -> str:
    if index < 1:
        raise ValueError("Column index must be positive")
    letters = []
    while index:
        index, remainder = divmod(index - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def workbook_sheet_mapping(archive: ZipFile) -> dict[str, dict[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {item.attrib["Id"]: item.attrib["Target"] for item in relationships}
    mapping: dict[str, dict[str, str]] = {}
    for sheet in workbook.findall("m:sheets/m:sheet", NS):
        name = sheet.attrib["name"]
        relationship_id = sheet.attrib[f"{{{REL_NS}}}id"]
        target = targets[relationship_id]
        target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(f"xl/{target}")
        mapping[name] = {"target": target, "relationship_id": relationship_id}
    return mapping


def read_shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t")) for item in root.findall("m:si", NS)]


def write_shared_string(root: ET.Element, value: str) -> int:
    item = ET.SubElement(root, f"{{{MAIN_NS}}}si")
    text = ET.SubElement(item, f"{{{MAIN_NS}}}t")
    text.text = value
    return len(root.findall("m:si", NS)) - 1


def read_sheet_rows(sheet_root: ET.Element) -> list[ET.Element]:
    sheet_data = sheet_root.find("m:sheetData", NS)
    if sheet_data is None:
        raise ValueError("Worksheet is missing sheetData")
    return sheet_data.findall("m:row", NS)


def cell_text(cell: ET.Element, shared_strings: list[str]) -> str | None:
    cell_type = cell.attrib.get("t")
    value_node = cell.find("m:v", NS)
    if cell_type == "inlineStr":
        inline = cell.find("m:is", NS)
        return "".join(node.text or "" for node in inline.iter(f"{{{MAIN_NS}}}t")) if inline is not None else ""
    if value_node is None:
        return None
    if cell_type == "s":
        index = int(value_node.text or 0)
        return shared_strings[index] if 0 <= index < len(shared_strings) else ""
    return value_node.text


def make_text_cell(reference: str, value: str, style: str | None = None) -> ET.Element:
    cell = ET.Element(f"{{{MAIN_NS}}}c", {"r": reference, "t": "inlineStr"})
    if style is not None:
        cell.set("s", style)
    inline = ET.SubElement(cell, f"{{{MAIN_NS}}}is")
    text = ET.SubElement(inline, f"{{{MAIN_NS}}}t")
    text.text = value
    return cell


def make_number_cell(reference: str, value: int | float, style: str | None = None) -> ET.Element:
    cell = ET.Element(f"{{{MAIN_NS}}}c", {"r": reference})
    if style is not None:
        cell.set("s", style)
    numeric = ET.SubElement(cell, f"{{{MAIN_NS}}}v")
    numeric.text = str(int(value) if isinstance(value, float) and value.is_integer() else value)
    return cell


def ensure_employee_row_values(employee: dict[str, Any]) -> dict[str, Any]:
    name = " ".join(str(employee.get("employee_name") or employee.get("name") or "").split())
    email = " ".join(str(employee.get("email") or "").split()).lower()
    department = " ".join(str(employee.get("department") or "").split())
    if not name:
        raise ValueError("Employee name is required")
    if "@" not in email:
        raise ValueError("A valid email address is required")
    if not department:
        raise ValueError("Department is required")
    job_title = " ".join(str(employee.get("job_title") or employee.get("role") or "Employee").split()) or "Employee"
    level = " ".join(str(employee.get("level") or employee.get("role") or "Employee").split()) or "Employee"
    manager = " ".join(str(employee.get("manager") or "").split())
    location = " ".join(str(employee.get("location") or "Remote").split()) or "Remote"
    hire_date = str(employee.get("hire_date") or datetime.now().strftime("%Y-%m-%d"))
    employment_status = " ".join(str(employee.get("employment_status") or "Active").split()) or "Active"
    department_id = " ".join(str(employee.get("department_id") or "").split())
    return {
        "employee_name": name,
        "email": email,
        "department": department,
        "job_title": job_title,
        "level": level,
        "manager": manager,
        "location": location,
        "hire_date": hire_date,
        "employment_status": employment_status,
        "department_id": department_id,
    }


def department_id_for_name(workbook: dict[str, list[dict[str, Any]]], department_name: str) -> str:
    # read_workbook() keys records by the sheet's raw header text (e.g.
    # "Department Name"), not a normalized snake_case name.
    for row in workbook.get("Departments", []):
        if str(row.get("Department Name") or "").strip() == department_name:
            return str(row.get("Department ID") or "").strip()
    return ""


def normalize_employee_record(row: dict[str, Any]) -> dict[str, Any]:
    """Convert an Employees-sheet row (keyed by raw header text) into the
    snake_case shape used internally by ensure_employee_row_values() and the
    admin sync payloads."""
    return {
        "employee_id": row.get("Employee ID"),
        "employee_name": row.get("Employee Name"),
        "email": row.get("Email"),
        "department_id": row.get("Department ID"),
        "department": row.get("Department"),
        "job_title": row.get("Job Title"),
        "level": row.get("Level"),
        "manager": row.get("Manager"),
        "location": row.get("Location"),
        "hire_date": row.get("Hire Date"),
        "employment_status": row.get("Employment Status"),
    }


def employee_row_map(workbook: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in workbook.get("Employees", []):
        email = str(row.get("Email") or "").strip().lower()
        if email:
            rows[email] = normalize_employee_record(row)
    return rows


def next_employee_id(workbook: dict[str, list[dict[str, Any]]]) -> str:
    numbers = []
    for row in workbook.get("Employees", []):
        match = re.match(r"E(\d+)$", str(row.get("Employee ID") or "").strip())
        if match:
            numbers.append(int(match.group(1)))
    return f"E{(max(numbers) + 1) if numbers else 1}"


def sync_employee_workbook(workbook_path: Path, *, action: str, employee: dict[str, Any]) -> None:
    if action not in {"create", "update", "promote", "delete"}:
        raise ValueError("Unsupported workbook sync action")
    if not workbook_path.is_file():
        raise FileNotFoundError("sample_data.xlsx was not found")

    with ZipFile(workbook_path, "r") as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
        workbook = read_workbook(workbook_path)
        sheet_map = workbook_sheet_mapping(archive)
        sheet_info = sheet_map.get("Employees")
        if sheet_info is None:
            raise ValueError("Employees sheet was not found")
        sheet_root = ET.fromstring(files[sheet_info["target"]])
        shared_strings = read_shared_strings(archive)
        sheet_dir = posixpath.dirname(sheet_info["target"])
        sheet_file = posixpath.basename(sheet_info["target"])
        table_rels_path = posixpath.join(sheet_dir, "_rels", f"{sheet_file}.rels")
        table_path = None
        if table_rels_path in files:
            rels_root = ET.fromstring(files[table_rels_path])
            for rel in rels_root.findall(f"{{{PKG_NS}}}Relationship"):
                if rel.attrib.get("Type", "").endswith("/table"):
                    target = rel.attrib["Target"]
                    table_path = posixpath.normpath(posixpath.join("xl/worksheets", target))
                    table_path = table_path.replace("xl/worksheets/../", "xl/")
                    break

    rows = read_sheet_rows(sheet_root)
    if not rows:
        raise ValueError("Employees sheet is missing data rows")
    header_row = rows[0]
    headers: list[str] = []
    for cell in header_row.findall("m:c", NS):
        headers.append(str(cell_text(cell, shared_strings) or "").strip())
    header_to_column = {header: column_letters(index) for index, header in enumerate(headers, start=1)}
    email_column = header_to_column.get("Email")
    if email_column is None:
        raise ValueError("Employees sheet is missing an Email column")

    sheet_data = sheet_root.find("m:sheetData", NS)
    assert sheet_data is not None

    table_root: ET.Element | None = None
    table_end_row = 126
    if table_path and table_path in files:
        table_root = ET.fromstring(files[table_path])
        table_ref_attr = table_root.attrib.get("ref")
        if table_ref_attr:
            match = re.match(r"[A-Z]+(\d+)$", table_ref_attr.split(":")[-1])
            if match:
                table_end_row = int(match.group(1))

    existing = employee_row_map(workbook)
    email_key = str(employee.get("email") or "").strip().lower()
    if action in {"update", "promote", "delete"} and email_key not in existing:
        raise ValueError("User not found")

    def row_email(row: ET.Element) -> str:
        for cell in row.findall("m:c", NS):
            ref = cell.attrib.get("r", "")
            if re.match(r"[A-Z]+", ref).group(0) == email_column:
                return str(cell_text(cell, shared_strings) or "").strip().lower()
        return ""

    def row_index(row: ET.Element) -> int:
        return int(row.attrib.get("r", "0"))

    def build_row(values: dict[str, Any], row_index_value: int) -> ET.Element:
        row = ET.Element(f"{{{MAIN_NS}}}row", {"r": str(row_index_value), "spans": "1:11", "ht": "27"})
        ordered = {
            "Employee ID": values.get("employee_id"),
            "Employee Name": values["employee_name"],
            "Email": values["email"],
            "Department ID": values.get("department_id") or department_id_for_name(workbook, values["department"]),
            "Department": values["department"],
            "Job Title": values["job_title"],
            "Level": values["level"],
            "Manager": values["manager"],
            "Location": values["location"],
            "Hire Date": values["hire_date"],
            "Employment Status": values["employment_status"],
        }
        for header, value in ordered.items():
            column = header_to_column[header]
            reference = f"{column}{row_index_value}"
            if header == "Hire Date":
                try:
                    dt = datetime.strptime(str(value), "%Y-%m-%d")
                except ValueError:
                    row.append(make_text_cell(reference, str(value), "4"))
                else:
                    row.append(make_number_cell(reference, (dt - EXCEL_EPOCH).days, "4"))
            elif value not in (None, ""):
                row.append(make_text_cell(reference, str(value), "3"))
        return row

    def retarget_row(row: ET.Element, new_index: int) -> None:
        row.set("r", str(new_index))
        for cell in row.findall("m:c", NS):
            match = re.match(r"([A-Z]+)\d+$", cell.attrib.get("r", ""))
            column = match.group(1) if match else ""
            cell.set("r", f"{column}{new_index}")

    # Build the payload for create/update/promote up front so we fail fast on
    # bad input before touching the XML tree at all.
    payload: dict[str, Any] = {}
    if action == "create":
        normalized = ensure_employee_row_values(employee)
        payload = dict(normalized)
        payload["employee_id"] = str(employee.get("employee_id") or existing.get(email_key, {}).get("employee_id") or next_employee_id(workbook)).strip()
        payload["department_id"] = department_id_for_name(workbook, payload["department"])
    elif action in {"update", "promote"}:
        merged = dict(existing[email_key])
        merged.update(employee)
        normalized = ensure_employee_row_values(merged)
        payload = dict(normalized)
        payload["employee_id"] = str(existing[email_key].get("employee_id") or next_employee_id(workbook)).strip()
        payload["department_id"] = department_id_for_name(workbook, payload["department"]) or str(
            existing[email_key].get("department_id") or ""
        ).strip()
        if action == "promote":
            payload["job_title"] = "Administrator"
            payload["level"] = "Administrator"

    # table_end_row grows every time we add a row (the table's own ref widens
    # to include it), so on a later call a previously-added employee row is
    # indistinguishable from one of the original 200 sample rows -- both just
    # sit at some index <= table_end_row. So rather than track "static body"
    # vs "admin-added" as separate buckets (which breaks the moment the table
    # grows past a row that used to be "custom"), we treat every row within
    # the table uniformly: find it by email, update it in place if it exists,
    # append it at the end of the table if it doesn't, and always renumber
    # the whole table contiguously afterwards. Anything past the table is
    # blank padding that just gets pushed down to make room.
    all_rows = list(sheet_data.findall("m:row", NS))
    table_rows = [row for row in all_rows if row is not header_row and row_index(row) <= table_end_row]
    filler_rows = [row for row in all_rows if row_index(row) > table_end_row]

    target_row = None
    for row in rows[1:]:
        if row_email(row) == email_key:
            target_row = row
            break

    target_position = table_rows.index(target_row) if target_row in table_rows else None
    if target_position is not None:
        table_rows.pop(target_position)

    if action != "delete":
        new_row = build_row(payload, 0)  # row index is fixed up by retarget_row below
        if target_position is not None:
            table_rows.insert(target_position, new_row)
        else:
            table_rows.append(new_row)

    # Rebuild sheetData from scratch: the header row, then every table row
    # renumbered contiguously starting at row 2, then any leftover blank
    # padding rows pushed down to directly follow the (possibly larger) table.
    for row in all_rows:
        sheet_data.remove(row)
    sheet_data.append(header_row)
    for offset, row in enumerate(table_rows, start=1):
        retarget_row(row, 1 + offset)
        sheet_data.append(row)
    filler_start = 1 + len(table_rows)
    for offset, row in enumerate(filler_rows, start=1):
        retarget_row(row, filler_start + offset)
        sheet_data.append(row)

    table_end_row = filler_start
    table_ref = f"A1:K{table_end_row}"
    sheet_last_row = filler_start + len(filler_rows) if filler_rows else table_end_row

    dim = sheet_root.find("m:dimension", NS)
    if dim is not None:
        dim.set("ref", f"A1:K{sheet_last_row}")
    if table_root is not None and table_path is not None:
        table_root.set("ref", table_ref)
        auto_filter = table_root.find("m:autoFilter", NS)
        if auto_filter is not None:
            auto_filter.set("ref", table_ref)
        files[table_path] = ET.tostring(table_root, encoding="utf-8", xml_declaration=True)
    files[sheet_info["target"]] = ET.tostring(sheet_root, encoding="utf-8", xml_declaration=True)

    temp_path = workbook_path.with_suffix(".xlsx.tmp")
    temp_path.unlink(missing_ok=True)
    try:
        with ZipFile(temp_path, "w") as out:
            for name, data in files.items():
                out.writestr(name, data)
        os.replace(temp_path, workbook_path)
    finally:
        temp_path.unlink(missing_ok=True)


def create_database(path: Path, sheets: dict[str, list[dict[str, Any]]], source_path: Path | None = None) -> None:
    # sqlite3.Connection's own context manager only commits/rolls back the
    # transaction, it does NOT close the connection or release the file handle.
    # On Windows, os.replace() then fails with PermissionError because the
    # .tmp file is still open. contextlib.closing() ensures the connection
    # (and its file handle) is actually closed before we return.
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE workbook_sheets (
                sheet_name TEXT PRIMARY KEY,
                table_name TEXT NOT NULL UNIQUE,
                row_count INTEGER NOT NULL
            );
            CREATE TABLE workbook_columns (
                sheet_name TEXT NOT NULL,
                table_name TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                source_name TEXT NOT NULL,
                column_name TEXT NOT NULL,
                data_type TEXT NOT NULL,
                PRIMARY KEY (table_name, ordinal)
            );
            CREATE TABLE workbook_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        table_names = unique_identifiers(list(sheets), "sheet")
        for (sheet_name, rows), suffix in zip(sheets.items(), table_names):
            table_name = f"data_{suffix}"
            headers = list(rows[0]) if rows else []
            column_names = unique_identifiers(headers, "column")
            types = [sqlite_type([row.get(header) for row in rows]) for header in headers]
            connection.execute(
                "INSERT INTO workbook_sheets VALUES (?, ?, ?)",
                (sheet_name, table_name, len(rows)),
            )
            connection.executemany(
                "INSERT INTO workbook_columns VALUES (?, ?, ?, ?, ?, ?)",
                [(sheet_name, table_name, index, header, column, data_type)
                 for index, (header, column, data_type) in enumerate(zip(headers, column_names, types), start=1)],
            )
            if not headers:
                continue
            definitions = ", ".join(f"{quote_identifier(column)} {data_type}" for column, data_type in zip(column_names, types))
            connection.execute(f"CREATE TABLE {quote_identifier(table_name)} ({definitions})")
            placeholders = ", ".join("?" for _ in headers)
            connection.executemany(
                f"INSERT INTO {quote_identifier(table_name)} VALUES ({placeholders})",
                [tuple(row.get(header) for header in headers) for row in rows],
            )
            for column in column_names:
                if column == "id" or column.endswith("_id"):
                    index_name = quote_identifier(f"idx_{table_name}_{column}")
                    connection.execute(f"CREATE INDEX {index_name} ON {quote_identifier(table_name)} ({quote_identifier(column)})")
        if source_path is not None:
            source_stat = source_path.stat()
            connection.executemany(
                "INSERT INTO workbook_meta VALUES (?, ?)",
                [
                    ("source_path", str(source_path.resolve())),
                    ("source_modified_ns", str(source_stat.st_mtime_ns)),
                    ("source_size", str(source_stat.st_size)),
                    ("source_modified", datetime.fromtimestamp(source_stat.st_mtime, timezone.utc).isoformat()),
                    ("last_synced", datetime.now(timezone.utc).isoformat()),
                ],
            )
        connection.commit()


def rebuild_database(workbook_path: Path | None = None, db_path: Path = DB_PATH) -> dict[str, Any]:
    workbook_path = workbook_path or (find_workspace() / "sample_data.xlsx")
    sheets = read_workbook(workbook_path)
    temporary_path = db_path.with_suffix(".db.tmp")
    temporary_path.unlink(missing_ok=True)
    try:
        create_database(temporary_path, sheets, workbook_path)
        os.replace(temporary_path, db_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return {
        "database": str(db_path),
        "workbook": str(workbook_path),
        "sheets": len(sheets),
        "rows": sum(map(len, sheets.values())),
    }


def main() -> None:
    result = rebuild_database()
    print(f"Created {result['database']} with {result['sheets']} queryable workbook sheets and {result['rows']} rows")


if __name__ == "__main__":
    main()
