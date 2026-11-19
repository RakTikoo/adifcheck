# Contributing

Thanks for looking at adifcheck. Issues and pull requests are welcome.

## Before you start

- For a starter issue, claim it with a comment first, so two people do not build the same thing.
- Keep each change small and focused on one issue.

## Running the tests

```console
python3 run_tests.py
```

The full suite (61 tests, stdlib `unittest`, no dependencies) must pass before a pull request is reviewed. Two bars are absolute: the official ADIF test files must still check with zero errors, and a new check must catch its planted cases without flagging valid records. Every rule needs a source — an ADIF 3.1.8 section for the core, or the named LoTW/eQSL document for a pack rule. If your change alters the CLI or its output, update the README in the same pull request — and SKILL.md too, if install, commands, flags, exit codes or output wording changed.

## Pull requests

- Say what you changed, how you tested it, and which source each new rule comes from.
