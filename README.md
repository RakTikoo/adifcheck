# adifcheck — check a ham-radio ADIF log's contents before you upload it

**Check a ham-radio log's contents — the values LoTW and eQSL actually reject — before you upload it.**

Every logger can write an ADIF file that is perfectly well-formed and still
gets QSOs rejected on upload: a submode written in the MODE field, a band
that disagrees with the frequency, a satellite contact with no propagation
mode, a date that isn't a date. The existing checkers look at the file's
punctuation. `adifcheck` looks at its *meaning*: it validates every record
against the ADIF 3.1.8 specification's own published tables — modes,
submodes and their pairings, bands and their frequency ranges, dates,
times, and the cross-field rules — and, optionally, against the documented
acceptance rules of LoTW and eQSL. Each finding names the record, the
field, what's wrong, and the source the rule comes from.

## Install

```console
$ pip install git+https://github.com/RakTikoo/adifcheck
```

Requires Python ≥ 3.10. No dependencies — the standard library only — and
everything runs **offline**: the specification tables are bundled as data
files generated from ADIF's published exports.

## Demo (real output)

```console
$ adifcheck examples/messy.adi --lotw --eqsl
examples/messy.adi: 5 records, 8 errors, 1 warnings
  record 1 (W1AW, 20261101): BAND: error: BAND=20m covers 14–14.35 MHz but FREQ=7.074 MHz disagrees; 7.074 MHz is in the 40M band [ADIF 3.1.8 §III.B]
  record 2 (K7ABC, 20261301): QSO_DATE: error: '20261301' is not a valid date in YYYYMMDD form [ADIF 3.1.8 §III.C]
  record 2 (K7ABC, 20261301): MODE: error: 'FT4' is not a mode in ADIF 3.1.8 — it is a submode of MFSK. Write MODE=MFSK with SUBMODE=FT4; as written, LoTW and eQSL will reject the record [ADIF 3.1.8 §III.B]
  record 3 (JA1XYZ, 20261102): PROP_MODE: error: SAT_NAME is set but PROP_MODE is not SAT — LoTW does not accept satellite QSOs in this shape [LoTW documented behaviour (DX Lab wiki)]
  record 4 (G4QQ, 20261102): BAND: error: '25m' is not a value of the Band enumeration [ADIF 3.1.8 §III.B]
  record 4 (G4QQ, 20261102): SUBMODE: error: SUBMODE 'FT4' belongs to MODE MFSK, but this record has MODE='SSB' — the pairing is not in the spec and upload services reject it [ADIF 3.1.8 §III.B]
  ...
result: FAIL — 8 error(s) that will cause rejections or dropped records
```

A clean log is quiet:

```console
$ adifcheck examples/clean.adi --lotw --eqsl
examples/clean.adi: 4 records, 0 errors, 0 warnings
result: clean — no findings
```

## Usage

```
adifcheck FILE [FILE ...] [--lotw] [--eqsl] [--strict] [--json]
```

- `FILE` — one or more ADIF logs, `.adi` or `.adx` (XML) format.
- `--lotw` — add the LoTW rule pack: the minimum fields become errors,
  satellite name/propagation-mode mismatches become errors, and duplicate
  QSOs (same call, date, minute, band, mode) are flagged.
