"""Writes docs/img/grid-light.png and grid-dark.png, the README's picture of the page's risk grid.

Each image is a headless Chrome screenshot of a small HTML file built from docs/index.html: the
page's own styles, its legend and its generated grid, nothing else, on a transparent background,
at twice the pixel density. The dark image takes the page's dark colours as its default. Each PNG
carries, in a tEXt chunk, the sha256 of the HTML it was shot from, so tests/test_page.py can tell
without Chrome when the page has changed and the images are stale. Run from the repository root,
after tools/gen_page.py:

    uv run python tools/shoot_grid.py           # shoot both images
    uv run python tools/shoot_grid.py --check   # exit 1 if either is stale

Chrome is found on the PATH (google-chrome, chromium, chromium-browser, chrome) or in its macOS
place; $CHROME names another.
"""

import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "index.html"
IMAGES = ROOT / "docs" / "img"
THEMES = ("light", "dark")
KEY = b"auditkit-source"  # the tEXt keyword of the source hash
PAD = 8  # CSS pixels of transparent margin around the grid
SCALE = 2
MACOS_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# The page's grid box is centred and scrolls; in the picture it shows whole, at its full width.
SHOT_CSS = f"""
html, body {{ background: transparent; margin: 0; }}
body {{ padding: {PAD}px; width: max-content; }}
.legend {{ margin: 0 0 10px; padding: 0 2px; }}
.gridbox {{ width: auto; position: static; left: auto; transform: none; overflow: visible;
  margin: 0; }}
"""
# Writes the picture's size into the DOM, for the measuring run (--dump-dom).
MEASURE = (
    "<script>addEventListener('load', () => { const r = document.querySelector('.shot')"
    ".getBoundingClientRect(); document.body.dataset.size = Math.ceil(r.width) + 'x' + "
    "Math.ceil(r.height); });</script>"
)


def path_of(theme: str) -> Path:
    return IMAGES / f"grid-{theme}.png"


def part(page: str, pattern: str, what: str) -> str:
    found = re.findall(pattern, page, re.DOTALL)
    if len(found) != 1:
        raise SystemExit(f"docs/index.html: no single {what}")
    return found[0]


def styles(css: str, theme: str) -> str:
    """The page's styles for one theme: its dark block left out (light), or made the default."""
    dark = part(
        css, r"(@media \(prefers-color-scheme: dark\) \{\s*:root \{.*?\}\s*\})", "dark block"
    )
    without = css.replace(dark, "")
    if theme == "light":
        return without
    root = part(dark, r"(:root \{.*?\})", "dark :root")
    return without + "\n" + root


def standalone(page: str, theme: str) -> str:
    """The HTML a picture is shot from: the page's styles, legend and grid, for one theme."""
    css = part(page, r"<style>\n(.*?)</style>", "style block")
    legend = part(page, r'(<ul class="legend">.*?</ul>)', "legend")
    grid = part(page, r"<!-- generated: grid -->\n(.*?)<!-- /generated: grid -->", "grid")
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<style>\n{styles(css, theme)}{SHOT_CSS}</style>\n</head>\n<body>\n"
        f'<div class="shot">\n{legend}\n{grid}</div>\n{MEASURE}\n</body>\n</html>\n'
    )


def digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def chunks(png: bytes):
    """Each chunk of a PNG as (type, data, start, end)."""
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    at = 8
    while at < len(png):
        (length,) = struct.unpack(">I", png[at : at + 4])
        kind = png[at + 4 : at + 8]
        yield kind, png[at + 8 : at + 8 + length], at, at + 12 + length
        at += 12 + length


def source_hash(png: bytes) -> str | None:
    """The sha256 a picture carries of the HTML it was shot from, or None."""
    for kind, data, _, _ in chunks(png):
        if kind == b"tEXt" and data.startswith(KEY + b"\0"):
            return data[len(KEY) + 1 :].decode("ascii")
    return None


def stamped(png: bytes, sha: str) -> bytes:
    """The PNG with one tEXt chunk naming the source's hash, right after its header."""
    data = KEY + b"\0" + sha.encode("ascii")
    chunk = struct.pack(">I", len(data)) + b"tEXt" + data
    chunk += struct.pack(">I", zlib.crc32(b"tEXt" + data))
    kept = [png[:8]]
    for kind, body, start, end in chunks(png):
        if kind == b"tEXt" and body.startswith(KEY + b"\0"):
            continue
        kept.append(png[start:end])
        if kind == b"IHDR":
            kept.append(chunk)
    return b"".join(kept)


def chrome() -> str:
    for name in (os.environ.get("CHROME"), "google-chrome", "chromium", "chromium-browser"):
        if name and (found := shutil.which(name)):
            return found
    if Path(MACOS_CHROME).exists():
        return MACOS_CHROME
    raise SystemExit("no Chrome found: install it, or name it in $CHROME")


def run_chrome(*args: str) -> str:
    done = subprocess.run(
        [chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if done.returncode != 0:
        raise SystemExit(f"Chrome failed: {done.stderr.strip()[-400:]}")
    return done.stdout


def shoot(source: str, out: Path) -> None:
    with tempfile.TemporaryDirectory() as folder:
        html_file = Path(folder) / "grid.html"
        html_file.write_text(source, encoding="utf-8")
        url = html_file.as_uri()
        dom = run_chrome("--window-size=2400,2000", "--dump-dom", url)
        width, height = map(int, re.search(r'data-size="(\d+)x(\d+)"', dom).groups())
        shot = Path(folder) / "grid.png"
        run_chrome(
            f"--window-size={width + 2 * PAD},{height + 2 * PAD}",
            f"--force-device-scale-factor={SCALE}",
            "--default-background-color=00000000",
            f"--screenshot={shot}",
            url,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(stamped(shot.read_bytes(), digest(source)))


def stale(page: str) -> list[str]:
    """The pictures that are missing, or were shot from another HTML than the page gives now."""
    late = []
    for theme in THEMES:
        path = path_of(theme)
        if not path.exists() or source_hash(path.read_bytes()) != digest(standalone(page, theme)):
            late.append(str(path.relative_to(ROOT)))
    return late


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if a picture is stale")
    args = parser.parse_args(argv)
    page = PAGE.read_text(encoding="utf-8")
    if args.check:
        late = stale(page)
        for path in late:
            print(f"{path} is out of date: run tools/shoot_grid.py", file=sys.stderr)
        return 1 if late else 0
    for theme in THEMES:
        shoot(standalone(page, theme), path_of(theme))
    return 0


if __name__ == "__main__":
    sys.exit(main())
