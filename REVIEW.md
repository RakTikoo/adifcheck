# Adversarial review of adifcheck (independent)

Reviewer did not build this tool. All results below are from running the
code and from a fresh download of the ADIF 3.1.8 spec exports on
2026-11-18. Probe files are in `/tmp/probes/` (names cited per finding).

## Verdict

**Ship after fixes.**

The core promise is real: the checker produces zero errors on ADIF's own
6,202-record test files in both formats, its bundled tables match the
spec exports exactly, and 25 of 34 adversarial probes behaved correctly
(including XXE and billion-laughs, which fail cleanly). But there is one
crash (a traceback, which the README explicitly promises never happens),
and two documented warning classes can never fire because of a data
generation bug — the README advertises behaviour the shipped data makes
impossible. Those three must be fixed before this is announced; the
rest are minor.

## Verified claims (with numbers)

- **Test suite**: `python3 run_tests.py` → "Ran 46 tests in 1.363s / OK",
  exit 0.
- **Official fixtures, core rules** (README claims zero errors):
  - `ADIF_318_test_QSOs_2026_10_12.adi`: 6202 records, **0 errors**,
    23 warnings, exit 0. ✔ claim verified.
  - `ADIF_318_test_QSOs_2026_10_12.adx`: 6202 records, **0 errors**,
    23 warnings, exit 0. ✔ claim verified.
  - The 23 warnings in each: 22× "TIME_OFF earlier than TIME_ON, no
    QSO_DATE_OFF" and 1× "PROP_MODE=SAT but SAT_NAME missing" — plausible
    true positives on mechanically generated records.
- **Official fixtures with `--lotw --eqsl`**: both formats give
  3 errors, 743 warnings, exit 1 (see Finding 11).
- **Spec spot checks**, re-derived from a fresh download of
  `https://adif.org.uk/318/resources` (`318/exports/csv/`):
  - (a) Band enumeration has **33** rows; **20m = 14.0–14.35 MHz**.
    Bundled `adif318.json` matches (runtime-normalised `"20M": [14.0, 14.35]`).
  - (b) **FT8 is a Mode** in 3.1.8 and is **not** in the Submode table;
    **FT4 is a Submode whose mode is MFSK** and is not a Mode.
    Bundled data matches on all four points.
  - (c) Propagation_Mode enumeration has **20** values; bundled data
    has 20. (Also checked: Mode 91 ✔, Submode 188 ✔, fields.csv has 186
    rows vs 185 bundled — the skipped row is the `USERDEFn` template,
    which `build_data.py` intentionally filters.)
- **Data reproducibility**: copying the project, re-running
  `tools/build_data.py` against the fresh spec CSVs and a live fetch of
  the eQSL content-spec page reproduces both bundled JSON files
  identically. The pipeline is sound; Finding 2 is a logic bug inside
  it, not stale data.
- **Exit codes**: 0 (clean / warnings-only), 1 (errors; also
  warnings-only under `--strict`), 2 (unreadable file) — all as
  documented. Multi-file runs continue past a missing file, report the
  others, and exit 2.
- **`--json`**: output parses as valid JSON for the messy example
  (8 errors reported) and for an unreadable file (error object form).
- **XML attacks fail cleanly**: ADX with an external entity
  (`xxe.adx`) → one-line error "undefined entity &xxe;", exit 2, no
  file disclosure. A 7-level billion-laughs file (`laughs_big.adx`) →
  one-line error "limit on input amplification factor … breached",
  exit 2 (the expat defence is active in this Python build). A small
  internal-entity file is expanded per normal XML rules and checked.