- `--eqsl` — add the eQSL rule pack: eQSL's minimum record contents, its
  call-length and truncation limits, and checks against eQSL's own
  published MODE/SUBMODE and BAND tables (which differ from ADIF's).
- `--strict` — warnings fail the run too.
- `--json` — machine-readable findings.

Exit codes: `0` clean, `1` errors found, `2` a file could not be read at
all. User errors are one-line messages, never tracebacks.

## Use in CI

This repo is also a composite GitHub Action: it installs adifcheck from this repo at the ref you pin, and checks the log files you name. The step fails when adifcheck exits 1 (errors found — the records an upload service would reject) or 2 (a file could not be read).

```yaml
- uses: RakTikoo/adifcheck@main
  with:
    paths: logs/station.adi
    lotw: "true"
    eqsl: "true"
```

`paths` takes one or more files, space-separated. `lotw`, `eqsl` and `strict` are `"false"` unless you set them to `"true"`.

As a [pre-commit](https://pre-commit.com) hook, checking each log as you commit it:

```yaml
- repo: https://github.com/RakTikoo/adifcheck
  rev: fb84f5f9c359a402343e90ec1bf9c38e8837f4e7
  hooks:
    - id: adifcheck
```

### What the core checks cover

- **Enumeration membership** for every enumeration-typed field (MODE,
  BAND, PROP_MODE, DXCC, CONT, QSL statuses, …), case-insensitively.
  Import-only (deprecated) values are warnings; deleted DXCC entities are
  flagged so you can check old QSOs deliberately.
- **Mode/submode pairing.** A submode in the MODE field (FT4 is the
  classic) is reported with the correct pairing to write; a submode
  paired with the wrong mode is an error.
- **Band ↔ frequency agreement** using the spec's band ranges, for both
  BAND/FREQ and BAND_RX/FREQ_RX. A frequency in no ADIF band is a warning.
- **Dates and times**: real calendar dates, valid HHMM/HHMMSS times,
  QSO_DATE_OFF not before QSO_DATE, and a warning when TIME_OFF precedes
  TIME_ON with no QSO_DATE_OFF (the after-midnight case).
- **Data-type grammar** from the spec's field table: ADIF numbers (no
  plus sign, no thousands separators), field min/max ranges (e.g. AGE,
  ANT_AZ), Booleans, Maidenhead grid squares and extensions, and
  Location values including the latitude/longitude hemisphere letter.
- **State/county membership, keyed by DXCC entity.** A STATE code is
  only legal for its own entity, so when a record carries DXCC (or
  MY_DXCC), STATE/MY_STATE are checked against the spec's subdivision
  table for that entity (66 entities define one) — and CNTY against the
  secondary table where the spec defines one.
- **Satellite pairing**: SAT_NAME without PROP_MODE=SAT, and vice versa
  (a warning in the core, an error under `--lotw`, matching LoTW's
  documented rejection list).
- **File shape**: duplicate fields in one record, declared lengths that
  don't match the data, a final record with no `<EOR>`, malformed tags,
  and unknown field names (a warning — often a misspelling).

## Where the rules come from

- The **core** tables (33 bands, 91 modes, 188 submodes, 185 fields, 19
  enumerations) are generated by `tools/build_data.py` from the ADIF
  3.1.8 specification's own machine-readable exports — not hand-copied —
  and the test suite pins the checker to ADIF's official test QSO files:
  6,202 generated records in both `.adi` and `.adx` form must produce
  **zero errors**.
- The **LoTW pack** implements the rejection causes documented in the
  DX Lab wiki ("Determining why an uploaded QSO was not accepted by
  LoTW"). That is documented behaviour, not an ARRL-published
  machine-readable spec — a softer oracle than the spec tables, and the
  findings say so.
- The **eQSL pack** implements eQSL.cc's published ADIF Content
  Specifications (minimum contents, length limits, its MODE/BAND tables),
  extracted from that page. Same caveat: documented behaviour. One
  visible consequence: eQSL's published band table omits 630m, 8m, 5m and
  submm even though eQSL says it accepts ADIF 3.1.8, so those bands are
  a *warning* ("expect manual intervention"), never an error.

## Honest limits

Two grades of rule live in this tool, and the findings never blur them:

- **Spec-proven (the core).** Every core check comes from the ADIF 3.1.8
  specification's own published tables and field definitions, and the
  test suite proves the core against the spec's own test files. If the
  core calls something an error, the spec says so.
- **Behaviour-based (the `--lotw` / `--eqsl` packs).** No upload service
  publishes a machine-readable rulebook. The packs implement what the
  services *document* — LoTW's rejection causes as listed in the DX Lab
  wiki, eQSL's published content-spec page — which is a softer source
  than the spec tables: documentation can lag the service's real
  behaviour. Pack findings cite their document, not the spec, and where
  a document looks stale (eQSL's band table) the finding is a warning,
  not an error.

And some things cannot be decided from the file alone, by any local
tool — adifcheck does not pretend otherwise:

- **Whether a callsign is really allocated**, whether a satellite name
  is a real satellite, and which DXCC entity a callsign belongs to are
  lookups against external databases, not properties of the file.
- **Service-side state is invisible locally**: LoTW callsign-certificate
  date ranges and expiry, whether the server has processed an upload
  yet, and your eQSL account profile (which its spec says some fields
  must match) are all outside the file.
- **Regional band plans** narrower than the ADIF band ranges are
  outside the spec, so outside this tool.

Rule-by-rule limits:

- **Subdivision checks need the DXCC entity in the record.** STATE and
  CNTY are checked when the record carries DXCC/MY_DXCC *and* the spec
  defines a subdivision table for that entity. Without the entity field
  there is nothing honest to check against, and most entities define no
  table. US counties are not enumerated by ADIF at all — CNTY there is a
  free-form "STATE,county" string — so US CNTY values are never
  membership-checked. (This limit is what's left after the check was
  built and proven against the spec's own test files; the earlier plan
  to skip subdivision checking entirely was dropped.)
- **CONTEST_ID is not checked against the Contest ID enumeration** — the
  spec types the field as String (the enumeration is advisory), and the
  ADX schema skips it too.
- **Free text between records is ignored.** ADIF's own test files
  interleave annotation lines between records, and many loggers emit
  comments; adifcheck checks field contents, not commentary.
- **Frequencies exactly on a band edge** are read against the spec's
  published lower/upper ranges, inclusive. If your service applies a
  narrower allocation (a national band plan, say), that is outside the
  ADIF spec and outside this tool.

## Use with an AI agent

This repo ships a [SKILL.md](SKILL.md) written to the Agent Skills open standard. If your coding agent supports skills, point it at this repo and it can install adifcheck, run it on your logs before upload, and read the findings for you. The skill works when the repo is cloned under its real name (`adifcheck`) — a skill's name must match its folder.

## Tests

```console
$ python3 run_tests.py
```

61 tests (stdlib `unittest`): the spec-data pins (counts, submode→mode
integrity, non-empty import-only/deleted sets), zero errors on ADIF's
official 3.1.8 test QSO files in both formats, a planted-error corpus
(23 records, one known problem each across every claimed check class —
all must be caught, and its 3 clean records must stay silent), every
core rule on synthetic records, both rule packs, parser edge cases
(length mismatches, missing `<EOR>`, multiline values, header-less
files, malformed ADX, hostile length specifiers), and CLI exit codes /
JSON output. An independent adversarial review of v0.1.0 is in
`REVIEW.md`, with its findings and their resolutions.

One honest consequence of the packs being behaviour-based: with
`--lotw --eqsl` switched on, ADIF's own test files produce 3 errors.
The test QSOs are mechanical enumeration exercises, and three of them
trip the packs' documented rules (a satellite record with no SAT_NAME;
two submodes missing from eQSL's published table). The zero-error pin
above is a core-rules claim, and it is stated that way.

The bundled specification data is the ADIF Developers Group's work,
reproduced from their published exports; the official test files in
`tests/fixtures/` carry their own notice. The eQSL tables are eQSL.cc's
published upload requirements, extracted from their content-spec page.

## License

MIT. Free and open source.
