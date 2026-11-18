#!/usr/bin/env python3
"""Regenerate adifcheck's bundled data files from their published sources.

Sources (downloaded manually; nothing here runs at install or run time):

1. ADIF 3.1.8 specification exports — the ZIP archive linked from
   https://www.adif.org/318/ADIF_318_resources.htm
   (https://adif.org.uk/318/resources). Extract it and pass the path of the
   ``318/exports/csv`` directory as the first argument. The generator reads
   ``fields.csv`` and the per-enumeration ``enumerations_*.csv`` files and
   writes ``adifcheck/data/adif318.json``.

2. eQSL.cc ADIF Content Specifications —
   https://www.eqsl.cc/qslcard/ADIFContentSpecs.cfm
   Save the page HTML and pass its path as the second argument. The
   generator extracts the MODES, BANDS and SAT_MODEs tables and writes
   ``adifcheck/data/eqsl.json``.

The ADIF specification tables are the work of the ADIF Developers Group;
the eQSL tables are eQSL.cc's published upload requirements. Both are
reproduced here as data so the checker can run offline, with attribution
in the JSON files themselves and in the README.

Usage:
    python3 tools/build_data.py /path/to/318/exports/csv /path/to/eqsl.html
"""

import csv
import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "adifcheck" / "data"

# Enumerations whose membership depends on another field's value (the
# subdivision tables are keyed by DXCC entity and are also revised by
# outside bodies). adifcheck deliberately does not check these, mirroring
# the ADIF ADX schema's own documented limitation.
SKIP_ENUMS = {
    "Primary_Administrative_Subdivision",
    "Secondary_Administrative_Subdivision",
    "Secondary_Administrative_Subdivision_Alt",
}


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.reader(fh))


def build_adif(csv_dir):
    csv_dir = Path(csv_dir)
    fields = {}
    enum_refs = {}  # field name -> enumeration name (base, [] stripped)
    with open(csv_dir / "fields.csv", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = row["Field Name"].strip()
            if not re.fullmatch(r"[A-Z0-9_]+", name):
                continue  # skips USERDEFn template row
            enum = row["Enumeration"].strip()
            base_enum = enum.split("[")[0].strip() if enum else ""
            fields[name] = {
                "type": row["Data Type"].strip(),
                "enum": base_enum or None,
                "header": row["Header Field"].strip() == "Y",
                "min": row["Minimum Value"].strip() or None,
                "max": row["Maximum Value"].strip() or None,
                # the exports mark rows with the literal string
                # "Import-only" (not a boolean)
                "import_only": row["Import-only"].strip() == "Import-only",
            }
            if base_enum:
                enum_refs[name] = base_enum

    enums = {}
    for enum_name in sorted(set(enum_refs.values())):
        if enum_name in SKIP_ENUMS:
            continue
        fname = "enumerations_" + enum_name.lower() + ".csv"
        if not (csv_dir / fname).exists():
            # Country has no export of its own: its values are the DXCC
            # entity names. Sponsored_Award is not exported by ADIF at all,
            # and "Credit,Award" belongs to list-typed fields; both are
            # skipped (the checker treats a missing enum as "not checked").
            if enum_name == "Country":
                dxcc = read_csv(csv_dir / "enumerations_dxcc_entity_code.csv")
                enums["Country"] = {
                    "values": [r[2].strip() for r in dxcc[1:] if r[2].strip()],
                    "import_only": [],
                    "deleted": [r[2].strip() for r in dxcc[1:]
                                if len(r) > 3 and r[3].strip() == "Deleted"],
                }
            continue
        rows = read_csv(csv_dir / fname)
        header, data = rows[0], rows[1:]
        import_idx = header.index("Import-only") if "Import-only" in header else None
        deleted_idx = header.index("Deleted") if "Deleted" in header else None
        deldate_idx = header.index("Deleted Date") if "Deleted Date" in header else None
        values, import_only, deleted = [], [], []
        for r in data:
            if len(r) < 2 or not r[1].strip():
                continue
            value = r[1].strip()
            values.append(value)
            # markers are literal strings in the exports, not booleans:
            # "Import-only" in its column, "Deleted" in its column, or a
            # non-empty "Deleted Date" (the ARRL Section table's form)
            if import_idx is not None and r[import_idx].strip() == "Import-only":
                import_only.append(value)
            if (deleted_idx is not None and r[deleted_idx].strip() == "Deleted") \
                    or (deldate_idx is not None and r[deldate_idx].strip()):
                deleted.append(value)
        enums[enum_name] = {
            "values": values,
            "import_only": import_only,
            "deleted": deleted,
        }

    band_rows = read_csv(csv_dir / "enumerations_band.csv")
    bands = {}
    for r in band_rows[1:]:
        bands[r[1].strip()] = [float(r[2]), float(r[3])]

    sub_rows = read_csv(csv_dir / "enumerations_submode.csv")
    submodes = {}
    sub_import = []
    for r in sub_rows[1:]:
        submodes[r[1].strip()] = r[2].strip()
        if len(r) > 4 and r[4].strip() == "Import-only":
            sub_import.append(r[1].strip())

    # Primary/secondary administrative subdivisions, keyed by DXCC
    # entity code — the only honest way to check STATE/CNTY, since a
    # code is legal only for its own entity. (These tables are why the
    # flat SKIP_ENUMS treatment exists; they are embedded separately.)
    def subdivisions(fname):
        rows = read_csv(csv_dir / fname)
        header, data = rows[0], rows[1:]
        imp_i = header.index("Import-only") if "Import-only" in header else None
        del_i = header.index("Deleted") if "Deleted" in header else None
        out = {}
        for r in data:
            # columns: Enumeration Name, Code, Name, DXCC Entity Code, ...
            if len(r) < 4 or not r[1].strip() or not r[3].strip():
                continue
            entry = out.setdefault(r[3].strip(),
                                   {"values": [], "import_only": [],
                                    "deleted": []})
            entry["values"].append(r[1].strip())
            if imp_i is not None and r[imp_i].strip() == "Import-only":
                entry["import_only"].append(r[1].strip())
            if del_i is not None and r[del_i].strip() == "Deleted":
                entry["deleted"].append(r[1].strip())
        return out

    primary = subdivisions("enumerations_primary_administrative_subdivision.csv")
    secondary = subdivisions("enumerations_secondary_administrative_subdivision.csv")

    return {
        "spec_version": "3.1.8",
        "source": "ADIF 3.1.8 specification exports (adif.org), "
                  "https://www.adif.org/318/ADIF_318_resources.htm",
        "fields": fields,
        "enums": enums,
        "bands": bands,
        "submodes": submodes,
        "submode_import_only": sub_import,
        "subdivisions": primary,
        "subdivisions_secondary": secondary,
        "skipped_enums": sorted(SKIP_ENUMS),
    }


def _cells(row_html):
    cs = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)
    out = []
    for c in cs:
        c = re.sub(r"<br\s*/?>", "\n", c, flags=re.I)
        c = re.sub(r"<[^>]+>", "", c)
        out.append(html.unescape(c).strip())
    return out


