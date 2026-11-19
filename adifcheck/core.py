"""Core parsing and checking for adifcheck.

The checking tables in ``data/adif318.json`` are generated from the ADIF
3.1.8 specification's own published exports (see ``tools/build_data.py``);
the eQSL tables in ``data/eqsl.json`` are extracted from eQSL.cc's
published ADIF Content Specifications. Nothing in this module is
hand-copied from the spec, so a spec update is a data regeneration, not
a code change.

Rule provenance:
- Core rules cite the ADIF 3.1.8 specification (section numbers as in
  the annotated spec at adif.org).
- The LoTW pack implements the rejection causes documented in the
  DX Lab wiki, "Determining why an uploaded QSO was not accepted by
  LoTW", plus the minimum-field requirements on its companion
  "Uploading QSOs to LoTW" guidance. It is documented behaviour, not
  an ARRL-published machine-readable spec, and is labelled as such.
- The eQSL pack implements eQSL.cc's published ADIF Content
  Specifications page (minimum record contents, field-length limits,
  and its MODES / BANDS / SAT_MODEs tables).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dc_field
from datetime import date
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"


class AdifError(Exception):
    """A user-facing, one-line error (never a traceback)."""


# ---------------------------------------------------------------- data

def _load(name):
    with open(DATA_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


def load_spec():
    raw = _load("adif318.json")
    spec = {
        "version": raw["spec_version"],
        "fields": raw["fields"],
        # enumeration values are case-insensitive in ADIF; store upper
        "enums": {
            name: {
                "values": {v.upper() for v in e["values"]},
                "import_only": {v.upper() for v in e["import_only"]},
                "deleted": {v.upper() for v in e["deleted"]},
            }
            for name, e in raw["enums"].items()
        },
        "bands": {k.upper(): v for k, v in raw["bands"].items()},
        "submodes": {k.upper(): v.upper() for k, v in raw["submodes"].items()},
        "submode_import_only": {v.upper() for v in raw["submode_import_only"]},
        "subdivisions": raw.get("subdivisions", {}),
        "subdivisions_secondary": raw.get("subdivisions_secondary", {}),
    }
    return spec


def load_eqsl():
    raw = _load("eqsl.json")
    return {
        "modes": {k.upper(): [s.upper() for s in v]
                  for k, v in raw["modes"].items()},
        "bands": {k.upper(): v for k, v in raw["bands"].items()},
        "sat_modes": {k.upper(): v.upper() for k, v in raw["sat_modes"].items()},
        "limits": raw["limits"],
    }


SPEC = load_spec()
EQSL = load_eqsl()

REF_SPEC = "ADIF 3.1.8"
REF_ENUM = "ADIF 3.1.8 §III.B"
REF_FIELDS = "ADIF 3.1.8 §III.C"
REF_MINIMUM = "ADIF 3.1.8 Resources §III.C (minimum-fields guideline)"
REF_LOTW = "LoTW documented behaviour (DX Lab wiki)"
REF_EQSL = "eQSL ADIF Content Specifications"


# ---------------------------------------------------------------- model

@dataclass
class Finding:
    record: int            # 1-based record number; 0 = file/header level
    severity: str          # "error" | "warning"
    field: str             # ADIF field name or "" for record-level
    rule: str              # short rule id, e.g. "enum", "band-freq"
    message: str
    ref: str               # provenance / spec citation
    pack: str = "core"     # "core" | "lotw" | "eqsl"

    def line(self, label=""):
        where = f"record {self.record}" if self.record else "header/file"
        if label:
            where += f" ({label})"
        fld = f"{self.field}: " if self.field else ""
        return f"  {where}: {fld}{self.severity}: {self.message} [{self.ref}]"


@dataclass
class Record:
    number: int
    fields: dict = dc_field(default_factory=dict)   # UPPER name -> raw value
    order: list = dc_field(default_factory=list)    # field names as seen
    # ADX carries application/user-defined fields as <APP>/<USERDEF>
    # elements whose FIELDNAME need not start with APP_; remember them
    app_fields: set = dc_field(default_factory=set)

    def get(self, name):
        v = self.fields.get(name)
        return v.strip() if isinstance(v, str) else v

    def has(self, name):
        return name in self.fields and self.fields[name].strip() != ""


@dataclass
class ParsedFile:
    header: dict = dc_field(default_factory=dict)
    userdefs: dict = dc_field(default_factory=dict)  # name -> USERDEFn raw spec
    records: list = dc_field(default_factory=list)
    findings: list = dc_field(default_factory=list)  # parse-level findings


# ---------------------------------------------------------------- ADI parser

_TAG_RE = re.compile(r"<\s*([A-Za-z0-9_]+)\s*(?::\s*(\d+)\s*(?::\s*([A-Za-z]))?)?\s*>")


def _decode_value(raw):
    """A value sliced from the byte-oriented view: recover UTF-8 text
    when the bytes are valid UTF-8, else keep the Latin-1 reading."""
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return raw


def parse_adi(text):
    """Parse ADI text tolerantly, collecting syntax findings.

    ``text`` is a Latin-1 view of the file's bytes, so one character is
    one byte and declared lengths behave as byte counts — the way most
    loggers write them (and identical to character counts for the
    ASCII files the spec mandates). Values are decoded back to text
    when stored. Lengths are honoured exactly (values may contain '<'
    and newlines); a record whose declared length does not land on the
    next tag is reported and the parser resynchronises at the next '<'.
    """
    parsed = _parse_adi(text, assume_header="<EOH" in text.upper())
    if not parsed.records and any(
            k in parsed.header for k in ("CALL", "QSO_DATE", "BAND", "MODE")):
        # The "<EOH" guess was fooled by that text inside a field value:
        # what landed in the "header" is really record content. Reparse
        # as a header-less file rather than reporting 0 records, clean.
        reparsed = _parse_adi(text, assume_header=False)
        if reparsed.records:
            return reparsed
    return parsed


def _parse_adi(text, assume_header):
    parsed = ParsedFile()
    pos, n = 0, len(text)
    in_header = assume_header
    current = None  # dict being accumulated
    rec_no = 0

    def syntax(severity, message, record=0):
        parsed.findings.append(Finding(record, severity, "", "syntax",
                                       message, "ADIF 3.1.8 §II (ADI format)"))

    while pos < n:
        lt = text.find("<", pos)
        if lt == -1:
            break  # trailing free text: ignored, like inter-record text
        # Free text between tags is ignored on purpose: the ADIF
        # group's own test files interleave annotation lines (starting
        # "====") between records, and many loggers emit comment text.
        # adifcheck checks field contents, not commentary.
        cur = 0 if in_header else rec_no + 1
        m = _TAG_RE.match(text, lt)
        if not m:
            end = text.find(">", lt)
            snippet = text[lt:(end + 1 if end != -1 else lt + 30)]
            syntax("error", f"malformed tag {snippet[:40]!r}", cur)
            pos = (end + 1) if end != -1 else n
            continue
        name = m.group(1).upper()
        length = m.group(2)
        pos = m.end()
        if length is not None and len(length) > 10:
            # int() on an unbounded digit string can raise (Python's
            # conversion limit) — a corrupt tag must be a finding,
            # never a traceback
            syntax("error",
                   f"field <{name}> has an absurd length specifier "
                   f"({len(length)} digits) — the tag is corrupt", cur)
            continue
        if name in ("EOH", "EOR"):
            if name == "EOH":
                in_header = False
                current = None
            else:
                if current is not None:
                    rec_no += 1
                    rec = Record(rec_no)
                    for k, v in current:
                        rec.fields[k] = v
                        rec.order.append(k)
                    parsed.records.append(rec)
                current = None
            continue
        if length is None:
            syntax("error", f"field <{name}> has no length specifier", cur)
            continue
        ln = int(length)
        value = _decode_value(text[pos:pos + ln])
        if len(text[pos:pos + ln]) < ln:
            syntax("error",
                   f"file ends inside field <{name}>: declared length {ln}, "
                   f"only {len(text[pos:pos + ln])} bytes remain", cur)
            pos = n
            break
        pos += ln
        # after the value, only whitespace may precede the next tag
        nxt = pos
        while nxt < n and text[nxt] in " \t\r\n":
            nxt += 1
        if nxt < n and text[nxt] != "<":
            syntax("error",
                   f"declared length {ln} for <{name}> does not match its "
                   f"data (extra characters before the next tag) — the "
                   f"logger that wrote this file counted the length wrong",
                   cur)
            pos = nxt
        if in_header:
            parsed.header[name] = value
            um = re.fullmatch(r"USERDEF(\d+)", name)
            if um:
                # value is "FIELDNAME" or "FIELDNAME,{...}" / ",{lo:hi}"
                fname = value.split(",", 1)[0].strip().upper()
                if fname:
                    parsed.userdefs[fname] = value
        else:
            if current is None:
                current = []
            current.append((name, value))
    if current:
        rec_no += 1
        rec = Record(rec_no)
        for k, v in current:
            rec.fields[k] = v
            rec.order.append(k)
        parsed.records.append(rec)
        syntax("error", "last record has no <EOR> — it is incomplete and "
                        "most importers will drop it", rec_no)
    if not parsed.records and not parsed.header and not parsed.findings:
        raise AdifError("no ADIF header or records found — is this an ADI file?")
    return parsed


# ---------------------------------------------------------------- ADX parser

def parse_adx(text):
    parsed = ParsedFile()
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise AdifError(f"not well-formed XML, so not valid ADX: {exc}")
    if root.tag.upper() != "ADX":
        raise AdifError(f"root element is <{root.tag}>, not <ADX> — "
                        f"not an ADX file")
    for child in root:
        tag = child.tag.upper()
        if tag == "HEADER":
            for el in child:
                fname = (el.get("FIELDNAME") or el.tag).upper()
                parsed.header[fname] = el.text or ""
                if el.tag.upper() == "USERDEF":
                    fname2 = (el.text or "").split(",", 1)[0].strip().upper()
                    if fname2:
                        parsed.userdefs[fname2] = el.text or ""
        elif tag == "RECORDS":
            for i, rec_el in enumerate(child, start=1):
                if rec_el.tag.upper() != "RECORD":
                    continue
                rec = Record(i)
                for el in rec_el:
                    fname = (el.get("FIELDNAME") or el.tag).upper()
                    rec.fields[fname] = el.text or ""
                    rec.order.append(fname)
                    if el.tag.upper() in ("APP", "USERDEF"):
                        rec.app_fields.add(fname)
                parsed.records.append(rec)
    if not parsed.records and not parsed.header:
        raise AdifError("no ADX header or records found — is this an ADX file?")
    return parsed


def parse_file(path):
    p = Path(path)
    if not p.is_file():
        raise AdifError(f"file not found: {path}")
    raw = p.read_bytes()
    if not raw.strip():
        raise AdifError(f"file is empty: {path}")
    # ADX is XML: sniff for the <ADX> root after an optional BOM and
    # XML prolog, whatever the file is named. ElementTree takes the
    # raw bytes and honours the document's own encoding declaration.
    head = raw[:2048].decode("latin-1").lstrip("\ufeff \t\r\n")
    if p.suffix.lower() == ".adx" or head.startswith("<ADX") \
            or (head.startswith("<?xml") and "<ADX" in head):
        return parse_adx(raw)
    # ADI: parse a Latin-1 view so lengths count bytes (see parse_adi)
    return parse_adi(raw.decode("latin-1"))


# ---------------------------------------------------------------- helpers

_NUMBER_RE = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)")
_INTEGER_RE = re.compile(r"-?\d+")
_POSINT_RE = re.compile(r"\d+")
_DATE_RE = re.compile(r"\d{8}")
_TIME_RE = re.compile(r"\d{4}(?:\d{2})?")
_GRID_RE = re.compile(
    r"[A-R]{2}(?:\d{2}(?:[A-X]{2}(?:\d{2})?)?)?", re.I)
_GRID_EXT_RE = re.compile(r"[A-X]{2}(?:\d{2})?", re.I)
_LOCATION_RE = re.compile(r"([NSEW])(\d{3}) (\d{2}\.\d{3})", re.I)


def _valid_date(v):
    if not _DATE_RE.fullmatch(v):
        return False
    try:
        date(int(v[0:4]), int(v[4:6]), int(v[6:8]))
        return True
    except ValueError:
        return False


def _valid_time(v):
    if not _TIME_RE.fullmatch(v):
        return False
    hh, mm = int(v[0:2]), int(v[2:4])
    ss = int(v[4:6]) if len(v) == 6 else 0
    return hh <= 23 and mm <= 59 and ss <= 59


def _band_for_freq(freq, bands):
    for name, (lo, hi) in bands.items():
        if lo <= freq <= hi:
            return name
    return None


def _known_field(name, parsed, rec=None):
    return (name in SPEC["fields"] or name.startswith("APP_")
            or name in parsed.userdefs
            or (rec is not None and name in rec.app_fields))


# ---------------------------------------------------------------- checking

def check_record(rec, parsed, packs):
    out = []
    F = rec.fields

    def add(severity, field_name, rule, message, ref, pack="core"):
        out.append(Finding(rec.number, severity, field_name, rule,
                           message, ref, pack))

    # duplicate fields within the record (spec: a field may appear only once)
    seen = {}
    for name in rec.order:
        seen[name] = seen.get(name, 0) + 1
    for name, count in seen.items():
        if count > 1:
            add("error", name, "duplicate-field",
                f"field appears {count} times in one record; ADIF allows "
                f"each field at most once per record", REF_FIELDS)

    # unknown fields
    for name in rec.order:
        if not _known_field(name, parsed, rec):
            add("warning", name, "unknown-field",
                "not an ADIF 3.1.8 field, an APP_ field, or a field "
                "declared in the header's USERDEF list — other programs "
                "will ignore it, and it may be a misspelling", REF_FIELDS)

    # per-field datatype + enumeration checks, driven by fields.csv data
    # (each field once — a duplicated field's value is checked a single
    # time; the duplicate-field error above covers the repetition)
    for name in dict.fromkeys(rec.order):
        info = SPEC["fields"].get(name)
        if info is None:
            continue
        value = rec.get(name)
        if value == "":
            continue
        dtype = info["type"]
        if dtype == "Enumeration" and info["enum"]:
            enum = SPEC["enums"].get(info["enum"])
            if enum is not None:
                uv = value.upper()
                if name == "MODE" and uv not in enum["values"] \
                        and uv in SPEC["submodes"]:
                    pass  # _check_mode_submode reports this with the fix
                elif uv not in enum["values"]:
                    add("error", name, "enum",
                        f"{value!r} is not a value of the {info['enum']} "
                        f"enumeration", REF_ENUM)
                elif uv in enum["deleted"]:
                    add("warning", name, "enum-deleted",
                        f"{value!r} is a deleted {info['enum']} value — "
                        f"legal for old QSOs, but check it is really the "
                        f"entity logged at the time", REF_ENUM)
                elif uv in enum["import_only"]:
                    add("warning", name, "enum-import-only",
                        f"{value!r} is import-only in the {info['enum']} "
                        f"enumeration: accepted when reading old files, "
                        f"but it should not be written in new logs", REF_ENUM)
        elif dtype == "Date":
            if not _valid_date(value):
                add("error", name, "date",
                    f"{value!r} is not a valid date in YYYYMMDD form",
                    REF_FIELDS)
        elif dtype == "Time":
            if not _valid_time(value):
                add("error", name, "time",
                    f"{value!r} is not a valid time (HHMM or HHMMSS, UTC)",
                    REF_FIELDS)
        elif dtype in ("Number", "Integer", "PositiveInteger"):
            ok = (_NUMBER_RE.fullmatch(value) if dtype == "Number"
                  else _INTEGER_RE.fullmatch(value) if dtype == "Integer"
                  else _POSINT_RE.fullmatch(value))
            if not ok:
                add("error", name, "number",
                    f"{value!r} is not a valid ADIF {dtype} (decimal, no "
                    f"plus sign, no thousands separators)",
                    "ADIF 3.1.8 Resources §III.A")
            else:
                num = float(value)
                if info["min"] is not None and num < float(info["min"]):
                    add("error", name, "range",
                        f"{value!r} is below the field's minimum of "
                        f"{info['min']}", REF_FIELDS)
                if info["max"] is not None and num > float(info["max"]):
                    add("error", name, "range",
                        f"{value!r} is above the field's maximum of "
                        f"{info['max']}", REF_FIELDS)
        elif dtype == "Boolean":
            if value.upper() not in ("Y", "N"):
                add("error", name, "boolean",
                    f"{value!r} is not a Boolean (Y or N)", REF_FIELDS)
        elif dtype in ("GridSquare", "GridSquareExt"):
            if dtype == "GridSquare" and not _GRID_RE.fullmatch(value):
                add("warning", name, "gridsquare",
                    f"{value!r} does not match the Maidenhead grid-square "
                    f"grammar (2, 4, 6 or 8 characters)", REF_FIELDS)
            if dtype == "GridSquareExt" and not _GRID_EXT_RE.fullmatch(value):
                add("warning", name, "gridsquare",
                    f"{value!r} is not a grid-square extension (two "
                    f"letters A–X, optionally followed by two digits)",
                    REF_FIELDS)
        elif dtype == "Location":
            m = _LOCATION_RE.fullmatch(value)
            if not m:
                add("error", name, "location",
                    f"{value!r} is not in Location form (e.g. 'W021 22.892')",
                    REF_FIELDS)
            else:
                is_lat = name in ("LAT", "MY_LAT")
                if is_lat and m.group(1).upper() not in "NS":
                    add("error", name, "location",
                        f"latitude {value!r} must start with N or S",
                        REF_FIELDS)
                if not is_lat and name in ("LON", "MY_LON") \
                        and m.group(1).upper() not in "EW":
                    add("error", name, "location",
                        f"longitude {value!r} must start with E or W",
                        REF_FIELDS)
                deg_limit = 90 if is_lat else 180
                if int(m.group(2)) > deg_limit or float(m.group(3)) >= 60:
                    add("error", name, "location",
                        f"{value!r} has degrees/minutes out of range "
                        f"({'latitude' if is_lat else 'longitude'} degrees "
                        f"must be 0–{deg_limit})", REF_FIELDS)

    _check_mode_submode(rec, add)
    _check_band_freq(rec, add, "BAND", "FREQ")
    _check_band_freq(rec, add, "BAND_RX", "FREQ_RX")
    _check_dates(rec, add)
    _check_subdivisions(rec, add)
    _check_satellite(rec, add, packs)
    _check_minimum(rec, add, packs)
    if "lotw" in packs:
        _check_lotw(rec, add)
    if "eqsl" in packs:
        _check_eqsl(rec, add)
    return out


def _check_mode_submode(rec, add):
    mode = rec.get("MODE")
    submode = rec.get("SUBMODE")
    modes = SPEC["enums"]["Mode"]["values"]
    if mode:
        mu = mode.upper()
        if mu not in modes and mu in SPEC["submodes"]:
            add("error", "MODE", "mode-is-submode",
                f"{mode!r} is not a mode in ADIF 3.1.8 — it is a submode "
                f"of {SPEC['submodes'][mu]}. Write MODE={SPEC['submodes'][mu]} "
                f"with SUBMODE={mu}; as written, LoTW and eQSL will "
                f"reject the record", REF_ENUM)
    if submode:
        su = submode.upper()
        if su not in SPEC["submodes"]:
            add("error", "SUBMODE", "submode",
                f"{submode!r} is not a value of the Submode enumeration",
                REF_ENUM)
        elif su in SPEC["submode_import_only"]:
            add("warning", "SUBMODE", "submode-import-only",
                f"{submode!r} is an import-only submode: accepted when "
                f"reading old files, but it should not be written in new "
                f"logs", REF_ENUM)
        elif mode and SPEC["submodes"][su] != mode.upper():
            add("error", "SUBMODE", "submode-mode",
                f"SUBMODE {submode!r} belongs to MODE "
                f"{SPEC['submodes'][su]}, but this record has "
                f"MODE={mode!r} — the pairing is not in the spec and "
                f"upload services reject it", REF_ENUM)
        elif not mode:
            add("warning", "SUBMODE", "submode-mode",
                f"SUBMODE {submode!r} is present but MODE is missing; "
                f"its mode is {SPEC['submodes'][su]} — add "
                f"MODE={SPEC['submodes'][su]}", REF_ENUM)


def _check_band_freq(rec, add, band_field, freq_field):
    band = rec.get(band_field)
    freq_raw = rec.get(freq_field)
    freq = None
    if freq_raw and _NUMBER_RE.fullmatch(freq_raw):
        # gate on the ADIF Number grammar, not float(): float() also
        # accepts 'nan', 'inf' and '1e5', which are not ADIF numbers
        # (the Number check has already reported those)
        freq = float(freq_raw)
    if band and freq is not None:
        rng = SPEC["bands"].get(band.upper())
        if rng is not None and not (rng[0] <= freq <= rng[1]):
            inside = _band_for_freq(freq, SPEC["bands"])
            hint = (f"; {freq:g} MHz is in the {inside} band"
                    if inside else "; that frequency is in no ADIF band")
            add("error", band_field, "band-freq",
                f"BAND={band} covers {rng[0]:g}–{rng[1]:g} MHz but "
                f"{freq_field}={freq_raw} MHz disagrees{hint}", REF_ENUM)
    elif freq is not None and freq > 0 and _band_for_freq(freq, SPEC["bands"]) is None:
        add("warning", freq_field, "freq-no-band",
            f"{freq_raw} MHz falls in no ADIF band — check the frequency "
            f"for a typo", REF_ENUM)


def _check_dates(rec, add):
    qd, qdo = rec.get("QSO_DATE"), rec.get("QSO_DATE_OFF")
    ton, toff = rec.get("TIME_ON"), rec.get("TIME_OFF")
    if qd and _valid_date(qd):
        d = date(int(qd[0:4]), int(qd[4:6]), int(qd[6:8]))
        if d > date.today():
            add("warning", "QSO_DATE", "date-future",
                f"QSO date {qd} is in the future", REF_FIELDS)
    if qd and qdo and _valid_date(qd) and _valid_date(qdo):
        if qdo < qd:
            add("error", "QSO_DATE_OFF", "date-order",
                f"QSO_DATE_OFF {qdo} is before QSO_DATE {qd}", REF_FIELDS)
        elif ton and toff and _valid_time(ton) and _valid_time(toff):
            if qdo == qd and toff < ton:
                add("error", "TIME_OFF", "time-order",
                    f"TIME_OFF {toff} is before TIME_ON {ton} on the "
                    f"same date", REF_FIELDS)
    elif ton and toff and _valid_time(ton) and _valid_time(toff) \
            and toff < ton and not qdo:
        add("warning", "TIME_OFF", "time-order",
            f"TIME_OFF {toff} is earlier than TIME_ON {ton} and there "
            f"is no QSO_DATE_OFF — if the QSO ended after midnight UTC, "
            f"add QSO_DATE_OFF, or uploaders may read the times as "
            f"backwards", REF_FIELDS)


def _check_subdivisions(rec, add):
    # A STATE/CNTY code is legal only for its own DXCC entity, so the
    # check runs only when the entity is present AND the spec defines
    # a subdivision table for it. Most entities define none, and US
    # counties are not enumerated by ADIF at all (CNTY there is a
    # free-form "STATE,county" string) — those stay unchecked.
    for s_field, d_field, table in (
            ("STATE", "DXCC", SPEC["subdivisions"]),
            ("MY_STATE", "MY_DXCC", SPEC["subdivisions"]),
            ("CNTY", "DXCC", SPEC["subdivisions_secondary"]),
            ("MY_CNTY", "MY_DXCC", SPEC["subdivisions_secondary"])):
        s_val, entity = rec.get(s_field), rec.get(d_field)
        if not s_val or not entity or entity.strip() not in table:
            continue
        entry = table[entity.strip()]
        if s_val.upper() not in {v.upper() for v in entry["values"]}:
            add("error", s_field, "subdivision",
                f"{s_field}={s_val} is not a subdivision of DXCC entity "
                f"{entity.strip()} in the spec's table for that entity",
                REF_ENUM)
        elif s_val.upper() in {v.upper() for v in entry["deleted"]}:
            add("warning", s_field, "subdivision",
                f"{s_field}={s_val} is a deleted subdivision of DXCC "
                f"entity {entity.strip()} — fine for an old QSO, wrong "
                f"for a new one", REF_ENUM)
        elif s_val.upper() in {v.upper() for v in entry["import_only"]}:
            add("warning", s_field, "subdivision",
                f"{s_field}={s_val} is import-only for DXCC entity "
                f"{entity.strip()} — kept for old logs, not for new "
                f"records", REF_ENUM)


def _check_satellite(rec, add, packs):
    prop = (rec.get("PROP_MODE") or "").upper()
    sat_name = rec.get("SAT_NAME")
    in_lotw = "lotw" in packs
    if sat_name and prop != "SAT":
        add("error" if in_lotw else "warning", "PROP_MODE", "sat-pairing",
            "SAT_NAME is set but PROP_MODE is not SAT — LoTW does not "
            "accept satellite QSOs in this shape",
            REF_LOTW if in_lotw else REF_FIELDS,
            "lotw" if in_lotw else "core")
    if prop == "SAT" and not sat_name:
        add("error" if in_lotw else "warning", "SAT_NAME", "sat-pairing",
            "PROP_MODE is SAT but SAT_NAME is missing — LoTW does not "
            "accept satellite QSOs in this shape",
            REF_LOTW if in_lotw else REF_FIELDS,
            "lotw" if in_lotw else "core")


_MINIMUM = ("CALL", "QSO_DATE", "TIME_ON", "MODE")


def _check_minimum(rec, add, packs):
    missing = [f for f in _MINIMUM if not rec.has(f)]
    if not rec.has("BAND") and not rec.has("FREQ"):
        missing.append("BAND or FREQ")
    if not missing:
        return
    if packs:
        add("error", "", "minimum-fields",
            "record is missing " + ", ".join(missing) +
            " — upload services require these in every QSO record",
            REF_LOTW if "lotw" in packs else REF_EQSL,
            "lotw" if "lotw" in packs else "eqsl")
    else:
        add("warning", "", "minimum-fields",
            "record is missing " + ", ".join(missing) +
            " from the spec's minimum useful QSO field set "
            "(run with --lotw or --eqsl to make this an error)",
            REF_MINIMUM)


def _check_lotw(rec, add):
    call = rec.get("CALL")
    if call and (any(c.isspace() for c in call) or len(call) < 3):
        add("error", "CALL", "lotw-callsign",
            f"{call!r} does not look like a callsign LoTW will accept "
            f"(no spaces; at least 3 characters)", REF_LOTW, "lotw")


def _check_eqsl(rec, add):
    limits = EQSL["limits"]
    call = rec.get("CALL")
    if call and len(call) > limits["CALL"]:
        add("error", "CALL", "eqsl-length",
            f"callsign is {len(call)} characters; eQSL rejects CALL "
            f"longer than {limits['CALL']}", REF_EQSL, "eqsl")
    for fld in ("QSLMSG", "RST_SENT", "SAT_NAME"):
        v = rec.get(fld)
        if v and len(v) > limits[fld]:
            add("warning", fld, "eqsl-length",
                f"{fld} is {len(v)} characters; eQSL truncates it after "
                f"{limits[fld]}", REF_EQSL, "eqsl")
    # eQSL requires one of BAND / FREQ / SAT_MODE
    if not (rec.has("BAND") or rec.has("FREQ") or rec.has("SAT_MODE")):
        add("error", "", "eqsl-minimum",
            "record has none of BAND, FREQ or SAT_MODE — eQSL requires "
            "one of them", REF_EQSL, "eqsl")
    # eQSL's own mode table (which differs from ADIF's: e.g. it lists
    # FT8 as a mode and keeps legacy modes ADIF has dropped)
    mode = rec.get("MODE")
    submode = rec.get("SUBMODE")
    if mode:
        mu = mode.upper()
        if mu in EQSL["modes"]:
            if submode and submode.upper() not in EQSL["modes"][mu] \
                    and submode.upper() in SPEC["submodes"]:
                add("error", "SUBMODE", "eqsl-mode",
                    f"eQSL's table for MODE {mu} does not list SUBMODE "
                    f"{submode!r}", REF_EQSL, "eqsl")
        elif mu in SPEC["submodes"] and SPEC["submodes"][mu] in EQSL["modes"]:
            pass  # core already errors; eQSL converts some old-style logs
        else:
            add("error", "MODE", "eqsl-mode",
                f"{mode!r} is not in eQSL's accepted MODE table",
                REF_EQSL, "eqsl")
    band = rec.get("BAND")
    if band and band.upper() not in EQSL["bands"]:
        # warning, not error: eQSL's published table omits bands ADIF
        # 3.1.8 defines (630m, 8m, 5m, submm) even though eQSL says it
        # accepts up to 3.1.8 — the table appears to lag the spec, so
        # this is "expect manual intervention", not a certain rejection
        add("warning", "BAND", "eqsl-band",
            f"{band!r} is not in eQSL's published BAND table — eQSL "
            f"says non-matching bands need manual intervention to fix, "
            f"and its table lags ADIF 3.1.8 (it has no 630m, 8m, 5m or "
            f"submm)", REF_EQSL, "eqsl")
    sat_mode = rec.get("SAT_MODE")
    if sat_mode and sat_mode.upper() not in EQSL["sat_modes"]:
        add("warning", "SAT_MODE", "eqsl-sat-mode",
            f"{sat_mode!r} is not in eQSL's SAT_MODE conversion table, "
            f"so eQSL cannot derive a band from it", REF_EQSL, "eqsl")


def check_parsed(parsed, packs):
    findings = list(parsed.findings)
    for rec in parsed.records:
        findings.extend(check_record(rec, parsed, packs))
    if "lotw" in packs:
        findings.extend(_check_duplicates(parsed))
    return findings


def _check_duplicates(parsed):
    out = []
    seen = {}
    for rec in parsed.records:
        key = (
            (rec.get("CALL") or "").upper(),
            rec.get("QSO_DATE") or "",
            (rec.get("TIME_ON") or "")[:4],
            (rec.get("BAND") or rec.get("FREQ") or "").upper(),
            (rec.get("MODE") or "").upper(),
            (rec.get("SUBMODE") or "").upper(),
        )
        if all(key[:5]):
            if key in seen:
                out.append(Finding(
                    rec.number, "warning", "", "lotw-duplicate",
                    f"same call, date, time (to the minute), band and "
                    f"mode as record {seen[key]} — LoTW does not accept "
                    f"duplicate QSOs", REF_LOTW, "lotw"))
            else:
                seen[key] = rec.number
    return out


def record_label(rec):
    bits = []
    if rec.get("CALL"):
        bits.append(rec.get("CALL"))
    if rec.get("QSO_DATE"):
        bits.append(rec.get("QSO_DATE"))
    return ", ".join(bits)
