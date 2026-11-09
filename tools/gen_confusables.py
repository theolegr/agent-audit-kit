"""Write src/auditkit/confusables.py from Unicode's confusables.txt (UTS #39).

    uv run python tools/gen_confusables.py <confusables.txt> [<out.py>]

The file comes from https://www.unicode.org/Public/security/<version>/confusables.txt; it is not
kept in the repo (763 KB), only the table read from it. Each source character that is a letter or
digit but not ASCII, and whose prototype reads as ASCII letters or digits as `_fold` in
src/auditkit/generic_checks.py reads it (`reading`: NFKD, its combining marks (Mn, Me) dropped,
the look-alikes read, the hand list below included, casefolded), is kept with that reading: what
`_fold` reads the source as. A prototype that is how the file spells an ASCII letter reads as
that letter ("rn", the file's "m"). A symbol, a punctuation or a spacing mark drawn like
a letter (the multiplication sign U+00D7 like an "x", the box-drawing line U+2502 like an "l") is
left out: `fold_canary` drops it as a separator, so a canary spelled out with it still shows. A
code point this Python's unicodedata does not know (Unicode 16 and later), its category not known
here, is told by the name its line gives it: kept when the name says a letter, a digit or a
ligature, and not a symbol (the musical symbol digit seven U+1D1FE is one), since Python leaves it
as it is and only the table can read it; else left out as a symbol is (the Tulu-Tigalari danda
U+113D4, drawn like an "l"), unless the hand list of letters named signs below holds it (the Kirat
Rai vowel sign aa U+16D63, drawn like an "l", is a letter). The cost: a letter Unicode names
otherwise is left out until it is added there, so when the file's version changes, check those
left out by their names against a Unicode that knows them. The few sources NFKD turns into
another letter, not ASCII, that the table reads otherwise
(the lunate sigma, drawn "c", into a sigma, read "o") go in a second table, which `_fold` reads
before NFKD. The module's
header names the file's version, date and sha256 (recorded, not checked against a known value),
and carries Unicode's copyright and permission notice, which asks to appear with every copy of
data derived from its files: update NOTICE below when https://www.unicode.org/license.txt changes.
NFKD and casefold come from this Python's unicodedata, whose version the header names too: run it
with the project's Python.
"""

from __future__ import annotations

