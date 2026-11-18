"""Command-line interface for adifcheck.

Usage:
  adifcheck FILE [FILE ...] [--lotw] [--eqsl] [--strict] [--json]

Exit codes: 0 = no errors (warnings allowed unless --strict),
1 = errors found, 2 = a file could not be read at all.
User-facing failures are one-line messages, never tracebacks.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import core
from .core import AdifError


def build_parser():
    parser = argparse.ArgumentParser(
        prog="adifcheck",
        description="Check an ADIF (.adi/.adx) ham-radio log's contents "
                    "against the ADIF 3.1.8 specification — the values "
                    "that get logs rejected — before you upload it.")
    parser.add_argument("files", nargs="+", help="ADIF files to check")
    parser.add_argument("--lotw", action="store_true",
                        help="also apply the LoTW rule pack (documented "
                             "behaviour: minimum fields, satellite "
                             "pairing, duplicates become findings)")
    parser.add_argument("--eqsl", action="store_true",
                        help="also apply the eQSL rule pack (eQSL's "
                             "published content spec: its own mode/band "
                             "tables and field-length limits)")
    parser.add_argument("--strict", action="store_true",
                        help="treat warnings as failures too")
    parser.add_argument("--json", action="store_true",
                        help="print findings as JSON")
    return parser


def check_one(path, packs):
    parsed = core.parse_file(path)
    findings = core.check_parsed(parsed, packs)
    labels = {rec.number: core.record_label(rec) for rec in parsed.records}
    return parsed, findings, labels


def main(argv=None):
    try:
        return _main(argv)
    except BrokenPipeError:
        # output piped into a command that closed early (e.g. `| head`):
        # exit quietly instead of printing a traceback
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0


def _main(argv=None):
    args = build_parser().parse_args(argv)
    packs = set()
    if args.lotw:
        packs.add("lotw")
    if args.eqsl:
        packs.add("eqsl")

    worst = 0
    json_out = []
    for path in args.files:
        try:
            parsed, findings, labels = check_one(path, packs)
        except AdifError as exc:
            if args.json:
                json_out.append({"file": path, "error": str(exc)})
            else:
                print(f"error: {exc}", file=sys.stderr)
            worst = 2
            continue
        errors = [f for f in findings if f.severity == "error"]
        warnings = [f for f in findings if f.severity == "warning"]
        if errors or (args.strict and warnings):
            worst = max(worst, 1)
        if args.json:
            json_out.append({
                "file": path,
                "records": len(parsed.records),
                "errors": len(errors),
                "warnings": len(warnings),
                "findings": [
                    {"record": f.record, "severity": f.severity,
                     "field": f.field, "rule": f.rule,
                     "message": f.message, "ref": f.ref, "pack": f.pack}
                    for f in findings
                ],
            })
            continue
        print(f"{path}: {len(parsed.records)} records, "
              f"{len(errors)} errors, {len(warnings)} warnings")
        for f in findings:
            print(f.line(labels.get(f.record, "")))
        if errors:
            print(f"result: FAIL — {len(errors)} error(s) that will "
                  f"cause rejections or dropped records")
        elif warnings:
            print("result: no errors; warnings above are worth a look")
        else:
            print("result: clean — no findings")
    if args.json:
        print(json.dumps(json_out, indent=1))
    return worst


if __name__ == "__main__":
    sys.exit(main())
