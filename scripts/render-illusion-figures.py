"""Export the /illusions figures from their draw.io sources.

docs/figures/<name>.drawio is the source of truth for each figure. Open one in
draw.io, edit it, save it, then run this to refresh the webp the page ships:

  python3 scripts/render-illusion-figures.py              # every figure
  python3 scripts/render-illusion-figures.py sds recipe   # only these

Photos and charts are embedded in the .drawio files, so an export needs only
the draw.io desktop CLI and the Lato font installed, not the research checkout.
"""

import argparse
import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SCALE = 2.5


def export(src, out_dir):
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
        (out_dir / f"{src.stem}.webp").write_bytes(best.getvalue())
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
        width, height = export(src, args.out)
        print(f"{src.stem}: {width}x{height}")


if __name__ == "__main__":
    main()