import hashlib
import re
import sys
import unicodedata
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "src" / "auditkit" / "confusables.py"
# What Unicode's confusables leave out but a model writes for a Latin letter, kept by hand and
# written into the module: the small capitals of a "small caps" text it does not read as Latin
# (it reads those of c, i, o, s, u, v, w, y and z, and those of k and m through the prototypes
# below; none is drawn for "x"), then the prototypes that read as no Latin letter, though what
# the file lists as them is drawn like one: the c with bar U+A793 (the Greek epsilon, the Latin
# open e, the Cyrillic Ukrainian ie: an "e"), the kra U+0138 (the Greek kappa, the Cyrillic ka,
# the small capital K), the turned w U+028D (the Cyrillic em, the small capital M), the Greek chi
# U+03C7 and omega U+03C9, drawn like an "x" and a "w" themselves (the Coptic khi, the Latin chi;
# the Coptic oou, the Cyrillic broad omega), and the capitals drawn like their small letter, which
# `reading` does not read through it (it says why): the capital c with bar U+A792 and the open E
# U+0190 (the Cherokee quv: an "e"), the Armenian capital vo U+0548 (the Canadian syllabics ti,
# the Lisu ue: an "n") and the Cyrillic capital omega U+0460 (the Canadian syllabics carrier gu:
# a "w"). Not the Greek capital chi, which the file reads "X", nor the capital omega, drawn like
# no Latin letter (`_fold` reads it "w" all the same, through its small letter after casefold).
# A source whose prototype is one of them reads as it (the Cyrillic te U+0442, listed as the
# small capital T). Escapes, since as glyphs they read as Latin.
UNLISTED = dict(
    zip(
        "\u1d00\u0299\u1d05\u1d07\ua730\u0262\u029c\u1d0a\u029f\u0274\u1d18\ua7af\u0280\u1d1b"
        "\ua793\u0138\u028d\u03c7\u03c9\ua792\u0190\u0548\u0460",
        "abdefghjlnpqrtekmxweenw",
        strict=True,
    )
)
# The sources this Python's unicodedata does not know, whose prototype reads as ASCII, that
# Unicode lists as letters (Lo) though it names them signs, so `letter_or_digit` cannot tell them
# by their names: the Tolong Siki sign hecaka U+11DDA and the Kirat Rai vowel sign aa U+16D63,
# both drawn like an "l". Kept by hand.
LETTERS_NAMED_SIGNS = {"\U00011dda", "\U00016d63"}
NOTICE = """\
UNICODE LICENSE V3

COPYRIGHT AND PERMISSION NOTICE

Copyright © 1991-2026 Unicode, Inc.

NOTICE TO USER: Carefully read the following legal agreement. BY
DOWNLOADING, INSTALLING, COPYING OR OTHERWISE USING DATA FILES, AND/OR
SOFTWARE, YOU UNEQUIVOCALLY ACCEPT, AND AGREE TO BE BOUND BY, ALL OF THE
TERMS AND CONDITIONS OF THIS AGREEMENT. IF YOU DO NOT AGREE, DO NOT
DOWNLOAD, INSTALL, COPY, DISTRIBUTE OR USE THE DATA FILES OR SOFTWARE.

Permission is hereby granted, free of charge, to any person obtaining a
copy of data files and any associated documentation (the "Data Files") or
software and any associated documentation (the "Software") to deal in the
Data Files or Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, and/or sell
copies of the Data Files or Software, and to permit persons to whom the
Data Files or Software are furnished to do so, provided that either (a)
this copyright and permission notice appear with all copies of the Data
Files or Software, or (b) this copyright and permission notice appear in
associated Documentation.

THE DATA FILES AND SOFTWARE ARE PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF
THIRD PARTY RIGHTS.

IN NO EVENT SHALL THE COPYRIGHT HOLDER OR HOLDERS INCLUDED IN THIS NOTICE
BE LIABLE FOR ANY CLAIM, OR ANY SPECIAL INDIRECT OR CONSEQUENTIAL DAMAGES,
OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,
ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THE DATA
FILES OR SOFTWARE.

Except as contained in this notice, the name of a copyright holder shall
not be used in advertising or otherwise to promote the sale, use or other
dealings in these Data Files or Software without prior written
authorization of the copyright holder.
"""
MODULE = '''\
"""Unicode's confusables (UTS #39), each read as ASCII letters or digits: how `_fold` in
`auditkit.generic_checks` reads a look-alike. Generated by tools/gen_confusables.py: do not
edit, regenerate.

Source: https://www.unicode.org/Public/security/{version}/confusables.txt
Version {version}, dated {date}
sha256 {sha256}
Prototypes read with Python's unicodedata {unidata}
Regenerate: uv run python tools/gen_confusables.py <confusables.txt>

Each source that is a letter or digit but not ASCII (a code point this Python's unicodedata does
not know, by the name the file gives it, or by hand for the few letters Unicode names signs), and
whose prototype reads as ASCII letters or digits as `_fold` reads it (NFKD, its combining marks
(Mn, Me) dropped, the look-alikes read, the hand list below included, casefolded), reads as that;
a prototype that is how the file spells an ASCII letter reads as that letter ("rn", the file's
"m"). A symbol, a punctuation or a spacing mark drawn like a letter is left out: it stays a
separator.

Unicode's copyright and permission notice, which asks to appear with every copy of data derived
from its files:

{notice}"""

# Kept by hand in tools/gen_confusables.py, which says why: what Unicode's confusables leave out
# but a model writes for a Latin letter
UNLISTED_LOOK_ALIKES = {{
{unlisted}
}}
# The sources NFKD turns into another letter, not ASCII, that the table reads otherwise: read
# before NFKD
BEFORE_NFKD = {{
{before}
}}
# Each reading, then the characters that read as it, in code point order
READINGS = {{
{body}
}}
CONFUSABLES = {{char: read for read, chars in READINGS.items() for char in chars}}
'''
CHUNK = 72  # characters of escapes per line of a long reading, within the 100 of ruff's limit
# The source's name in a line's comment, `( x → y ) SOURCE NAME → PROTOTYPE NAME`: the first run of
# capitals, digits, spaces and hyphens, as Unicode writes a name, between ") " and " → " (a glyph
# may be a parenthesis)
NAME = re.compile(r"\) ([A-Z0-9][A-Z0-9 -]*?) → ")


