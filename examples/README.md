# Published runs

One run is published: [`2026-11-16_003815`](2026-11-16_003815), the PHQ-9 pack on
claude-haiku-5-5. Its results, and why it was graded twice, are in the
[README](../README.md).

The folder holds one set of conversations and two gradings of them:

| What | Where |
|---|---|
| The conversations | `phq9-*/run_<n>.json`, one file each |
| The second grading, after two fixes to the kit: read this one | [`regrade-2026-11-16_154557/report.md`](2026-11-16_003815/regrade-2026-11-16_154557/report.md) |
| The first grading, where self-harm is not met | [`report.md`](2026-11-16_003815/report.md) |

`report.md` is a replay made just after the run, with the same code. `history/` keeps the copy
written when the run ended, with the same verdicts. The other files:
[the run folder](../docs/USAGE.md#the-run-folder) in `USAGE.md`.

The folders `phq9-sh-*`, `phq9-inj-*` and `phq9-vague-*` are the variations, written by
`tools/gen_cases.py` from the table in `domains/phq9/variations.yaml`. The other 20 cases were
written by hand.

To replay the run, copy the folder first, since a replay writes into it: the commands are in the
README's [For developers](../README.md#for-developers). A plain `--replay` keeps the pack saved
with the run, whose self-harm rule still asks for the second alert, so self-harm is not met there
too. `--regrade-current` uses today's pack and gives the second grading.
