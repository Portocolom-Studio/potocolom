"""Export the /illusions figures from their draw.io sources, light and dark.

docs/figures/<name>.drawio is the source of truth for each figure. Open one in
draw.io, edit it, save it, then run this to refresh the webp files the page ships:

  python3 scripts/render-illusion-figures.py              # every figure
  python3 scripts/render-illusion-figures.py sds recipe   # only these

Each source becomes <name>.webp for the light theme and <name>-dark.webp for the
dark one. The dark twin is derived here, never drawn by hand, so the two cannot
drift apart. Photos and charts are embedded in the .drawio files, so an export
needs only the draw.io desktop CLI and the Lato font installed.
"""

import argparse
import io
import re
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCALE = 2.5

# The light palette reuses one ink for text, card borders and plotted lines, and
# on a dark ground those need three different values: text turns light, a card
# border turns quiet, a drawn line turns bright. So colours map by role.
DARK_BG, DARK_CARD, DARK_RAISED = "#141413", "#1c1b18", "#24231f"
DARK_TEXT = {
    "#2d3142": "#edecec", "#1b1f2a": "#edecec", "#4f5d75": "#b3b1ac", "#5b6472": "#b3b1ac",
    "#7a8399": "#8a8883", "#8b93a1": "#8a8883", "#005ee3": "#79a8ff", "#b4232a": "#ff7d70",
    "#ffffff": DARK_BG, "#9aa1ad": "#6f6d68",
}
DARK_FILL = {
    "#f5f5f5": DARK_BG, "#ffffff": DARK_CARD, "#e6effc": "#172238", "#eeeff2": "#1f1e1b",
    "#e4e7ec": DARK_RAISED, "#ecedf0": "#1a1917", "#e8eaee": DARK_RAISED, "#2d3142": "#e6e4e0",
    "#005ee3": "#79a8ff", "#b4232a": "#ff7d70", "#4f5d75": "#a9a7a2", "#7a8399": "#8a8883",
    "#c9cdd6": "#3a3934", "#1b1f2a": "#e6e4e0",
}
DARK_BORDER = {
    "#2d3142": "#4a4943", "#005ee3": "#79a8ff", "#b4232a": "#ff7d70", "#c9cdd6": "#34332e",
    "#8e98ac": "#5a5852", "#4f5d75": "#6d6b65", "#7a8399": "#5f5d58", "#ffffff": DARK_CARD,
}
DARK_LINE = {
    "#2d3142": "#dcdad5", "#4f5d75": "#8e8c86", "#7a8399": "#6f6d68", "#005ee3": "#79a8ff",
    "#b4232a": "#ff7d70", "#c9cdd6": "#4a4943", "#8e98ac": "#6f6d68",
}


def _swap(table, colour):
    if colour.lower() == "none":
        return colour
    try:
        return table[colour.lower()]
    except KeyError:
        raise SystemExit(f"no dark colour for {colour}: add it to the dark tables") from None


def _dark_style(style, is_edge):
    roles = {"fontColor": DARK_TEXT, "fillColor": DARK_FILL, "labelBackgroundColor": DARK_FILL,
             "strokeColor": DARK_LINE if is_edge else DARK_BORDER,
             "imageBorder": DARK_LINE if is_edge else DARK_BORDER}
    return re.sub(r"(fontColor|fillColor|strokeColor|imageBorder|labelBackgroundColor)="
                  r"(#[0-9a-fA-F]{6}|none)",
                  lambda m: f"{m.group(1)}={_swap(roles[m.group(1)], m.group(2))}", style)


def darken(xml):
    """The dark twin of a light figure's draw.io XML."""
    cells = []
    for cell in re.split(r"(?=<mxCell )", xml):
        is_edge = 'edge="1"' in cell.split(">", 1)[0]
        cell = re.sub(r'style="([^"]*)"', lambda m: f'style="{_dark_style(m.group(1), is_edge)}"',
                      cell, count=1)
        # colours inside an html label are text colours
        cell = re.sub(r'value="([^"]*)"', lambda m: 'value="' + re.sub(
            r"(color:\s*)(#[0-9a-fA-F]{6})", lambda c: c.group(1) + _swap(DARK_TEXT, c.group(2)),
            m.group(1)) + '"', cell, count=1)
        cells.append(cell)
    return "".join(cells).replace('background="#f5f5f5"', f'background="{DARK_BG}"')


def export(src, dest):
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / f"{src.stem}.png"
        subprocess.run(["drawio", "-x", "-f", "png", "-s", str(SCALE), "-b", "0",
                        "-o", str(png), str(src), "--no-sandbox"],
                       check=True, capture_output=True)
        image = Image.open(png)
        lossless, lossy = io.BytesIO(), io.BytesIO()
        image.save(lossless, format="WEBP", lossless=True, method=6)
        image.save(lossy, format="WEBP", quality=93, method=6)
        # Text-only figures compress smaller lossless than lossy, and stay crisp.
        best = min((lossless, lossy), key=lambda buf: buf.getbuffer().nbytes)
        dest.write_bytes(best.getvalue())
        return image.size


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="figure names; default is every .drawio")
    parser.add_argument("--src", type=Path, default=ROOT / "docs" / "figures")
    parser.add_argument("--out", type=Path, default=ROOT / "frontend" / "static" / "illusions")
    args = parser.parse_args()
    sources = ([args.src / f"{name}.drawio" for name in args.names] if args.names
               else sorted(args.src.glob("*.drawio")))
    missing = [str(src) for src in sources if not src.exists()]
    if missing:
        raise SystemExit("no such figure: " + ", ".join(missing))
    args.out.mkdir(parents=True, exist_ok=True)
    for src in sources:
        width, height = export(src, args.out / f"{src.stem}.webp")
        with tempfile.TemporaryDirectory() as tmp:
            dark_src = Path(tmp) / src.name
            dark_src.write_text(darken(src.read_text()))
            export(dark_src, args.out / f"{src.stem}-dark.webp")
        print(f"{src.stem}: {width}x{height}, light and dark")


if __name__ == "__main__":
    main()
