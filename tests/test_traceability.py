"""Traceability: every pack in `domains/`, in every variant, lints with no error, every case and
every conformance or hazard check links at least one hazard, and validity checks link none. This
is how the lint runs with no one at the terminal: under `uv run pytest`."""

import shutil
from pathlib import Path

import pytest

from auditkit.lint import lint_pack
from auditkit.pack import load_pack

DOMAINS = Path(__file__).resolve().parents[1] / "domains"
FIXTURE_PACKS = Path(__file__).parent / "fixtures" / "packs"
PACKS = sorted(d.name for d in DOMAINS.iterdir() if (d / "pack.yaml").is_file())
VARIANTS = [
    (name, variant)
    for name in PACKS
    for variant in ["default", *load_pack(DOMAINS, name).config.variants]
]


def assert_traces(packs_dir: Path, name: str, variant: str) -> None:
    """Cases tag the variant's hazards; checks link the full registry, since a variant's
    `exclude_hazards` keep their checks (printed as not applicable, docs/DOMAIN_PACKS.md §8)."""
    pack = load_pack(packs_dir, name, variant=variant)
    registry = load_pack(packs_dir, name).hazards  # the default variant excludes none
    for case in pack.cases.values():
        assert case.hazards and set(case.hazards) <= set(pack.hazards), case.id
    for bound in pack.checks:
        spec = bound.spec
        if spec.kind == "validity":
            assert (spec.hazards, spec.controls) == ([], []), spec.id
        else:
            assert spec.hazards and set(spec.hazards) <= set(registry), spec.id
            controls = {c.id for hid in spec.hazards for c in registry[hid].controls}
            assert set(spec.controls) <= controls, spec.id


def test_domains_holds_the_phq9_pack():
    assert "phq9" in PACKS


@pytest.mark.parametrize(("name", "variant"), VARIANTS)
def test_every_pack_and_variant_lints_with_no_error(name, variant):
    errors = [i for i in lint_pack(DOMAINS, name, variant=variant) if i.level == "error"]
    assert errors == []


@pytest.mark.parametrize(("name", "variant"), VARIANTS)
def test_every_case_and_check_traces_to_a_hazard_and_validity_checks_to_none(name, variant):
    assert_traces(DOMAINS, name, variant)


def test_a_check_may_link_a_hazard_its_variant_excludes(tmp_path):
    # The toy pack's variant `alt` excludes H-03, which a check of the pack links.
    packs = tmp_path / "packs"
    shutil.copytree(FIXTURE_PACKS, packs, ignore=shutil.ignore_patterns("__pycache__"))
    default = load_pack(packs, "toypack")
    excluded = set(default.config.variants["alt"].exclude_hazards)
    linked = {hid for b in default.checks for hid in b.spec.hazards}
    assert excluded and excluded <= linked
    assert_traces(packs, "toypack", "alt")
