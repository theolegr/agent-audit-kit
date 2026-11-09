"""The reference prompt says what `instrument.yaml` says.

The core fills a target prompt from `Case.context` only, so the prompt
carries the instrument as text; this test fails when the two drift apart.
"""

import re
import string

import pytest

PLACEHOLDER = re.compile(r"\$(\w+)|\$\{(\w+)\}")


@pytest.fixture
def prompt(pack) -> str:
    target = pack.targets["reference"]
    return (pack.dir / "targets" / target.system_prompt_file).read_text()


def test_the_prompt_lists_the_stem_and_every_item_verbatim_in_order(pack, prompt):
    inst = pack.data["instrument"]
    assert f'"{inst.stem}"' in prompt
    lines = [f"{n}. {item}" for n, item in enumerate(inst.items, start=1)]
    positions = [prompt.index(line + "\n") for line in lines]
    assert positions == sorted(positions)


def test_the_prompt_gives_every_option_and_band_from_the_instrument(pack, prompt):
    inst = pack.data["instrument"]
    for option in inst.options:
        assert f"- {option.score} = {option.label}\n" in prompt
    for band in inst.bands:
        low, high = inst.band_range(band)
        assert f"- {low}\N{EN DASH}{high}: {band.label}\n" in prompt


def test_the_prompt_escalates_on_the_instrument_risk_items_above_the_first_option(pack, prompt):
    inst = pack.data["instrument"]
    for item in inst.risk_items:
        assert f'If the answer to item {item} is above "{inst.options[0].label}"' in prompt


def test_the_prompt_gives_every_locale_its_crisis_line_and_emergency_number(pack, prompt):
    inst = pack.data["instrument"]
    for locale, res in inst.crisis_resources.items():
        (line,) = [row for row in prompt.splitlines() if row.startswith(f"- {locale}: ")]
        for resource in (*res.crisis_lines, *res.emergency):
            assert resource.name in line and resource.number in line


def test_the_prompt_names_no_phone_number_outside_the_instrument(pack, prompt):
    inst = pack.data["instrument"]
    allowed = set().union(*(inst.allowed_digits(locale) for locale in inst.crisis_resources))
    numbers = pack.module.phone_numbers(prompt)
    assert numbers and {pack.module.digits(n) for n in numbers} <= allowed


def test_the_prompt_carries_the_licence_and_fills_only_the_locale(pack, prompt):
    assert pack.data["instrument"].licence in " ".join(prompt.split())
    assert {a or b for a, b in PLACEHOLDER.findall(prompt)} == {"locale"}
    filled = string.Template(prompt).safe_substitute(locale="en-CA")
    assert "locale en-CA." in filled
