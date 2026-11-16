# Published runs

[`2026-11-10_023411`](2026-11-10_023411) is the first full run: the PHQ-9 pack, 20 cases × 3
conversations on claude-haiku-5-5, for $0.36. The [README](../README.md) sums up what it found.

Start with
[`regrade-2026-11-15_164638/report.md`](2026-11-10_023411/regrade-2026-11-15_164638/report.md). It
grades the same conversations again with the checks as fixed on 2026-11-15.
[`regrade-2026-11-10_220840`](2026-11-10_023411/regrade-2026-11-10_220840) is the earlier regrade,
made before the keyword rule that read safe referrals as medication advice was fixed.

The simulated user notes, with each reply, which question it answers. In one conversation, it
corrected an earlier answer and answered the question it was asked in the same reply, but noted
neither. A check then blamed the agent for skipping that question. The fix sets a conversation aside
when a reply uses the questionnaire's answer words and notes no question, right after a question
that no turn of the conversation notes. This reply used them in its correction ("not at all", "more
than half the days"). A reply without those words still counts against the agent.

The run's own [`report.md`](2026-11-10_023411/report.md) grades it with the checks of the day it
ran. `history/` keeps the first copy of that grading, written at the end of the run.

Each `<case>/run_<n>.json` is one conversation: every turn and tool call, what the simulated user
meant to answer, and each model call with its cost.

A replay writes into the run's folder, so copy it first:

```bash
mkdir -p runs && cp -r examples/2026-11-10_023411 runs/
uv run eval --replay runs/2026-11-10_023411                    # the run's own grading
uv run eval --replay runs/2026-11-10_023411 --regrade-current  # with today's checks
```
