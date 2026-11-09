"""`auditkit.confusables`, the table tools/gen_confusables.py writes from Unicode's confusables.txt
(UTS #39): the generator on a tiny file in its format, and the committed module.

Set CONFUSABLES_TXT to a downloaded confusables.txt 18.0.0 to check that the generator rebuilds the
committed module from it byte for byte; without it, that test is skipped.
"""

import hashlib
import importlib.util
import os
import subprocess
import sys
import unicodedata
from pathlib import Path
from types import ModuleType

import pytest

from auditkit import confusables

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tools" / "gen_confusables.py"
MODULE = ROOT / "src" / "auditkit" / "confusables.py"
SHA256 = "6ed3ee967c9dfdf6677d563c9985182fbc50a2efb7d6059cd57b2e2ce18f5b92"
FIXTURE = (
    "\ufeff# confusables.txt\n"
    "# Date: 2099-01-02, 03:04:05 GMT\n"
    "# Version: 99.0.0\n"
    "#\n"
    "0430 ;\t0061 ;\tMA\t# ( \u0430 \u2192 a ) CYRILLIC SMALL LETTER A \u2192 LATIN SMALL A\n"
    "\n"
    "0391 ;\t0041 ;\tMA\t# ( \u0391 \u2192 A ) GREEK CAPITAL LETTER ALPHA \u2192 LATIN CAPITAL A\n"
    "04D1 ;\t0061 0306 ;\tMA\t# the combining breve is dropped\n"
    "0271 ;\t0072 006E 0326 ;\tMA\t# M WITH HOOK: rn, the comma below dropped, read m\n"
    "006D ;\t0072 006E ;\tMA\t# ( m \u2192 rn ): an ASCII source, so rn reads m\n"
    "01B7 ;\t0033 ;\tMA\t# ( \u01b7 \u2192 3 ) LATIN CAPITAL LETTER EZH \u2192 DIGIT THREE\n"
    "0049 ;\t006C ;\tMA\t# an ASCII source: skipped\n"
    "0444 ;\t0278 ;\tMA\t# a prototype that reads as no ASCII: skipped\n"
    "0442 ;\t1D1B ;\tMA\t# CYRILLIC TE: its prototype, the small capital T, read by hand\n"
    "03B5 ;\tA793 ;\tMA\t# GREEK EPSILON: its prototype, the c with bar, read by hand\n"
    "15EF ;\t0460 ;\tMA\t# CARRIER GU: its prototype, the capital omega, read by hand\n"
    "0413 ;\t0393 ;\tMA\t# CYRILLIC CAPITAL GHE: not read through the small gamma\n"
    "03B3 ;\t0079 ;\tMA\t# ( \u03b3 \u2192 y ) GREEK SMALL LETTER GAMMA\n"
    "03C3 ;\t006F ;\tMA\t# ( \u03c3 \u2192 o ) GREEK SMALL LETTER SIGMA\n"
    "03F2 ;\t0063 ;\tMA\t# LUNATE SIGMA: NFKD makes it a sigma, so it is read before NFKD\n"
    "017F ;\t0066 ;\tMA\t# LONG S: NFKD reads it as s, kept\n"
    # code points this Python does not know (noncharacters) under the names of Unicode 16 and later
    # ones: told apart by the name the comment gives
    "FDD0 ;\t0041 ;\tMA\t# * ( \ufdd0 \u2192 A ) OUTLINED LATIN CAPITAL LETTER A \u2192 "
    "LATIN CAPITAL LETTER A\t# a letter: kept\n"
    "FDD1 ;\t00DF ;\tMA\t# ( \ufdd1 \u2192 \u00df ) LATIN SMALL LIGATURE LONG S WITH "
    "DESCENDER S \u2192 LATIN SMALL LETTER SHARP S\t# a ligature, a letter too: kept\n"
    "FDD2 ;\t006C ;\tMA\t# * ( \ufdd2 \u2192 l ) TULU-TIGALARI DANDA \u2192 LATIN SMALL "
    "LETTER L\t# no letter or digit: skipped\n"
    "FDD3 ;\t0037 ;\tMA\t# * ( \ufdd3 \u2192 7 ) MUSICAL SYMBOL DIGIT SEVEN WITH LOW "
    "STROKE \u2192 DIGIT SEVEN\t# a symbol, though named a digit: skipped\n"
    "16D63 ;\t006C ;\tMA\t# ( \U00016d63 \u2192 l ) KIRAT RAI VOWEL SIGN AA \u2192 LATIN SMALL "
    "LETTER L\t# a letter, though named a sign: kept, as the generator's hand list says\n"
    "2768 ;\t0028 ;\tMA\t# a prototype that is no letter or digit: skipped\n"
    "20A5 ;\t0072 006E 0338 ;\tMA\t# MILL SIGN, a source that is no letter or digit: skipped\n"
    "00D7 ;\t0078 ;\tMA\t# MULTIPLICATION SIGN: skipped too\n"
)