def _split_list(text):
    parts = re.split(r"[,\n]", text)
    return [p.strip() for p in parts if p.strip()]


def build_eqsl(html_path):
    raw = Path(html_path).read_text(encoding="utf-8", errors="replace")
    tables = re.findall(r"<table.*?</table>", raw, re.S | re.I)
    main = max(tables, key=len)
    rows = [_cells(r) for r in re.findall(r"<tr.*?</tr>", main, re.S | re.I)]

    modes = {}
    bands = {}
    sat_modes = {}
    section = None
    for cs in rows:
        if not cs:
            continue
        head = cs[0]
        if head == "MODES":
            section = "modes"
            continue
        if head.startswith("IMPORTING ADIF") or head.startswith("MODE CONVERSIONS"):
            section = None  # the conversion table reuses mode names as keys
            continue
        if head == "BANDS":
            section = "bands"
            continue
        if head == "SAT_MODEs":
            section = "sat"
            continue
        if head in ("Mode", "BAND", "SAT_MODE"):
            continue
        if section == "modes" and len(cs) >= 1 and re.fullmatch(r"[A-Z0-9]+", head):
            modes[head] = _split_list(cs[1]) if len(cs) > 1 else []
        elif section == "bands" and len(cs) >= 2:
            m = re.fullmatch(r"([\d.]+) to ([\d.]+)", cs[1])
            if m and not head.startswith("SAT"):
                bands[head] = [float(m.group(1)), float(m.group(2))]
        elif section == "sat" and len(cs) >= 2 and re.fullmatch(r"[A-Z/]+", head):
            sat_modes[head] = cs[1]
    if len(modes) < 40 or len(bands) < 20 or len(sat_modes) < 10:
        raise SystemExit(
            f"eQSL extraction looks wrong: {len(modes)} modes, "
            f"{len(bands)} bands, {len(sat_modes)} sat_modes")
    return {
        "source": "eQSL.cc ADIF Content Specifications, "
                  "https://www.eqsl.cc/qslcard/ADIFContentSpecs.cfm "
                  "(retrieved 2026-11-18)",
        "modes": modes,
        "bands": bands,
        "sat_modes": sat_modes,
        "limits": {"CALL": 13, "QSLMSG": 240, "RST_SENT": 8, "SAT_NAME": 15},
    }


def main(argv):
    if len(argv) != 3:
        raise SystemExit(__doc__)
    adif = build_adif(argv[1])
    eqsl = build_eqsl(argv[2])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "adif318.json").write_text(
        json.dumps(adif, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (DATA_DIR / "eqsl.json").write_text(
        json.dumps(eqsl, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"adif318.json: {len(adif['fields'])} fields, "
          f"{len(adif['enums'])} enumerations, {len(adif['bands'])} bands, "
          f"{len(adif['submodes'])} submodes")
    print(f"eqsl.json: {len(eqsl['modes'])} modes, {len(eqsl['bands'])} bands, "
          f"{len(eqsl['sat_modes'])} sat modes")


if __name__ == "__main__":
    main(sys.argv)