- **Probes that behaved correctly**: empty file (clean error, exit 2);
  1 KB of binary garbage (clean error, exit 2); header-only file
  (0 records, exit 0); `<` inside a length-delimited value (honoured,
  clean); all-lowercase tags and values (clean); band edges exactly
  14.0 and 14.35 MHz (pass — inclusive, as documented) and 14.3500001
  (band-freq error); FREQ=14230 with BAND=20m (error) and without BAND
  (freq-no-band warning); TIME_ON=240000 (time error); QSO_DATE=20260229
  (date error) vs 20240229 (passes); PROP_MODE=SAT with SAT_NAME empty
  or all-whitespace (sat-pairing warning); duplicate fields in ADX
  (duplicate-field error); CRLF line endings throughout (clean);
  a single record with a 5 MB COMMENT (clean, ~instant); LoTW
  duplicates differing only in seconds (flagged — to-the-minute, as
  documented) and differing in SUBMODE (correctly not flagged).

## Findings

### 1. CRITICAL — Traceback on a length specifier with more than 4300 digits
`core.py:197` does `ln = int(length)` on an unbounded `\d+` regex match.
Python's int-conversion limit turns a hostile/corrupt tag into an
uncaught `ValueError`, and the CLI prints a full traceback — breaking
the README's "never tracebacks" promise, in both text and `--json`
modes (no JSON is emitted).
Repro (`/tmp/probes/bignum_tag.adi`):
`<EOH>\n<CALL:` + `9`×5000 + `>W1AW<EOR>` →
`ValueError: Exceeds the limit (4300 digits) for integer string conversion`.

