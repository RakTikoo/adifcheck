"""Tests for adifcheck.

The two strongest pins are the official ADIF 3.1.8 test QSO files
(tests/fixtures/): 6202 records generated from the spec's own tables.
The core checker must report ZERO errors on both, in .adi and .adx form.
Everything else is unit tests over small synthetic logs.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adifcheck import core
from adifcheck.cli import main

FIXTURES = ROOT / "tests" / "fixtures"
OFFICIAL_ADI = FIXTURES / "ADIF_318_test_QSOs_2026_10_12.adi"
OFFICIAL_ADX = FIXTURES / "ADIF_318_test_QSOs_2026_10_12.adx"


def run_cli(*argv):
    return main(list(argv))


def check_text(text, packs=frozenset()):
    parsed = core.parse_adi(text)
    return core.check_parsed(parsed, packs)


def one_record(**fields):
    tags = "".join(f"<{k}:{len(str(v))}>{v}" for k, v in fields.items())
    return f"<EOH>\n{tags}<EOR>"


class TestSpecData(unittest.TestCase):
    def test_counts_match_adif_318(self):
        self.assertEqual(core.SPEC["version"], "3.1.8")
        self.assertEqual(len(core.SPEC["bands"]), 33)
        self.assertEqual(len(core.SPEC["enums"]["Mode"]["values"]), 91)
        self.assertEqual(len(core.SPEC["submodes"]), 188)
        self.assertEqual(len(core.SPEC["enums"]["Propagation_Mode"]["values"]), 20)
        self.assertEqual(len(core.SPEC["fields"]), 185)
        self.assertEqual(len(core.SPEC["enums"]["DXCC_Entity_Code"]["values"]), 403)

    def test_every_submode_points_at_a_real_mode(self):
        modes = core.SPEC["enums"]["Mode"]["values"]
        for sub, mode in core.SPEC["submodes"].items():
            self.assertIn(mode, modes, f"submode {sub} -> unknown mode {mode}")

    def test_ft8_is_a_mode_in_318_not_a_submode(self):
        # ADIF 3.1.8 promoted FT8 to a full mode; FT4 stayed a submode.
        self.assertIn("FT8", core.SPEC["enums"]["Mode"]["values"])
        self.assertNotIn("FT8", core.SPEC["submodes"])
        self.assertEqual(core.SPEC["submodes"]["FT4"], "MFSK")

    def test_band_ranges_are_sane(self):
        for name, (lo, hi) in core.SPEC["bands"].items():
            self.assertGreater(hi, lo, name)
        self.assertEqual(core.SPEC["bands"]["20M"], [14.0, 14.35])

    def test_eqsl_tables_extracted(self):
        self.assertEqual(len(core.EQSL["modes"]), 50)
        self.assertEqual(core.EQSL["limits"]["CALL"], 13)
        self.assertEqual(core.EQSL["modes"]["MFSK"][:3],
                         ["FST4", "FST4W", "FT2"])


class TestOfficialTestFiles(unittest.TestCase):
    def test_official_adi_zero_errors(self):
        parsed = core.parse_file(str(OFFICIAL_ADI))
        self.assertEqual(len(parsed.records), 6202)
        findings = core.check_parsed(parsed, frozenset())
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], [f.line() for f in errors[:5]])

    def test_official_adx_zero_errors(self):
        parsed = core.parse_file(str(OFFICIAL_ADX))
        self.assertEqual(len(parsed.records), 6202)
        findings = core.check_parsed(parsed, frozenset())
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], [f.line() for f in errors[:5]])

    def test_official_files_agree(self):
        adi = core.check_parsed(core.parse_file(str(OFFICIAL_ADI)), frozenset())
        adx = core.check_parsed(core.parse_file(str(OFFICIAL_ADX)), frozenset())
        self.assertEqual(len(adi), len(adx))


class TestCleanAndMessyExamples(unittest.TestCase):
    def test_clean_example_is_clean_with_packs(self):
        rc = run_cli(str(ROOT / "examples" / "clean.adi"), "--lotw", "--eqsl")
        self.assertEqual(rc, 0)

    def test_messy_example_fails(self):
        rc = run_cli(str(ROOT / "examples" / "messy.adi"), "--lotw", "--eqsl")
        self.assertEqual(rc, 1)

    def test_adx_example_is_clean(self):
        rc = run_cli(str(ROOT / "examples" / "sample.adx"))
        self.assertEqual(rc, 0)


class TestCoreRules(unittest.TestCase):
    def _errors(self, text, packs=frozenset()):
        return [f for f in check_text(text, packs) if f.severity == "error"]

    def test_bad_band(self):
        fs = self._errors(one_record(CALL="W1AW", QSO_DATE="20261101",
                                     TIME_ON="1800", BAND="25m", MODE="SSB"))
        self.assertEqual([f.field for f in fs], ["BAND"])

    def test_band_freq_disagreement(self):
        fs = self._errors(one_record(CALL="W1AW", QSO_DATE="20261101",
                                     TIME_ON="1800", BAND="20m",
                                     FREQ="7.074", MODE="SSB"))
        self.assertEqual([(f.field, f.rule) for f in fs],
                         [("BAND", "band-freq")])

    def test_band_freq_agreement_passes(self):
        self.assertEqual(self._errors(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            FREQ="14.230", MODE="SSB")), [])

    def test_submode_in_mode_field(self):
        fs = self._errors(one_record(CALL="W1AW", QSO_DATE="20261101",
                                     TIME_ON="1800", BAND="40m", MODE="FT4"))
        self.assertEqual([f.rule for f in fs], ["mode-is-submode"])
        self.assertIn("MFSK", fs[0].message)

    def test_submode_wrong_mode(self):
        fs = self._errors(one_record(CALL="W1AW", QSO_DATE="20261101",
                                     TIME_ON="1800", BAND="20m", MODE="SSB",
                                     SUBMODE="FT4"))
        self.assertEqual([f.rule for f in fs], ["submode-mode"])

    def test_unknown_submode(self):
        fs = self._errors(one_record(CALL="W1AW", QSO_DATE="20261101",
                                     TIME_ON="1800", BAND="20m", MODE="SSB",
                                     SUBMODE="WIBBLE"))
        self.assertEqual([f.rule for f in fs], ["submode"])

    def test_impossible_date(self):
        fs = self._errors(one_record(CALL="W1AW", QSO_DATE="20261301",
                                     TIME_ON="1800", BAND="20m", MODE="SSB"))
        self.assertEqual([f.rule for f in fs], ["date"])

    def test_impossible_time(self):
        fs = self._errors(one_record(CALL="W1AW", QSO_DATE="20261101",
                                     TIME_ON="2561", BAND="20m", MODE="SSB"))
        self.assertEqual([f.rule for f in fs], ["time"])

    def test_bad_number_format(self):
        fs = self._errors(one_record(CALL="W1AW", QSO_DATE="20261101",
                                     TIME_ON="1800", BAND="20m", MODE="SSB",
                                     TX_PWR="+100"))
        self.assertEqual([f.rule for f in fs], ["number"])

    def test_age_out_of_range(self):
        fs = self._errors(one_record(CALL="W1AW", QSO_DATE="20261101",
                                     TIME_ON="1800", BAND="20m", MODE="SSB",
                                     AGE="250"))
        self.assertEqual([f.rule for f in fs], ["range"])

    def test_boolean_must_be_y_or_n(self):
        fs = self._errors(one_record(CALL="W1AW", QSO_DATE="20261101",
                                     TIME_ON="1800", BAND="20m", MODE="SSB",
                                     SWL="X"))
        self.assertEqual([f.rule for f in fs], ["boolean"])

    def test_duplicate_field(self):
        text = ("<EOH>\n<CALL:4>W1AW<CALL:4>W1AW<BAND:3>20m"
                "<MODE:3>SSB<QSO_DATE:8>20261101<TIME_ON:4>1800<EOR>")
        fs = self._errors(text)
        self.assertEqual([f.rule for f in fs], ["duplicate-field"])

    def test_unknown_field_warns_not_errors(self):
        findings = check_text(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            MODE="SSB", JUNKFIELD="x"))
        self.assertEqual([(f.severity, f.rule) for f in findings],
                         [("warning", "unknown-field")])

    def test_satellite_missing_prop_mode_warns_in_core(self):
        findings = check_text(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="70cm",
            MODE="FM", SAT_NAME="AO-91"))
        self.assertEqual([(f.severity, f.rule) for f in findings],
                         [("warning", "sat-pairing")])

    def test_missing_minimum_warns_in_core(self):
        findings = check_text(one_record(CALL="W1AW", TIME_ON="1800"))
        self.assertTrue(any(f.rule == "minimum-fields" and
                            f.severity == "warning" for f in findings))

    def test_grid_two_chars_valid(self):
        self.assertEqual(self._errors(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            MODE="SSB", GRIDSQUARE="IO")), [])

    def test_location_lowercase_hemisphere_ok(self):
        self.assertEqual(self._errors(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            MODE="SSB", LAT="s018 28.430")), [])

    def test_location_wrong_hemisphere_letter(self):
        fs = self._errors(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            MODE="SSB", LAT="W021 22.892"))
        self.assertEqual([f.rule for f in fs], ["location"])


class TestPacks(unittest.TestCase):
    def _findings(self, text, packs):
        return check_text(text, packs)

    def test_lotw_minimum_is_error(self):
        fs = self._findings(one_record(CALL="W1AW", TIME_ON="1800"), {"lotw"})
        self.assertTrue(any(f.rule == "minimum-fields" and
                            f.severity == "error" for f in fs))

    def test_lotw_satellite_pairing_is_error(self):
        fs = self._findings(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="70cm",
            MODE="FM", SAT_NAME="AO-91"), {"lotw"})
        self.assertTrue(any(f.rule == "sat-pairing" and f.pack == "lotw"
                            and f.severity == "error" for f in fs))

    def test_lotw_duplicate_qso(self):
        rec = one_record(CALL="W1AW", QSO_DATE="20261101", TIME_ON="180000",
                         BAND="20m", MODE="SSB")
        text = "<EOH>\n" + rec.replace("<EOH>\n", "") + "\n" + \
            rec.replace("<EOH>\n", "") + "\n"
        fs = self._findings(text, {"lotw"})
        self.assertTrue(any(f.rule == "lotw-duplicate" for f in fs))

    def test_eqsl_call_too_long(self):
        fs = self._findings(one_record(
            CALL="W1AW/PORTABLE123", QSO_DATE="20261101", TIME_ON="1800",
            BAND="20m", MODE="SSB"), {"eqsl"})
        self.assertTrue(any(f.rule == "eqsl-length" and f.field == "CALL"
                            for f in fs))

    def test_eqsl_630m_is_warning_not_error(self):
        fs = self._findings(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="630m",
            MODE="CW"), {"eqsl"})
        self.assertTrue(any(f.rule == "eqsl-band" and
                            f.severity == "warning" for f in fs))

    def test_eqsl_requires_band_or_freq_or_sat_mode(self):
        fs = self._findings(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800",
            MODE="SSB"), {"eqsl"})
        self.assertTrue(any(f.rule == "eqsl-minimum" for f in fs))


class TestParsing(unittest.TestCase):
    def test_length_mismatch_reported(self):
        text = ("<EOH>\n<CALL:3>W1AW<BAND:3>20m<MODE:3>SSB"
                "<QSO_DATE:8>20261101<TIME_ON:4>1800<EOR>")
        fs = check_text(text)
        self.assertTrue(any(f.rule == "syntax" for f in fs))

    def test_missing_eor_reported(self):
        text = ("<EOH>\n<CALL:4>W1AW<BAND:3>20m<MODE:3>SSB"
                "<QSO_DATE:8>20261101<TIME_ON:4>1800")
        fs = check_text(text)
        self.assertTrue(any("no <EOR>" in f.message for f in fs))

    def test_multiline_value_parsed_by_length(self):
        text = ("<EOH>\n<CALL:4>W1AW<BAND:3>20m<MODE:3>SSB"
                "<QSO_DATE:8>20261101<TIME_ON:4>1800"
                "<NOTES:17>line one\nline two<EOR>")
        parsed = core.parse_adi(text)
        self.assertEqual(parsed.records[0].get("NOTES"), "line one\nline two")

    def test_headerless_file_parses(self):
        text = ("<CALL:4>W1AW<BAND:3>20m<MODE:3>SSB"
                "<QSO_DATE:8>20261101<TIME_ON:4>1800<EOR>")
        parsed = core.parse_adi(text)
        self.assertEqual(len(parsed.records), 1)

    def test_lowercase_tags_and_field_names(self):
        text = ("<eoh>\n<call:4>W1AW<band:3>20m<mode:3>SSB"
                "<qso_date:8>20261101<time_on:4>1800<eor>")
        parsed = core.parse_adi(text)
        self.assertEqual(parsed.records[0].get("CALL"), "W1AW")

    def test_adx_basic(self):
        parsed = core.parse_file(str(ROOT / "examples" / "sample.adx"))
        self.assertEqual(len(parsed.records), 2)
        self.assertEqual(parsed.records[0].get("MODE"), "SSB")

    def test_adx_malformed_xml_is_one_line_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".adx",
                                         delete=False) as fh:
            fh.write("<ADX><RECORDS><RECORD><CALL>W1AW</CALL></RECORD>")
            name = fh.name
        with self.assertRaises(core.AdifError) as ctx:
            core.parse_file(name)
        self.assertNotIn("\n", str(ctx.exception))

    def test_empty_file_is_one_line_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".adi",
                                         delete=False) as fh:
            name = fh.name
        with self.assertRaises(core.AdifError):
            core.parse_file(name)

    def test_missing_file_is_one_line_error(self):
        with self.assertRaises(core.AdifError) as ctx:
            core.parse_file("/nonexistent/nope.adi")
        self.assertIn("not found", str(ctx.exception))


class TestReviewRegressions(unittest.TestCase):
    """Every probe the independent review caught, pinned as a test."""

    def test_absurd_length_is_a_finding_not_a_traceback(self):
        text = "<EOH>\n<CALL:" + "9" * 5000 + ">W1AW<EOR>"
        findings = check_text(text)
        self.assertTrue(any(f.rule == "syntax" for f in findings))

    def test_import_only_value_warns(self):
        self.assertIn("enum-import-only", {f.rule for f in check_text(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            MODE="SSB", QSL_RCVD="V"))})

    def test_deleted_dxcc_warns(self):
        self.assertIn("enum-deleted", {f.rule for f in check_text(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            MODE="SSB", DXCC="2"))})

    def test_bundled_marker_sets_are_not_empty(self):
        self.assertGreaterEqual(
            len(core.SPEC["enums"]["Mode"]["import_only"]), 40)
        self.assertGreaterEqual(
            len(core.SPEC["enums"]["DXCC_Entity_Code"]["deleted"]), 60)

    def test_headerless_eoh_inside_value_keeps_records(self):
        text = ("<CALL:4>W1AW<BAND:3>20m<MODE:3>SSB"
                "<QSO_DATE:8>20261101<TIME_ON:4>1800"
                "<NOTES:12>see <EOH> ok<EOR>")
        parsed = core.parse_adi(text)
        self.assertEqual(len(parsed.records), 1)
        self.assertEqual(check_text(text), [])

    def test_truncated_field_reports_its_own_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".adi",
                                         delete=False) as fh:
            fh.write("<EOH>\n<CALL:99>W1AW")
            name = fh.name
        parsed = core.parse_file(name)
        self.assertTrue(any("file ends inside field" in f.message
                            for f in parsed.findings))

    def test_duplicate_field_value_checked_once(self):
        text = ("<CALL:4>W1AW<BAND:4>25m <BAND:4>25m "
                "<QSO_DATE:8>20261101<TIME_ON:4>1800<MODE:3>SSB<EOR>")
        findings = check_text(text)
        band_errors = [f for f in findings
                       if f.rule == "enum" and f.field == "BAND"]
        self.assertEqual(len(band_errors), 1)
        self.assertIn("duplicate-field", {f.rule for f in findings})

    def test_latitude_over_90_is_an_error(self):
        self.assertIn("location", {f.rule for f in check_text(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            MODE="SSB", LAT="N120 00.000"))})

    def test_ten_character_gridsquare_warns(self):
        self.assertIn("gridsquare", {f.rule for f in check_text(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            MODE="SSB", GRIDSQUARE="IO91AB12CD"))})

    def test_adx_with_prolog_in_adi_named_file(self):
        text = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<ADX><HEADER></HEADER><RECORDS><RECORD>'
                '<CALL>W1AW</CALL><QSO_DATE>20261101</QSO_DATE>'
                '<TIME_ON>1800</TIME_ON><BAND>20m</BAND><MODE>SSB</MODE>'
                '</RECORD></RECORDS></ADX>')
        with tempfile.NamedTemporaryFile("w", suffix=".adi",
                                         delete=False) as fh:
            fh.write(text)
            name = fh.name
        parsed = core.parse_file(name)
        self.assertEqual(len(parsed.records), 1)

    def test_byte_counted_utf8_value_parses(self):
        raw = ("<CALL:4>W1AW<QSO_DATE:8>20261101<TIME_ON:4>1800"
               "<BAND:3>20m<MODE:3>SSB<COMMENT:5>café<EOR>")
        with tempfile.NamedTemporaryFile("wb", suffix=".adi",
                                         delete=False) as fh:
            fh.write(raw.encode("utf-8"))
            name = fh.name
        parsed = core.parse_file(name)
        self.assertEqual(parsed.records[0].get("COMMENT"), "café")
        self.assertEqual(core.check_parsed(parsed, frozenset()), [])


class TestSubdivisions(unittest.TestCase):
    def test_state_wrong_for_entity_is_an_error(self):
        self.assertIn("subdivision", {f.rule for f in check_text(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            MODE="SSB", DXCC="291", STATE="XX"))})

    def test_state_right_for_entity_is_clean(self):
        self.assertEqual(check_text(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            MODE="SSB", DXCC="291", STATE="CA")), [])

    def test_state_without_dxcc_is_not_checked(self):
        self.assertEqual(check_text(one_record(
            CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800", BAND="20m",
            MODE="SSB", STATE="XX")), [])


class TestCorpus(unittest.TestCase):
    def test_planted_corpus_fully_caught_no_false_positives(self):
        truth = json.loads((FIXTURES / "corpus_truth.json").read_text())
        parsed = core.parse_file(str(FIXTURES / "corpus.adi"))
        findings = core.check_parsed(parsed, frozenset({"lotw", "eqsl"}))
        by_rec = {}
        for f in findings:
            by_rec.setdefault(f.record, set()).add(f.rule)
        for i, want in enumerate(truth, start=1):
            if want is None:
                self.assertEqual(by_rec.get(i, set()), set(),
                                 f"clean record {i} produced findings")
            else:
                self.assertIn(want, by_rec.get(i, set()),
                              f"record {i}: planted {want} not caught")


class TestCli(unittest.TestCase):
    def test_json_output_parses(self):
        out = subprocess.run(
            [sys.executable, "-m", "adifcheck", "--json", "--lotw", "--eqsl",
             str(ROOT / "examples" / "messy.adi")],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        data = json.loads(out.stdout)
        self.assertEqual(data[0]["errors"], 8)

    def test_strict_turns_warnings_into_failure(self):
        text = one_record(CALL="W1AW", QSO_DATE="20261101", TIME_ON="1800",
                          BAND="20m", MODE="SSB", JUNKFIELD="x")
        with tempfile.NamedTemporaryFile("w", suffix=".adi",
                                         delete=False) as fh:
            fh.write(text)
            name = fh.name
        self.assertEqual(run_cli(name), 0)
        self.assertEqual(run_cli(name, "--strict"), 1)
