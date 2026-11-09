"""A sibling module, imported by pack.py with a relative import."""

VERSION = 1


def normalise(text: str) -> str:
    return " ".join(text.lower().split())