### 2. MAJOR — Import-only and deleted enumeration warnings can never fire (data bug)
The spec exports mark rows with the literal strings `"Import-only"` and
`"Deleted"` in those columns (DXCC uses `"Deleted"`; ARRL Section
expresses deletion via a "Deleted Date" column). `tools/build_data.py`
compares against `"true"` instead (in the enumeration, submode, and
field paths). Result: **every** `import_only` and `deleted` list in the
bundled `adif318.json` is empty, so the `enum-import-only`,
`enum-deleted`, and `submode-import-only` rules in `core.py` are dead
code. This silently disables behaviour the README advertises
("Import-only (deprecated) values are warnings; deleted DXCC entities
are flagged"). Affected real values include 42 import-only modes,
QSL_Rcvd `V`, and 62 deleted DXCC entities.
Repros (both currently report "clean", exit 0; both should warn):
- `/tmp/probes/importonly_qsl.adi` — valid record plus `<QSL_RCVD:1>V`.
- `/tmp/probes/deleted_dxcc.adi` — valid record plus `<DXCC:1>2`
  (entity code 2, "ABU AIL IS.", is marked Deleted in the spec export).
No test covers either warning class.

### 3. MAJOR — A header-less log containing the literal text `<EOH` in a value is silently reported as 0 records, clean
`parse_adi` decides "has a header" by `" <EOH" in text.upper()` over the
whole file. In a header-less file, if any field *value* contains the
substring `<EOH` (e.g. a NOTES field mentioning the marker), every
record is filed as header text and the run reports success with zero
records — the worst failure mode for a pre-upload checker, because the
user is told a log they never actually checked is clean. The code
comment acknowledges the heuristic but understates it: no records are
"invented", but all of them are *lost*, silently.
Repro (`/tmp/probes/headerless_eoh.adi`): one valid record whose
`<NOTES:12>see <EOH> ok` → output "0 records, 0 errors, 0 warnings /
result: clean", exit 0.

### 4. MODERATE — A truncated final field's real error is swallowed when the file has no header fields
When parsing ends with findings but no records and an empty header,
`parse_adi` raises `AdifError("no ADIF header or records found — is
this an ADI file?")`, discarding the precise syntax finding. The same
truncated file with one header field present exits 1 with the correct
"file ends inside field <CALL>" error. Inconsistent and misleading.
Repros: `/tmp/probes/toolong.adi` (`<EOH>\n<CALL:99>W1AW`) → exit 2,
generic message; `/tmp/probes/toolong2.adi` (same, preceded by an
ADIF_VER header field) → exit 1, precise error.

### 5. MODERATE — Duplicated fields are value-checked once per occurrence (identical findings repeated)
`check_record` iterates `rec.order`, which contains a duplicated field
name twice, while `rec.fields` holds one effective value — so the same
finding is emitted twice, in addition to the duplicate-field error.
Repro (`/tmp/probes/dup_bad_band.adi`): BAND twice, both `25m` →
3 errors: duplicate-field + the identical "'25m' is not a value of the
Band enumeration" error ×2.

### 6. MINOR — Band↔frequency check trusts `float()` on values already rejected as non-numbers
`_check_band_freq` uses `float(freq_raw)` in a try/except, but Python's
`float()` accepts `nan`, `inf`, `1e5`, and `1_0`, none of which are
ADIF Numbers. The Number check errors, and then band-freq adds a second
error computed from the nonsense value.
Repros: `/tmp/probes/freq_nan.adi` (extra error "FREQ=nan MHz
disagrees; that frequency is in no ADIF band") and
`/tmp/probes/freq_sci.adi` (FREQ=1e5 treated as 100000 MHz).
Gate band-freq on the ADIF Number regex instead.

### 7. MINOR — ADX with an XML prolog in a file not named `.adx` is rejected as "not an ADI file"
Format detection is `suffix == .adx or text.startswith("<ADX")`. A
valid ADX document starting with `<?xml version="1.0"?>` in a `.adi`
file goes to the ADI parser and fails with the misleading "no ADIF
header or records found". Repro: `/tmp/probes/adx_named_adi.adi`.

### 8. MINOR — Latitude degrees are only range-checked against 180, not 90
The Location check rejects degrees > 180 for both LAT and LON, so an
impossible latitude passes. (The range test also sits in an `elif`
chain, so a wrong-hemisphere value skips it entirely.)
Repro (`/tmp/probes/lat120.adi`): `<LAT:11>N120 00.000` → clean.

### 9. MINOR — GridSquare check accepts 10 characters; its own message says 2, 4, 6 or 8
`_GRID_RE` allows a fifth pair, so a 10-character GRIDSQUARE passes
silently, contradicting the warning text (and the datatype description
the message paraphrases). Repro (`/tmp/probes/grid10.adi`):
`<GRIDSQUARE:10>IO91AB12CD` → clean.

### 10. MINOR — Byte-counted UTF-8 lengths cascade into a second, spurious error
For a non-ASCII value whose length was counted in bytes (a common
logger behaviour), the parser reports the genuine length mismatch and
then, because resync consumed the `<` of `<EOR>`, a spurious "last
record has no <EOR>" error — two errors, one cause. Character-counted
lengths parse cleanly. Repro: `/tmp/probes/utf8_bytelen.adi`
(`<COMMENT:5>café`) vs `/tmp/probes/utf8_charlen.adi` (`<COMMENT:4>café`,
clean).

### 11. OBSERVATION — With both packs on, ADIF's own test files yield 3 errors
Record 3937 (PROP_MODE=SAT, no SAT_NAME → LoTW error) and records
6031/6035 (SUBMODEs FSK31/FSQCALL absent from eQSL's table for their
modes → eQSL errors). The test QSOs are mechanical enumeration
exercises, not realistic logs, so these are plausibly true positives
against the packs' documented rules — but "official file + packs" is
the first combination a sceptical user will try, so the maintainer
should consciously accept or address it. The README's zero-error claim
is correctly scoped to the core rules.

### 12. OBSERVATION — Inconsistent record attribution for parse findings
In `parse_adi`, malformed-tag findings are attributed to `rec_no`
(the *previous* record) while missing-length and length-mismatch
findings use `rec_no + 1` (the record being read). Cosmetic, but it
can send a user hunting in the wrong record.

## Fix wave (maintainer addendum, 2026-11-18 — after the review above)

Every finding was fixed or consciously accepted, then everything was
re-measured:

1. **FIXED** — lengths with >9 digits are a syntax finding, never an
   int() conversion. Regression test added.
2. **FIXED** — `build_data.py` now matches the exports' literal markers
   ("Import-only", "Deleted", non-empty "Deleted Date"); regenerated
   data has 42 import-only modes, 62 deleted DXCC entities, QSL_RCVD=V
   import-only. Both repros now warn; data tests pin the sets non-empty.
3. **FIXED** — if the "<EOH" guess files record-typical fields as
   "header" and yields zero records, the file is reparsed header-less.
   The repro now returns its record. Regression test added.
4. **FIXED** — parse findings are returned (exit 1) instead of being
   discarded behind the generic not-an-ADI error. Regression test added.
5. **FIXED** — per-field checks iterate unique field names; a
   duplicated field's value is checked once. Regression test added.
6. **FIXED** — band↔frequency gated on the ADIF Number regex.
7. **FIXED** — ADX sniffed after BOM/prolog regardless of file name.
   Regression test added.
8. **FIXED** — latitude range is 0–90, and hemisphere/range checks no
   longer hide behind an elif chain. Regression test added.
9. **FIXED** — GridSquare regex aligned to the datatype's 2/4/6/8
   characters. Regression test added.
10. **FIXED** — ADI is parsed byte-oriented (Latin-1 view, values
    decoded as UTF-8 when valid): byte-counted UTF-8 lengths parse
    cleanly with no cascade. Regression test added. (Character-counted
    UTF-8 lengths in .adi now mismatch — .adi is ASCII per spec, and
    byte counting is what loggers actually write.)
11. **ACCEPTED, documented** — the 3 pack errors on the official files
    are true positives against the packs' documented rules; the README
    Tests section now states this and scopes the zero-error claim to
    the core rules.
12. **FIXED** — parse findings attribute consistently to the record
    being read.

Re-measurement after the wave: 61/61 tests pass (46 original + 11
review regressions + 3 subdivision + 1 corpus); official files still
0 errors in both formats (warnings 23 → 27, the four new ones being
subdivision warnings from the newly-enabled checks); planted corpus
still 23/23 caught with 0 false positives; fresh-venv install proof
re-run clean. A gap the review did not cover — STATE/CNTY membership —
was also closed in this wave: checked against the spec's subdivision
tables keyed by DXCC entity, with the official files as the
false-positive bar (they stayed at zero errors).

## Suggestions

1. Cap or guard the length conversion (e.g. reject a length token
   longer than ~10 digits as a syntax error) and add a regression test
   with a 5000-digit length. Audit for other unbounded `int()`/`float()`
   conversions on attacker-controlled text.
2. Fix `build_data.py` marker handling: treat `"Import-only"` /
   `"Deleted"` (and a non-empty "Deleted Date" for ARRL Section) as the
   markers; regenerate; add tests that pin at least one import-only
   warning (QSL_RCVD=V) and one deleted-entity warning (a deleted DXCC
   code), plus a data test asserting the bundled import_only/deleted
   sets are non-empty where the spec has them.
3. Replace the `"<EOH" in text` heuristic with a structural test (an
   `<EOH>` *tag* encountered before the first `<EOR>`), and if a
   "header" ends up containing record-like field runs with no EOH ever
   seen, say so loudly instead of reporting 0 records clean.
4. Don't raise `AdifError` when parse findings already exist — return
   them and let the CLI exit 1 with the precise messages.
5. De-duplicate per-field checks for duplicated field names (check the
   effective value once; the duplicate-field error already covers the
   repetition).
6. Gate `_check_band_freq` on `_NUMBER_RE.fullmatch` rather than
   `float()` success.
7. Sniff ADX by looking for `<ADX` after an optional BOM/XML prolog,
   regardless of file suffix.
8. Tighten Location latitude to ≤ 90 degrees and align the GridSquare
   regex with its documented lengths (or correct the message).
9. Add regression tests for every probe in `/tmp/probes/` — especially
   Findings 1–4, which the current 46 tests all miss.
