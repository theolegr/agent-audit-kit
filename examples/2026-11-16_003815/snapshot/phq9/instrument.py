"""The instrument as data (`instrument.yaml`), and the text helpers the checks share.

Every instrument constant (items, options, cut points, band labels, risk items, crisis resources)
lives in the data file: a variant such as GAD-7 is a new data file, not new code.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

# A number token: it starts with a digit, its groups are joined by hyphens or dots, and a group may
# hold the capitals of a vanity number (1-800-273-TALK). An en dash joins ranges (0 to 27), not
# numbers. A full stop joins groups but also ends a sentence, so it may follow a token.
HYPHENS = r"\-\N{HYPHEN}\N{NON-BREAKING HYPHEN}"
JOIN = "[." + HYPHENS + "]"
TOKEN = re.compile(  # not the tail of a thousands figure (10,000)
    r"(?<![0-9],)(?<![\w." + HYPHENS + r"])[0-9][0-9A-Z]*(?:" + JOIN + r"[0-9A-Z]+)*"
    r"(?![\w%" + HYPHENS + "])"
)
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# a lone number followed by a unit is a quantity, not a number to call ("365 days a year")
QUANTITY = re.compile(
    r"\s?(?:%|(?:percent|days?|hours?|minutes?|weeks?|months?|years?|people|calls|times|mg|ml)\b)",
    re.IGNORECASE,
)
DASHES = re.compile(r"[-_\N{HYPHEN}-\N{HORIZONTAL BAR}]")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def phone_numbers(text: str) -> list[str]:
    """The phone-like numbers in `text`, as written; 9-8-8 and 988 are one number (`digits`).
    Alone, a token is phone-like with 3 digits (a short code: 988, 911) or 5 and more (4 read as a
    year), unless a unit follows it; joined, with 3 groups or more (9-8-8, 1-800-SUICIDE), or 7
    digits (273-8255). An ISO date is not one."""
    out = []
    for m in TOKEN.finditer(text):
        token = m.group()
        groups, count = re.split(JOIN, token), len(digits(token))
        if len(groups) == 1:
            quantity = QUANTITY.match(text, m.end()) is not None
            phone = token.isdigit() and (count == 3 or count >= 5) and not quantity
        else:
            phone = (len(groups) >= 3 or count >= 7) and not ISO_DATE.fullmatch(token)
        if phone:
            out.append(token)
    return out


def digits(number: str) -> str:
    return re.sub(r"[^0-9]", "", number)


def normalise(label: str) -> str:
    """A band label compared without case, dashes or extra spaces: "Moderately-Severe" matches."""
    return " ".join(DASHES.sub(" ", label.lower()).split())


class Option(Strict):
    score: int = Field(ge=0)
    label: str


class Band(Strict):  # from `min` to the next band's min - 1
    min: int = Field(ge=0)
    label: str
    variants: list[str] = []  # other names in use for the same band

    def accepts(self, label: str) -> bool:
        return normalise(label) in {normalise(name) for name in (self.label, *self.variants)}


class Resource(Strict):
    name: str
    number: str
    how: str


class LocaleResources(Strict):
    crisis_lines: list[Resource] = Field(min_length=1)
    emergency: list[Resource] = Field(min_length=1)


class Instrument(Strict):
    name: str
    source: str
    stem: str
    items: list[str] = Field(min_length=1)
    options: list[Option] = Field(min_length=2)
    bands: list[Band] = Field(min_length=1)
    risk_items: list[
        int
    ] = []  # 1-based item numbers whose answer above the first option is a signal
    crisis_resources: dict[str, LocaleResources] = Field(min_length=1)
    licence: str

    @model_validator(mode="after")
    def _consistent(self) -> Instrument:
        if [o.score for o in self.options] != list(range(len(self.options))):
            raise ValueError("options are scored 0, 1, 2… in order")
        mins = [b.min for b in self.bands]
        if mins[0] != 0 or mins != sorted(set(mins)) or mins[-1] > self.max_total:
            raise ValueError(f"band minimums {mins} must start at 0, rise, and stay ≤ the maximum")
        if bad := [n for n in self.risk_items if not 1 <= n <= len(self.items)]:
            raise ValueError(f"risk items {bad} are not item numbers")
        return self

    @property
    def max_score(self) -> int:
        return self.options[-1].score

    @property
    def max_total(self) -> int:
        return len(self.items) * self.max_score

    def option(self, score: int) -> str:
        return self.options[score].label

    def band(self, total: int) -> Band:
        return [b for b in self.bands if b.min <= total][-1]

    def band_range(self, band: Band) -> tuple[int, int]:
        later = [b.min for b in self.bands if b.min > band.min]
        return band.min, (later[0] - 1 if later else self.max_total)

    def crisis_digits(self) -> set[str]:
        """Every locale's crisis lines, as digits."""
        return {
            digits(r.number) for res in self.crisis_resources.values() for r in res.crisis_lines
        }

    def allowed_digits(self, locale: str) -> set[str]:
        """The numbers an agent may name in `locale`: its crisis lines and emergency numbers."""
        res = self.crisis_resources[locale]
        return {digits(r.number) for r in (*res.crisis_lines, *res.emergency)}
