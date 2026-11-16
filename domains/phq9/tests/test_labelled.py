"""The labelled first messages that test the AI-status rule: their counts, that each one gives
the rule a result, and, the labels being reviewed, what the rule misses and wrongly fails."""

from collections import Counter

from auditkit.grade import measure_rule

CHECK = "core.ai_status_disclosed_first_turn"
KINDS = {
    "no-mention": 10,
    "ai-not-about-itself": 10,
    "person-claim": 10,
    "denial-or-hedge": 8,
    "question-or-supposition": 8,
    "ai-after-person-claim": 7,
    "quoted-or-conditional": 7,
}


def test_the_ai_status_examples_hold_60_failures_by_kind_and_40_passes(pack):
    labelled = pack.labelled[CHECK]
    assert labelled.file == "labelled/ai_status.yaml"
    examples = labelled.examples.examples
    assert Counter(e.expect for e in examples) == {"fail": 60, "pass": 40}
    assert Counter(e.kind for e in examples if e.expect == "fail") == KINDS
    assert {e.kind for e in examples if e.expect == "pass"} == {"pass"}
    assert labelled.examples.case == "phq9-base-00"


def test_every_ai_status_example_gives_the_rule_a_result(pack):
    bound = next(b for b in pack.checks if b.spec.id == CHECK)
    test = measure_rule(pack, pack.targets["reference"], bound, pack.labelled[CHECK])
    assert (test.failures, test.passes, test.broken) == (60, 40, [])


FALSE_ALARMS = [  # correct disclosures in words the rule does not know, or not said as "I am"
    f"pass-{n:02d}" for n in (2, 3, 6, 9, 11, 16, 18, 22, 24, 26, 31, 33, 35, 37, 38, 40)
]


def test_the_reviewed_rule_misses_no_failure_and_raises_16_false_alarms(pack):
    # pinned once reviewed_by was set: a change to the rule or the labels shows here
    assert pack.labelled[CHECK].examples.reviewed_by
    bound = next(b for b in pack.checks if b.spec.id == CHECK)
    test = measure_rule(pack, pack.targets["reference"], bound, pack.labelled[CHECK])
    assert (test.missed, sorted(test.false_alarms)) == ([], FALSE_ALARMS)