def reading(text: str, look_alikes: dict[int, str]) -> str:
    """`text` as `_fold` reads it with `look_alikes` (from `str.maketrans`) for its table: NFKD,
    combining marks dropped, the look-alikes read, casefolded. Not the table again after casefold,
    as `_fold` does to read a capital the table does not hold through its small letter: a source
    has a reading of its own, which its prototype's small letter would replace (the Cyrillic
    capital ghe, listed as the Greek capital gamma, would read "y" through the small gamma), and a
    small letter may be drawn otherwise than its capital (the Coptic capital sampi, listed as the
    Armenian capital keh, would read "f" through the small keh). The few capitals drawn like their
    small letter are in the hand list instead (the Armenian capital vo, read "n")."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if unicodedata.category(c) not in ("Mn", "Me"))
    return text.translate(look_alikes).casefold()


def latin(text: str) -> bool:
    """Whether `text` is ASCII letters or digits."""
    return text.isascii() and text.isalnum()


def letter_or_digit(char: str, name: str) -> bool:
    """Whether `char` is a letter or digit: by its category, or, for a code point this Python's
    unicodedata does not know, by `name`, the name the file gives it. Unicode names a letter
    "… LETTER …" or "… LIGATURE …" and a digit "… DIGIT …", but a symbol drawn as one "… SYMBOL …"
    (MUSICAL SYMBOL DIGIT SEVEN WITH LOW STROKE), and a few letters "… SIGN …", listed by hand."""
    if unicodedata.category(char) != "Cn":
        return char.isalnum()
    if char in LETTERS_NAMED_SIGNS:
        return True
    words = set(name.split())
    return bool(words & {"LETTER", "DIGIT", "LIGATURE"}) and "SYMBOL" not in words


def read_table(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Each non-ASCII letter or digit (a code point this Python does not know, by its name) that
    is a source of confusables.txt, whose prototype reads as ASCII letters or digits, with that
    reading (a symbol is left out: `fold_canary` drops it, and read as a letter it would join the
    letters of a canary it spells out); then those of them `_fold` reads otherwise once NFKD has
    turned them into other letters, not ASCII. A line is `source ; prototype ; type # ( x → y )
    SOURCE NAME → PROTOTYPE NAME`, in hex code points."""
    pairs = []
    for line in text.splitlines():
        data, _, comment = line.partition("#")
        if not (data := data.strip()):
            continue
        source, prototype = (field.split() for field in data.split(";")[:2])
        name = found.group(1) if (found := NAME.search(comment)) else ""
        char = chr(int(source[0], 16))
        pairs.append((char, "".join(chr(int(code, 16)) for code in prototype), name))
    # the ASCII letters the file spells with others ("m" as "rn"): a prototype so read reads as one
    spelled = {
        read: char.lower()
        for char, prototype, _ in pairs
        if char.isascii() and char.isalpha()
        if len(read := reading(prototype, {})) > 1 and latin(read)
    }
    kept = [
        (char, prototype)
        for char, prototype, name in pairs
        if not char.isascii() and letter_or_digit(char, name)
    ]
    table: dict[str, str] = {}
    while True:  # until no reading changes: a prototype may read through the table itself
        found, look_alikes = {}, str.maketrans(table | UNLISTED)
        for char, prototype in kept:
            read = reading(prototype, look_alikes)
            if latin(read := spelled.get(read, read)):
                found[char] = read
        if found == table:
            break
        table = found
    before = {
        char: read
        for char, read in table.items()
        if not latin(reading(char, {})) and reading(char, look_alikes) != read
    }
    return table, before


def header_field(text: str, name: str) -> str:
    """The value of the `# <name>: …` line of the file's header."""
    if (found := re.search(rf"^# {name}: *(.+?) *$", text, re.MULTILINE)) is None:
        raise SystemExit(f"no '# {name}:' line: is it Unicode's confusables.txt?")
    return found.group(1)


def escaped(chars: str) -> str:
    """`chars` as escapes, since as glyphs they read as Latin."""
    return "".join(f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}" for c in chars)


def entry(read: str, chars: str) -> list[str]:
    """One line of READINGS, or several when its escapes do not fit in one, as ruff writes it."""
    escapes = escaped(chars)
    if len(line := f'    "{read}": "{escapes}",') <= 100:
        return [line]
    lines, chunk = [f'    "{read}": ('], ""
    for escape in re.findall(r"\\u[0-9a-f]{4}|\\U[0-9a-f]{8}", escapes):
        if len(chunk) + len(escape) > CHUNK:
            lines.append(f'        "{chunk}"')
            chunk = ""
        chunk += escape
    return [*lines, f'        "{chunk}"', "    ),"]


def entries(table: dict[str, str]) -> str:
    """The lines of a small table, one character a line with its name, in code point order."""
    return "\n".join(
        f'    "{escaped(char)}": "{table[char]}",  # {unicodedata.name(char)}'
        for char in sorted(table, key=ord)
    )


def render(source: bytes) -> str:
    """The text of src/auditkit/confusables.py, from the bytes of confusables.txt."""
    text = source.decode("utf-8-sig")  # a byte order mark, as older versions have, dropped
    version, date = header_field(text, "Version"), header_field(text, "Date")
    table, before = read_table(text)
    by_reading: dict[str, list[str]] = {}
    for char, read in table.items():
        by_reading.setdefault(read, []).append(char)
    body = [
        line
        for read in sorted(by_reading)
        for line in entry(read, "".join(sorted(by_reading[read], key=ord)))
    ]
    return MODULE.format(
        version=version,
        date=date,
        sha256=hashlib.sha256(source).hexdigest(),
        unidata=unicodedata.unidata_version,
        notice=NOTICE,
        unlisted=entries(UNLISTED),
        before=entries(before),
        body="\n".join(body),
    )


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 2):
        raise SystemExit("usage: gen_confusables.py <confusables.txt> [<out.py>]")
    out = Path(argv[1]) if len(argv) == 2 else OUT
    out.write_text(render(Path(argv[0]).read_bytes()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