def generate(source: Path, out: Path) -> subprocess.CompletedProcess:
    """The generator as its docstring says to run it (`-I`: nothing imported from the cwd)."""
    command = [sys.executable, "-I", str(GENERATOR), str(source), str(out)]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def loaded(module: Path) -> ModuleType:
    """A generated module, loaded from its file."""
    spec = importlib.util.spec_from_file_location("generated_confusables", module)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_generator_keeps_non_ascii_letters_or_digits_whose_prototype_reads_as_ascii(tmp_path):
    source, out = tmp_path / "confusables.txt", tmp_path / "confusables.py"
    source.write_text(FIXTURE, encoding="utf-8")
    done = generate(source, out)
    assert done.returncode == 0, done.stderr
    module = loaded(out)
    assert module.CONFUSABLES == {
        "\u0430": "a",
        "\u0391": "a",  # casefolded
        "\u04d1": "a",
        "\u0271": "m",  # "rn", how the file reads an "m"
        "\u01b7": "3",
        "\u0442": "t",  # read through the hand list
        "\u03b5": "e",  # the same
        "\u15ef": "w",  # the same
        # not the capital ghe, listed as the capital gamma: `_fold` reads it "r" through its own
        # small letter, where the small gamma would read "y"
        "\u03b3": "y",
        "\u03c3": "o",
        "\u03f2": "c",
        "\u017f": "f",  # NFKD reads it first, as "s"
        "\ufdd0": "a",
        "\ufdd1": "ss",
        "\U00016d63": "l",
    }
    # read before NFKD: only the letter NFKD turns into another one, not ASCII, read otherwise
    assert module.BEFORE_NFKD == {"\u03f2": "c"}
    assert module.UNLISTED_LOOK_ALIKES["\u1d1b"] == "t"  # the hand list, written in the module
    header = out.read_text(encoding="utf-8")
    assert "https://www.unicode.org/Public/security/99.0.0/confusables.txt" in header
    assert "2099-01-02, 03:04:05 GMT" in header
    assert hashlib.sha256(source.read_bytes()).hexdigest() in header


def test_generator_refuses_a_file_with_no_version_line(tmp_path):
    source, out = tmp_path / "confusables.txt", tmp_path / "confusables.py"
    source.write_text(FIXTURE.replace("# Version: 99.0.0\n", ""), encoding="utf-8")
    done = generate(source, out)
    assert done.returncode != 0 and "Version" in done.stderr
    assert not out.exists()


def test_committed_module_names_its_source_and_carries_unicodes_notice():
    text = " ".join(MODULE.read_text(encoding="utf-8").split())  # one line: wrapping aside
    assert "https://www.unicode.org/Public/security/18.0.0/confusables.txt" in text
    assert "Version 18.0.0" in text and SHA256 in text
    assert "Generated by tools/gen_confusables.py: do not edit" in text
    assert "UNICODE LICENSE V3 COPYRIGHT AND PERMISSION NOTICE Copyright" in text
    assert "this copyright and permission notice appear with all copies of the Data Files" in text
    assert "authorization of the copyright holder." in text  # the notice's last words


def test_committed_table_reads_non_ascii_characters_as_ascii_letters_or_digits():
    table = confusables.CONFUSABLES
    assert len(table) > 1500
    assert all(len(c) == 1 and not c.isascii() for c in table)
    # letters and digits only: a symbol drawn like one ("\u00d7" like an "x") stays a separator,
    # so a canary spelled out with it still shows; a code point this Python does not know is told
    # by its name, its category not known here: of the 95 that read as ASCII, 88 are named letters
    # or digits and 2 letters named signs, kept by hand
    assert all(c.isalnum() or unicodedata.category(c) == "Cn" for c in table)
    assert sum(unicodedata.category(c) == "Cn" for c in table) == 90
    readings = set(table.values())
    assert all(r and r.isascii() and r.isalnum() and r == r.lower() for r in readings)


def test_hand_table_holds_only_what_confusables_omits():
    # a small capital Unicode lists one day goes from the hand table to the generated one
    assert set(confusables.UNLISTED_LOOK_ALIKES) & set(confusables.CONFUSABLES) == set()


def test_committed_module_reads_before_nfkd_only_letters_nfkd_turns_into_others():
    # NFKD makes the lunate sigmas sigmas, read "o"; two Cyrillic modifier letters a soft sign and
    # a yeru, which the table cannot read; the stretched heh a heh, read "o" where it is listed as
    # "ol". A letter NFKD reads as ASCII is not here: the long s U+017F (listed as "f") is an "s".
    assert confusables.BEFORE_NFKD == {
        "\u03f2": "c",
        "\u03f9": "c",
        "\ua69d": "b",
        "\U0001e047": "bi",
        "\U0001ee64": "ol",
    }


@pytest.mark.skipif(
    not Path(os.environ.get("CONFUSABLES_TXT", "")).is_file(),
    reason="CONFUSABLES_TXT names no downloaded confusables.txt",
)
def test_generator_rebuilds_the_committed_module_from_unicodes_file(tmp_path):
    out = tmp_path / "confusables.py"
    done = generate(Path(os.environ["CONFUSABLES_TXT"]), out)
    assert done.returncode == 0, done.stderr
    assert out.read_bytes() == MODULE.read_bytes()
