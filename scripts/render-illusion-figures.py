"""Build the ten /illusions figures as self-contained HTML, then export webp.

Each figure is one inline SVG: boxes, arrows, math, and real run photos
embedded as base64 data URIs. Headless Chrome screenshots the page at
scale 2, and PIL writes the webp that the page ships.

Usage (from repo root):
  python3 scripts/render-illusion-figures.py            # write into frontend/static/illusions
  python3 scripts/render-illusion-figures.py --out /tmp/figs --only sds recipe

Photo wells read from frontend/static/illusions (committed) and from the
gitignored research exports (.local/illusion-reliability/...). Without the
research checkout the photo figures cannot build, and the script says so.

Every number printed on a figure is either a CLI default read from
worker/worker/illusions.py or a measured value from the window-2 campaign.
The two differ: the gallery ran --experimental-recipe author_reference,
which swaps the network, the optimizer, the guidance, and the SDS
objective. Figures that show a photo label the recipe that made it.
"""

import argparse
import base64
import io
import math
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static" / "illusions"
LOCAL = ROOT / ".local" / "illusion-reliability"
CLEAN = LOCAL / "keepers" / "window2-2026-08-clean"
SMOKE = LOCAL / "campaigns" / "window2" / "smoke"
SMOKE_ARM = SMOKE / "arm_neg_on_indep"
SWAN = LOCAL / "campaigns/window2/runs/window2/a_forked_reference_sketch/elephant_swan/seed_11/attempt_001"
EAGLE = LOCAL / "campaigns/window2/runs/window2/a_forked_reference_sketch/eagle_phoenix/seed_11/attempt_001"

FONTS = "https://fonts.googleapis.com/css2?family=Lato:wght@300;400;700;900&display=swap"
SANS = "'Lato','DejaVu Sans',sans-serif"
MONO = "'DejaVu Sans Mono',ui-monospace,monospace"
DISPLAY = "'Lato','DejaVu Sans',sans-serif"

PAPER, INK = "#f5f5f5", "#2d3142"
MUTED, SOFT = "#4f5d75", "#7a8399"
ACCENT, ACCENT_TINT = "#eb6c36", "rgba(235,108,54,0.08)"
CUT = "#b4232a"
RULE = "rgba(45,49,66,0.12)"
INPUT_FILL, INPUT_STROKE = "rgba(79,93,117,0.10)", "#8e98ac"
KEEP_PNG = False
NOTE_FILL, NOTE_STROKE = "rgba(45,49,66,0.02)", "rgba(45,49,66,0.20)"


def chrome() -> str:
    """Headless Chrome, preferring the system browser over the playwright copy."""
    system = Path("/usr/bin/google-chrome")
    if system.exists():
        return str(system)
    pattern = ".cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"
    found = sorted(Path.home().glob(pattern))
    if not found:
        raise SystemExit("no headless chrome: install google-chrome or playwright chromium")
    return str(found[-1])


def photo_uri(path, size=320, quality=72, rotate=0):
    img = Image.open(path).convert("RGB")
    if rotate:
        img = img.rotate(rotate)
    img.thumbnail((size, size))
    return _jpeg_uri(img, quality)


def _jpeg_uri(img, quality=72):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def svg_open(slug, title, desc, w, h):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'width="{w}" height="{h}" role="img" aria-labelledby="{slug}-title {slug}-desc">'
            f'<title id="{slug}-title">{title}</title><desc id="{slug}-desc">{desc}</desc>'
            f'<defs>'
            f'<marker id="arr" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">'
            f'<polygon points="0 0,8 3,0 6" fill="{MUTED}"/></marker>'
            f'<marker id="arr-a" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">'
            f'<polygon points="0 0,8 3,0 6" fill="{ACCENT}"/></marker>'
            f'</defs><rect width="100%" height="100%" fill="{PAPER}"/>')


def shell(svg):
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8">'
            f'<link href="{FONTS}" rel="stylesheet">'
            f'<style>html,body{{margin:0;padding:0;background:{PAPER};}}</style>'
            f'</head><body>{svg}</body></html>')


def eyebrow(x, y, text):
    return (f'<text x="{x}" y="{y}" fill="{SOFT}" font-size="11" font-family="{MONO}" '
            f'letter-spacing="0.14em">{text}</text>')


def heading(x, y, text, size=28):
    return (f'<text x="{x}" y="{y}" fill="{INK}" font-size="{size}" font-family="{DISPLAY}" '
            f'font-weight="700" letter-spacing="-0.01em">{text}</text>')


def box(x, y, w, h, name, sub=None, fill="#ffffff", stroke=INK, name_size=12, sub_size=9):
    s = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{PAPER}"/>',
         f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" '
         f'stroke="{stroke}" stroke-width="1"/>']
    lines = sub.split("\n") if sub else []
    cy = y + h / 2 - (len(lines) * 7 - 4)
    s.append(f'<text x="{x + w / 2}" y="{cy}" fill="{INK}" font-size="{name_size}" font-weight="600" '
             f'font-family="{SANS}" text-anchor="middle">{name}</text>')
    for i, line in enumerate(lines):
        s.append(f'<text x="{x + w / 2}" y="{cy + 18 + i * 14}" fill="{MUTED}" '
                 f'font-size="{sub_size}" font-family="{MONO}" text-anchor="middle">{line}</text>')
    return "\n".join(s)


def caption(x, y, text, size=9):
    return (f'<text x="{x}" y="{y}" fill="{SOFT}" font-size="{size}" font-family="{MONO}" '
            f'text-anchor="middle">{text}</text>')


def note(x, y, text, size=10, anchor="middle", fill=MUTED):
    return (f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" font-family="{MONO}" '
            f'text-anchor="{anchor}">{text}</text>')


def arrow(x1, y1, x2, y2, colour=MUTED, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    marker = "arr-a" if colour == ACCENT else "arr"
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{colour}" '
            f'stroke-width="1.2"{d} marker-end="url(#{marker})"/>')


def path(d, colour=MUTED, dash=None, head=True):
    da = f' stroke-dasharray="{dash}"' if dash else ""
    marker = ""
    if head:
        marker = ' marker-end="url(#arr-a)"' if colour == ACCENT else ' marker-end="url(#arr)"'
    return f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="1.2"{da}{marker}/>'


def arrow_label(x, y, text, w=120):
    return (f'<rect x="{x - w / 2}" y="{y - 18}" width="{w}" height="12" rx="2" fill="{PAPER}"/>'
            f'<text x="{x}" y="{y - 9}" fill="{SOFT}" font-size="8" font-family="{MONO}" '
            f'text-anchor="middle" letter-spacing="0.06em">{text}</text>')


def legend(items, w, y):
    s = [f'<line x1="30" y1="{y - 8}" x2="{w - 30}" y2="{y - 8}" stroke="{RULE}" stroke-width="0.8"/>',
         f'<text x="30" y="{y + 8}" fill="{MUTED}" font-size="8" font-family="{MONO}" '
         f'letter-spacing="0.14em">LEGEND</text>']
    x = 140
    step = min(170, (w - 200) / max(len(items), 1))
    for swatch, label in items:
        s.append(f'<rect x="{x}" y="{y - 2}" width="10" height="10" fill="{swatch}"/>')
        s.append(f'<text x="{x + 16}" y="{y + 7}" fill="{MUTED}" font-size="9" '
                 f'font-family="{SANS}">{label}</text>')
        x += step
    return "\n".join(s)


def photo(uri, x, y, size, label=None, label_gap=18):
    s = [f'<image href="{uri}" x="{x}" y="{y}" width="{size}" height="{size}"/>']
    if label:
        s.append(caption(x + size / 2, y + size + label_gap, label))
    return "\n".join(s)


def write(out, slug, svg, w, h):
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / f"{slug}.html"
    png_path = out / f"{slug}.png"
    html_path.write_text(shell(svg))
    subprocess.run([chrome(), "--headless", "--disable-gpu", "--no-sandbox",
                    f"--screenshot={png_path}", f"--window-size={w},{h}",
                    "--hide-scrollbars", "--force-device-scale-factor=2",
                    "--virtual-time-budget=5000", f"file://{html_path}"],
                   check=True, capture_output=True)
    Image.open(png_path).save(out / f"{slug}.webp", quality=85)
    if not KEEP_PNG:
        png_path.unlink()
        html_path.unlink()
    print(f"{slug}: {w * 2}x{h * 2}")


def alphas_cumprod(steps=1000, beta_start=0.00085, beta_end=0.012):
    """SD 1.5's scaled_linear schedule, straight from its scheduler_config.json."""
    lo, hi = beta_start ** 0.5, beta_end ** 0.5
    out, running = [], 1.0
    for i in range(steps):
        beta = (lo + (hi - lo) * i / (steps - 1)) ** 2
        running *= 1.0 - beta
        out.append(running)
    return out


def weight_panel(x, y, pw, ph):
    """w(t) = 1 - alpha_bar(t) over the schedule, with the sampled window shaded.

    Readers keep reading w as the guidance scale. Drawing it settles the point:
    w lives in [0, 1] and G is 60.
    """
    ac = alphas_cumprod()
    weights = [1.0 - a for a in ac]
    x0, x1 = x + 50, x + pw - 24
    base, span = y + ph - 60, ph - 114
    lo, hi = int(0.02 * len(weights)), int(0.98 * len(weights))
    s = [box(x, y, pw, ph, "", None, fill="#ffffff"),
         note(x + 30, y + 32, "TIMESTEP WEIGHT w(t) = 1 − ᾱt", 9, anchor="start", fill=SOFT),
         f'<rect x="{x0 + (x1 - x0) * lo / len(weights):.1f}" y="{base - span}" '
         f'width="{(x1 - x0) * (hi - lo) / len(weights):.1f}" height="{span}" '
         f'fill="{ACCENT_TINT}"/>',
         f'<line x1="{x0}" y1="{base}" x2="{x1}" y2="{base}" stroke="{RULE}" stroke-width="1"/>',
         f'<line x1="{x0}" y1="{base - span}" x2="{x0}" y2="{base}" stroke="{RULE}" stroke-width="1"/>']
    pts = " ".join(f"{x0 + (x1 - x0) * i / (len(weights) - 1):.1f},{base - v * span:.1f}"
                   for i, v in enumerate(weights))
    s.append(f'<polyline points="{pts}" fill="none" stroke="{INK}" stroke-width="1.6"/>')
    s.append(note(x0 - 8, base - span + 4, "1.0", 9, anchor="end", fill=SOFT))
    s.append(note(x0 - 8, base + 3, "0", 9, anchor="end", fill=SOFT))
    s.append(note(x0, base + 18, "t = 0", 9, anchor="start", fill=SOFT))
    s.append(note(x1, base + 18, "t = 1000", 9, anchor="end", fill=SOFT))
    s.append(note(x0 + (x1 - x0) * 0.5, base - span - 6, "sampled window: 2% to 98%", 9, fill=ACCENT))
    s.append(note(x + pw / 2, base + 36, "w runs 0.019 to 0.994 across that window.", 10))
    s.append(note(x + pw / 2, base + 52, "G is 60. They are different numbers.", 10))
    return "\n".join(s)


SDS_OBJECTIVES = [
    ("legacy", "εcfg − ε", "no w(t) at all", "the CLI default"),
    ("weighted_sds", "w(t)·(εcfg − ε)", "w(t) scales it", "this gallery"),
    ("csd", "w(t)·(εc − εu)", "ignores G", "not used here"),
    ("nfsd", "w(t)·(δD + G·δC)", "adds a δD term", "not used here"),
]


def objective_panel(x, y, pw, ph):
    """The four residuals compute_sds_gradient can build, side by side."""
    s = [box(x, y, pw, ph, "", None, fill="#ffffff"),
         note(x + 30, y + 32, "THE FOUR RESIDUALS r", 9, anchor="start", fill=SOFT)]
    cols = [(x + 30, "objective"), (x + 158, "r ="), (x + 330, "note"), (x + 452, "used")]
    for cx, label in cols:
        s.append(f'<text x="{cx}" y="{y + 56}" fill="{SOFT}" font-size="9" '
                 f'font-family="{MONO}" letter-spacing="0.1em">{label.upper()}</text>')
    ry = y + 82
    for name, expr, why, used in SDS_OBJECTIVES:
        live = used == "this gallery"
        if live:
            s.append(f'<rect x="{x + 22}" y="{ry - 14}" width="{pw - 44}" height="22" rx="4" '
                     f'fill="{ACCENT_TINT}"/>')
        ink = ACCENT if live else INK
        s.append(f'<text x="{x + 30}" y="{ry}" fill="{ink}" font-size="11" font-weight="600" '
                 f'font-family="{MONO}">{name}</text>')
        s.append(f'<text x="{x + 158}" y="{ry}" fill="{ink}" font-size="11" '
                 f'font-family="{MONO}">{expr}</text>')
        s.append(f'<text x="{x + 330}" y="{ry}" fill="{MUTED}" font-size="10" '
                 f'font-family="{SANS}">{why}</text>')
        s.append(f'<text x="{x + 452}" y="{ry}" fill="{ACCENT if live else SOFT}" font-size="10" '
                 f'font-family="{MONO}">{used}</text>')
        ry += 28
    s.append(note(x + pw / 2, y + ph - 16,
                  "every symbol here is in the reference table below", 10))
    return "\n".join(s)


# --------------------------------------------------------------- figures

def fig_architecture(out):
    prime = photo_uri(STATIC / "elephant-swan-prime.webp", 300)
    view1 = photo_uri(STATIC / "elephant-swan-view.webp", 300)
    view2 = photo_uri(CLEAN / "s5-elephant_swan-seed11-oil-neg_off_joint-final-view2.png", 300)
    w, h = 1280, 720
    s = [svg_open("arch", "Flip pipeline: one prime, two views",
                  "Prime p becomes views d1 and d2 through flip arrangements. Frozen "
                  "diffusion scores both views. Gradients update only the prime weights.",
                  w, h)]
    s.append(eyebrow(40, 44, "DIFFUSION ILLUSIONS · FLIP PIPELINE"))
    s.append(heading(40, 78, "One prime, two views"))
    s.append(path("M115,184 V260"))
    s.append(path("M190,300 H337 Q345,300 345,292 V254"))
    s.append(path("M190,335 H292 Q300,335 300,343 V400"))
    s.append(arrow(440, 212, 500, 212))
    s.append(path("M440,442 H462 Q470,442 470,450 V467 Q470,475 478,475 H500"))
    s.append(path("M650,215 H672 Q680,215 680,223 V300 Q680,308 688,308 H710"))
    s.append(path("M650,475 H672 Q680,475 680,467 V330 Q680,322 688,322 H710"))
    s.append(path("M825,240 V112 Q825,104 817,104 H123 Q115,104 115,112 V116", ACCENT, "4,3"))
    s.append(note(700, 96, "GRAD θ", 9, fill=SOFT))
    s.append(box(40, 120, 150, 64, "θ · FFN weights", "only thing trained",
                 fill=ACCENT_TINT, stroke=ACCENT))
    s.append(photo(prime, 40, 260, 150, "p · print this"))
    s.append(box(250, 170, 190, 84, "a₁(p) = p", "identity"))
    s.append(box(250, 400, 190, 84, "a₂(p) = rot₁₈₀(p)", "180° turn"))
    s.append(photo(view1, 500, 140, 150, "d₁ · upright"))
    s.append(photo(view2, 500, 400, 150, "d₂ · after the turn"))
    s.append(box(710, 240, 240, 140, "Frozen diffusion",
                 "SDS: r = w(t)·(εcfg − ε)\nDream: L = (1−SSIM) + MSE\nSD 1.5 · DreamShaper LCM"))
    s.append(box(990, 240, 250, 140, "nothing else moves",
                 "UNet, VAE, text encoder\narrangements are fixed ops\nphotos: elephant + swan, seed 11",
                 fill=NOTE_FILL, stroke=NOTE_STROKE))
    s.append(note(640, 604, "d₁ = a₁(p) = p · d₂ = a₂(p) = rot₁₈₀(p) · the printed sheet is p"))
    s.append(legend([(INPUT_FILL, "input"), (ACCENT_TINT, "trainable"),
                     ("#ffffff", "frozen"), (NOTE_FILL, "note")], w, 648))
    s.append("</svg>")
    write(out, "architecture", "\n".join(s), w, h)


def fig_ffn(out):
    prime_path = STATIC / "elephant-swan-prime.webp"
    grid = Image.new("RGB", (150, 130), "white")
    gx = ImageDraw.Draw(grid)
    for j in range(13):
        for i in range(15):
            gx.rectangle([8 + i * 9, 8 + j * 9, 8 + (i + 1) * 9 - 1, 8 + (j + 1) * 9 - 1],
                         fill=(int(255 * i / 14), int(255 * j / 12), 128))
    waves = Image.new("RGB", (220, 130), "white")
    wx = ImageDraw.Draw(waves)
    wx.line([(8, 65), (212, 65)], fill="#94a3b8", width=1)
    wx.line([(8 + i * 2, 65 - int(48 * math.sin(i / 31.8 * 2 * math.pi))) for i in range(103)],
            fill="#2563eb", width=3)
    wx.line([(8 + i * 2, 65 - int(48 * math.cos(i / 31.8 * 2 * math.pi))) for i in range(103)],
            fill="#dc2626", width=3)
    prime_img = Image.open(prime_path).convert("RGB")
    crop_a = _jpeg_uri(prime_img.crop((60, 150, 124, 214)).resize((150, 130)))
    crop_b = _jpeg_uri(prime_img.crop((120, 40, 184, 104)).resize((150, 130)))

    w, h = 1280, 720
    s = [svg_open("ffn", "Prime network: coordinates become printable RGB",
                  "Pixel coordinates pass fixed Fourier features and a small trained "
                  "network. Evidence below shows smooth printable output.", w, h)]
    s.append(eyebrow(40, 44, "PRIME NETWORK · FOURIER FEATURES"))
    s.append(heading(40, 78, "Coordinates in, printable RGB out"))
    for x1, x2 in [(140, 170), (360, 390), (560, 590), (870, 900), (1050, 1080)]:
        s.append(arrow(x1, 200, x2, 200))
    s.append(box(40, 160, 100, 80, "(x, y)", "grid in [0,1)", fill=INPUT_FILL, stroke=INPUT_STROKE))
    s.append(box(170, 160, 190, 80, "B ∼ N(0, 10²)", "2 x 128 · fixed buffer"))
    s.append(box(390, 160, 170, 80, "sin + cos", "256-d features"))
    s.append(box(590, 140, 280, 120, "Conv 1x1 · 256-256-256-256-3",
                 "ReLU + BatchNorm x3\n199,683 trained weights", fill=ACCENT_TINT, stroke=ACCENT))
    s.append(box(900, 160, 150, 80, "σ · sigmoid", "RGB (1,3,256,256)"))
    s.append(photo(photo_uri(prime_path, 320), 1080, 120, 160, "printable prime"))
    s.append(box(590, 290, 460, 56, "this is the gallery network: --experimental-recipe author_reference",
                 None, fill=NOTE_FILL, stroke=NOTE_STROKE, name_size=10))
    s.append(box(40, 290, 520, 56,
                 "CLI default instead: Linear MLP 512-256-256-256-3 · B 2 x 256 · 512px · 263,683 θ",
                 None, fill=NOTE_FILL, stroke=NOTE_STROKE, name_size=10))
    s.append(f'<image href="{_jpeg_uri(grid)}" x="60" y="400" width="150" height="130"/>')
    s.append(caption(135, 548, "every pixel: (x, y)"))
    s.append(f'<image href="{_jpeg_uri(waves)}" x="250" y="400" width="220" height="130"/>')
    s.append(caption(360, 548, "fixed waves, not learned"))
    s.append(f'<image href="{crop_a}" x="510" y="400" width="150" height="130"/>')
    s.append(f'<image href="{crop_b}" x="680" y="400" width="150" height="130"/>')
    s.append(caption(670, 548, "2x crops: smooth, printable"))
    s.append(box(870, 400, 370, 130, "pixels: hide the art in noise",
                 "weights: hold the shape\nsmooth enough to print\npaper Sec. 4.3"))
    s.append(note(640, 606, "v = [ sin(2πBx) ‖ cos(2πBx) ] · RGB = σ(net(v))"))
    s.append(legend([(INPUT_FILL, "input"), (ACCENT_TINT, "trainable"),
                     ("#ffffff", "fixed"), (NOTE_FILL, "note")], w, 648))
    s.append("</svg>")
    write(out, "ffn", "\n".join(s), w, h)


def fig_sds(out):
    view1 = photo_uri(STATIC / "elephant-swan-view.webp", 300)
    w, h = 1280, 800
    s = [svg_open("sds", "One Score Distillation step",
                  "The derived view encodes to a latent, gains noise, and the frozen UNet "
                  "scores it. The residual re-enters as a gradient on the prime weights only.",
                  w, h)]
    s.append(eyebrow(40, 44, "SCORE DISTILLATION · ONE STEP"))
    s.append(heading(40, 78, "Noise in, gradient out"))
    for x1, x2 in [(150, 180), (300, 330), (460, 490), (640, 670), (830, 860), (1080, 1110)]:
        s.append(arrow(x1, 215, x2, 215))
    s.append(box(40, 170, 110, 90, "θ", "FFN weights", fill=ACCENT_TINT, stroke=ACCENT))
    s.append(box(180, 170, 120, 90, "render p", "differentiable"))
    s.append(photo(view1, 330, 155, 120, "d = a(p)"))
    s.append(box(490, 170, 150, 90, "VAE encode", "z · (1,4,64,64)"))
    s.append(box(670, 170, 160, 90, "add noise", "z_t = sched(z, ε, t)"))
    s.append(box(860, 155, 220, 120, "frozen UNet",
                 "one CFG-doubled forward\nreturns εu and εc"))
    s.append(box(1110, 170, 130, 90, "residual r", "built no_grad"))
    s.append(path("M95,265 V300 Q95,308 103,308 H557 Q565,308 565,300 V265", ACCENT, head=False))
    s.append(note(330, 330, "GRADIENT PATH: θ TO z", 9, fill=SOFT))
    s.append(f'<line x1="845" y1="180" x2="845" y2="250" stroke="{CUT}" stroke-width="1.6"/>')
    s.append(note(838, 140, "no grad past here", 9, anchor="end", fill=CUT))
    s.append(box(40, 350, 580, 130, "how the gradient is made",
                 "εcfg = εu + G·(εc − εu)\n"
                 "r = w(t)·(εcfg − ε)\n"
                 "w(t) = 1 − ᾱt, the timestep weight\n"
                 "L = (z · r.detach()).sum(), then the optimizer steps θ",
                 sub_size=11))
    s.append(box(660, 350, 580, 130, "what the flags change",
                 "--experimental-recipe author_reference swapped the network,\n"
                 "the optimizer, G and the objective in one flag\n"
                 "--view-batch-size splits the one forward into chunks\n"
                 "sds_gradient_scale multiplied r by 0.1 for this gallery",
                 fill=NOTE_FILL, stroke=NOTE_STROKE, sub_size=11))
    s.append(objective_panel(660, 512, 580, 214))
    s.append(weight_panel(40, 512, 580, 214))
    s.append(legend([(ACCENT_TINT, "trainable"), ("#ffffff", "frozen or fixed"),
                     (NOTE_FILL, "note"), (CUT, "gradient stops")], w, 762))
    s.append("</svg>")
    write(out, "sds", "\n".join(s), w, h)


CHECKPOINTS = [(250, 173.1, 2237.6), (1000, -134.5, 2097.7),
               (2500, 29.2, 952.1), (5000, -685.4, 1282.4)]


def fig_two_phase(out):
    names = [SMOKE / f"ckpt_sds_{n:04d}" / "derived_1.png" for n in (250, 1000, 2500, 5000)]
    names.append(SMOKE_ARM / "ckpt_dream_round_01" / "derived_1.png")
    names.append(SMOKE_ARM / "ckpt_final" / "derived_1.png")
    labels = ["SDS 250", "SDS 1000", "SDS 2500", "SDS 5000", "Dream r1", "final"]
    w, h = 1280, 780
    s = [svg_open("two-phase", "Two phases: real optimization checkpoints",
                  "A giraffe sketch from noise at step 250 to a clean final image. Phase 1 "
                  "distills, a fresh Adam starts phase 2, Dream Target polishes.", w, h)]
    s.append(eyebrow(40, 44, "TWO PHASES · REAL CHECKPOINTS"))
    s.append(heading(40, 78, "Noise, then a giraffe"))
    s.append(box(60, 120, 760, 56, "Phase 1 · SDS · frozen SD 1.5", None))
    s.append(box(830, 120, 130, 56, "fresh Adam", None, fill=ACCENT_TINT, stroke=ACCENT))
    s.append(box(970, 120, 250, 56, "Phase 2 · Dream Target", None))
    x = 60
    for src, lab in zip(names, labels):
        s.append(photo(photo_uri(src, 320), x, 200, 160, lab, 28))
        x += 200
    s.append(note(640, 408, "window-2 calibration smoke run · giraffe and penguin · seed 11 · "
                            "Dream frames from the independent arm with the negative prompt on", 9))

    s.append(box(40, 428, 580, 252, "", None, fill="#ffffff"))
    s.append(note(70, 460, "DREAM STRENGTH LADDER", 9, anchor="start", fill=SOFT))
    x0, x1, base, span = 90, 570, 616, 130
    s.append(f'<line x1="{x0}" y1="{base}" x2="{x1}" y2="{base}" stroke="{RULE}" stroke-width="1"/>')
    s.append(f'<line x1="{x0}" y1="{base - span}" x2="{x0}" y2="{base}" stroke="{RULE}" stroke-width="1"/>')
    ladder = [0.9 * (1 - i / 7) + 0.05 for i in range(8)]
    points = [(x0 + i * (x1 - x0) / 7, base - v * span) for i, v in enumerate(ladder)]
    s.append('<polyline points="' + " ".join(f"{px:.1f},{py:.1f}" for px, py in points) +
             f'" fill="none" stroke="{MUTED}" stroke-width="1.4"/>')
    for (px, py), v in zip(points, ladder):
        s.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.5" fill="{MUTED}"/>')
    s.append(f'<circle cx="{points[0][0]:.1f}" cy="{points[0][1]:.1f}" r="6" fill="none" '
             f'stroke="{ACCENT}" stroke-width="2"/>')
    s.append(note(x0 + 14, base - span - 4, "s = 0.95", 9, anchor="start"))
    s.append(note(x0, base + 18, "round 1", 9, anchor="start", fill=SOFT))
    s.append(note(x1, base + 18, "round 8", 9, anchor="end", fill=SOFT))
    s.append(note(330, base + 38, "default: 8 rounds walk 0.95 down to 0.05", 10))
    s.append(note(330, base + 54, "gallery: 1 round, so the ladder is [0.95] and never decays", 10, fill=ACCENT))

    s.append(box(660, 428, 580, 252, "", None, fill="#ffffff"))
    s.append(note(690, 460, "SDS LOSS AND GRADIENT NORM", 9, anchor="start", fill=SOFT))
    gx0, gx1 = 780, 1190
    grad_base, loss_base, band = 540, 615, 60
    lpts, gpts = [], []
    for i, (step, loss, grad) in enumerate(CHECKPOINTS):
        px = gx0 + i * (gx1 - gx0) / 3
        lpts.append((px, loss_base - (loss + 700) / 900 * band))
        gpts.append((px, grad_base - (grad - 800) / 1600 * band))
        s.append(note(px, loss_base + 20, str(step), 9, fill=SOFT))
    s.append(f'<line x1="{gx0}" y1="{loss_base}" x2="{gx1}" y2="{loss_base}" '
             f'stroke="{RULE}" stroke-width="1"/>')
    s.append(note(770, grad_base - 26, "grad norm", 9, anchor="end", fill=ACCENT))
    s.append(note(770, loss_base - 26, "loss", 9, anchor="end", fill=INK))
    s.append('<polyline points="' + " ".join(f"{px:.1f},{py:.1f}" for px, py in lpts) +
             f'" fill="none" stroke="{INK}" stroke-width="1.6"/>')
    s.append('<polyline points="' + " ".join(f"{px:.1f},{py:.1f}" for px, py in gpts) +
             f'" fill="none" stroke="{ACCENT}" stroke-width="1.6" stroke-dasharray="5,3"/>')
    for (px, py), (_, loss, _) in zip(lpts, CHECKPOINTS):
        s.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.5" fill="{INK}"/>')
        s.append(note(px, py - 9, f"{loss:.0f}", 9, fill=INK))
    for (px, py), (_, _, grad) in zip(gpts, CHECKPOINTS):
        s.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3.5" fill="{ACCENT}"/>')
        s.append(note(px, py - 9, f"{grad:.0f}", 9, fill=ACCENT))
    s.append(note(950, 654, "loss swings sign while the image only improves", 10))
    s.append(note(950, 670, "SDS loss is not a quality signal, which is why a human gates", 10))

    s.append(note(640, 714, "defaults: 500 SDS steps + 8 Dream rounds x 300 steps per round · "
                            "gallery: 5000 SDS steps + 1 Dream round x 300 steps"))
    s.append(legend([(ACCENT_TINT, "optimizer reset"), ("#ffffff", "phase"),
                     (INK, "loss"), (ACCENT, "grad norm")], w, 750))
    s.append("</svg>")
    write(out, "two-phase", "\n".join(s), w, h)


def fig_dream(out):
    d_uri = photo_uri(SMOKE / "ckpt_sds_5000" / "derived_1.png", 340)
    z_uri = photo_uri(SMOKE_ARM / "ckpt_dream_round_01" / "target_1.png", 340)
    dp_uri = photo_uri(SMOKE_ARM / "ckpt_dream_round_01" / "derived_1.png", 340)
    w, h = 1280, 720
    s = [svg_open("dream", "One Dream Target round",
                  "A derived view is dreamed into a target and frozen for the round. SSIM "
                  "plus MSE pulls the view toward it. The next round dreams again.", w, h)]
    s.append(eyebrow(40, 44, "DREAM TARGET · ONE ROUND"))
    s.append(heading(40, 78, "Dream it, then match it"))
    for x1, x2 in [(230, 260), (490, 530), (720, 760), (960, 1000)]:
        s.append(arrow(x1, 290, x2, 290))
    s.append(photo(d_uri, 40, 200, 180, "d · entering Dream"))
    s.append(box(260, 210, 230, 160, "SDEdit(d, s, prompt)",
                 "DreamShaper LCM · CFG 2\ns = 0.95 this round"))
    s.append(photo(z_uri, 530, 200, 180, "z · frozen this round"))
    s.append(box(760, 210, 200, 160, "L = (1−SSIM) + MSE", "300 steps · fresh Adam\nlr 3e-3 here"))
    s.append(photo(dp_uri, 1000, 200, 180, "d′ · regressed to z"))
    s.append(path("M1090,460 V520 Q1090,528 1082,528 H383 Q375,528 375,520 V378",
                  ACCENT, "4,3"))
    s.append(arrow_label(740, 528, "NEXT ROUND DREAMS AGAIN", 220))
    s.append(box(40, 566, 1200, 52,
                 "default: 8 rounds, s walks 0.95 down to 0.05 · gallery: 1 round, so s stays 0.95 · "
                 "photos from the independent arm with the negative prompt on",
                 None, fill=NOTE_FILL, stroke=NOTE_STROKE, name_size=10))
    s.append(legend([(INPUT_FILL, "input"), (ACCENT_TINT, "loop back"),
                     ("#ffffff", "frozen op"), (NOTE_FILL, "note")], w, 668))
    s.append("</svg>")
    write(out, "dream", "\n".join(s), w, h)


def fig_joint(out):
    indep = SWAN / "arm_neg_off_indep" / "ckpt_dream_round_01"
    joint = SWAN / "arm_neg_off_joint" / "ckpt_dream_round_01"
    va = photo_uri(joint / "derived_1.png", 340)
    vb = photo_uri(joint / "derived_2.png", 340)
    pr = photo_uri(joint / "target_1.png", 340)
    pairs = [("independent targets", indep, 60), ("joint targets", joint, 420)]
    w, h = 760, 1340
    s = [svg_open("joint", "Joint Dream: two views, one consensus",
                  "Both flip views denoise together and reconcile to one consensus image in "
                  "pixel space. The two Dream targets become orientations of that image.", w, h)]
    s.append(eyebrow(40, 44, "JOINT DREAM · CONSENSUS"))
    s.append(heading(40, 78, "Two views, one image"))
    s.append(path("M290,330 V372 Q290,380 298,380 H352 Q360,380 360,388 V420"))
    s.append(path("M470,330 V372 Q470,380 462,380 H408 Q400,380 400,388 V420"))
    s.append(arrow(380, 500, 380, 540))
    s.append(arrow(380, 620, 380, 660))
    s.append(arrow(380, 760, 380, 806))
    s.append(photo(va, 170, 130, 180, "vA · upright"))
    s.append(photo(vb, 410, 130, 180, "vB · as rot180"))
    s.append(box(180, 420, 400, 80, "decode predicted x₀ · both views", None))
    s.append(box(180, 540, 400, 80, "rot₁₈₀(vB) into the upright frame", None))
    s.append(box(180, 660, 400, 100, "c = (xA + rot₁₈₀(xB)) / 2",
                 "xA and xB are the decoded x₀\npredictions · pixel space, not latent"))
    s.append(photo(pr, 290, 806, 180, "c · one consensus image"))
    s.append(note(380, 1022, "c becomes both Dream targets. The prime only follows later,", 10))
    s.append(note(380, 1038, "when 300 regression steps pull the views back onto them.", 10))
    for label, base, x in pairs:
        s.append(note(x + 125, 1078, label, 10, fill=ACCENT if "joint" in label else MUTED))
        s.append(f'<image href="{photo_uri(base / "target_1.png", 260)}" x="{x}" y="1092" '
                 f'width="115" height="115"/>')
        s.append(f'<image href="{photo_uri(base / "target_2.png", 260, rotate=180)}" x="{x + 135}" '
                 f'y="1092" width="115" height="115"/>')
        s.append(caption(x + 125, 1226, "target A · target B turned upright"))
    s.append(note(380, 1262, "same pair, same seed 11, one flag apart: independent targets disagree "
                             "about the shared pixels,", 10))
    s.append(note(380, 1278, "joint targets are one image seen two ways · "
                             "elephant and swan, sketch, Dream round 1", 10))
    s.append(legend([(INPUT_FILL, "input"), ("#ffffff", "frozen op"),
                     (NOTE_FILL, "evidence")], w, 1312))
    s.append("</svg>")
    write(out, "joint", "\n".join(s), w, h)


def fig_workflow(out):
    prime = photo_uri(STATIC / "elephant-swan-prime.webp", 300)
    view1 = photo_uri(STATIC / "elephant-swan-view.webp", 300)
    view2 = photo_uri(CLEAN / "s5-elephant_swan-seed11-oil-neg_off_joint-final-view2.png", 300)
    w, h = 1280, 720
    s = [svg_open("workflow", "Baking one keeper",
                  "A pair, two prompts, and a seed become flip views. Score Distillation, a "
                  "fresh Adam, and Dream Target produce the printable prime and both views.",
                  w, h)]
    s.append(eyebrow(40, 44, "OPTIMIZER · BAKE PIPELINE"))
    s.append(heading(40, 78, "Bake one keeper"))
    stages = [
        (40, 200, "input", "pair · 2 prompts\nseed · --type flip", INPUT_FILL, INPUT_STROKE),
        (250, 170, "FFN primes", "random θ\n256px here, 512px default", "#ffffff", INK),
        (450, 170, "flip views", "d₁ = p\nd₂ = rot₁₈₀(p)", "#ffffff", INK),
        (650, 190, "SDS steps", "500 default\n5000 here", "#ffffff", INK),
        (860, 150, "fresh Adam", "lr 1e-3 default\n3e-3 here", ACCENT_TINT, ACCENT),
        (1020, 200, "Dream rounds", "8 x 300 default\n1 x 300 here", "#ffffff", INK),
    ]
    for x, bw, name, sub, fill, stroke in stages:
        s.append(box(x, 190, bw, 130, name, sub, fill=fill, stroke=stroke))
    for x1, x2 in [(240, 250), (420, 450), (620, 650), (840, 860), (1010, 1020)]:
        s.append(arrow(x1, 255, x2, 255))
    s.append(path("M1220,320 V360 Q1220,368 1212,368 H438 Q430,368 430,376 V470 "
                  "Q430,478 438,478 H462"))
    s.append(arrow_label(900, 368, "OUTPUTS", 100))
    s.append(photo(prime, 470, 404, 150, "prime_1.png · print this"))
    s.append(photo(view1, 640, 404, 150, "derived_1.png · upright"))
    s.append(photo(view2, 810, 404, 150, "derived_2.png · turned"))
    s.append(box(990, 404, 250, 150, "d₁ and p look alike on purpose",
                 "a₁ is the identity, so the\nfirst view is the prime\nitself. "
                 "d₂ is the one\nyou have to earn.",
                 fill=NOTE_FILL, stroke=NOTE_STROKE))
    s.append(note(640, 606, "one cell = one pair + seed + mode · the gallery ran "
                            "--experimental-recipe author_reference"))
    s.append(legend([(INPUT_FILL, "input"), (ACCENT_TINT, "optimizer reset"),
                     ("#ffffff", "step"), (NOTE_FILL, "note")], w, 648))
    s.append("</svg>")
    write(out, "workflow", "\n".join(s), w, h)


RECIPE_ROWS = [
    ("prime network", "Linear MLP · 263,683 θ", "Conv 1x1 + BatchNorm · 199,683 θ"),
    ("prime resolution", "512 px", "256 px"),
    ("phase 1 optimizer", "Adam · lr 1e-3", "SGD · lr 1e-4"),
    ("phase 2 optimizer", "Adam · lr 1e-3", "Adam · lr 3e-3"),
    ("guidance G", "100", "60"),
    ("SDS objective", "legacy · no w(t)", "weighted_sds · grad scale 0.1"),
    ("budget", "500 SDS + 8 rounds x 300", "5000 SDS + 1 round x 300"),
    ("joint Dream", "off", "opt-in · 16 of the 26 keepers"),
]

VERDICTS = [
    ("256 px primes were enough", "512 px cost 3.3x for no gain"),
    ("one Dream round", "more rounds made images worse"),
    ("no negative prompt", "it did not lift the keeper rate"),
    ("oil style", "kept for colour, not for yield"),
]


def fig_recipe(out):
    w, h = 1280, 800
    s = [svg_open("recipe", "CLI defaults against the recipe that baked the gallery",
                  "Eight settings, the shipped default, and the research value. The gallery "
                  "ran author_reference, which changes the network, the optimizer, the "
                  "guidance, and the objective.", w, h)]
    s.append(eyebrow(40, 44, "TWO RECIPES · MEASURED VERDICTS"))
    s.append(heading(40, 78, "Different network, different budget"))
    cols = [(60, 300, "setting"), (380, 380, "CLI default"), (790, 420, "gallery recipe")]
    for x, cw, label in cols:
        accent = label == "gallery recipe"
        s.append(f'<rect x="{x}" y="130" width="{cw}" height="40" rx="4" '
                 f'fill="{ACCENT_TINT if accent else "rgba(45,49,66,0.04)"}"/>')
        s.append(f'<text x="{x + 14}" y="156" fill="{ACCENT if accent else MUTED}" font-size="11" '
                 f'font-family="{MONO}" letter-spacing="0.1em">{label.upper()}</text>')
    y = 194
    for i, (name, default, gallery) in enumerate(RECIPE_ROWS):
        if i % 2:
            s.append(f'<rect x="60" y="{y - 22}" width="1150" height="52" fill="rgba(45,49,66,0.02)"/>')
        s.append(f'<text x="74" y="{y + 6}" fill="{INK}" font-size="13" font-weight="600" '
                 f'font-family="{SANS}">{name}</text>')
        s.append(f'<text x="394" y="{y + 6}" fill="{MUTED}" font-size="12" '
                 f'font-family="{MONO}">{default}</text>')
        s.append(f'<text x="804" y="{y + 6}" fill="{INK}" font-size="12" '
                 f'font-family="{MONO}">{gallery}</text>')
        y += 52
    s.append(f'<line x1="60" y1="{y - 22}" x2="1210" y2="{y - 22}" stroke="{RULE}" stroke-width="0.8"/>')
    s.append(note(60, y + 6, "every photo on this page came from the right column: "
                             "--experimental-recipe author_reference swaps four things at once",
                  10, anchor="start"))
    cy = y + 30
    for i, (head, body) in enumerate(VERDICTS):
        s.append(box(60 + i * 295, cy, 265, 86, head, body, fill=NOTE_FILL, stroke=NOTE_STROKE))
    s.append(legend([("rgba(45,49,66,0.04)", "default"), (ACCENT_TINT, "gallery"),
                     (NOTE_FILL, "measured verdict")], w, cy + 128))
    s.append("</svg>")
    write(out, "recipe", "\n".join(s), w, h)


def fig_review(out):
    rejected = photo_uri(SWAN / "arm_neg_off_indep" / "ckpt_final" / "derived_1.png", 340)
    kept = photo_uri(STATIC / "elephant-swan-view.webp", 340)
    w, h = 1280, 780
    s = [svg_open("review", "Review funnel: a human is the gate",
                  "Two hundred and six baked cells reach a blind human review. Forty three "
                  "score four or five. Sixteen of those lose on a frame defect. Twenty six "
                  "become keepers.", w, h)]
    s.append(eyebrow(40, 44, "SELECTION · THE GATE"))
    s.append(heading(40, 78, "Human review is the gate"))
    s.append(box(40, 170, 230, 120, "206 cells baked", "98 bases x 4 Dream arms"))
    s.append(box(350, 170, 260, 120, "blind human review", "score 0 to 5 · frame flag",
                 fill=ACCENT_TINT, stroke=ACCENT))
    s.append(box(690, 170, 230, 120, "43 score 4 or 5", "21 fours · 22 fives"))
    s.append(box(1000, 170, 240, 120, "26 keepers", "window2-2026-08-clean"))
    for x1, x2 in [(270, 350), (610, 690), (920, 1000)]:
        s.append(arrow(x1, 230, x2, 230))
    s.append(path("M960,290 V318 Q960,326 968,326 H1010", MUTED, "4,3"))
    s.append(box(1020, 310, 220, 56, "16 cut on the frame flag", None,
                 fill=NOTE_FILL, stroke=NOTE_STROKE, name_size=11))
    s.append(path("M480,290 V326", MUTED, "4,3"))
    s.append(box(300, 336, 520, 56, "CLIP pair score: AUC 0.706, under the 0.75 bar · recorded, never a gate",
                 None, fill=NOTE_FILL, stroke=NOTE_STROKE, name_size=11))
    s.append(photo(rejected, 60, 420, 200, None))
    s.append(caption(160, 646, "cut · score 5 · frame disqualifying"))
    s.append(caption(160, 664, "elephant and swan · seed 11 · sketch · independent"))
    s.append(photo(kept, 330, 420, 200, None))
    s.append(caption(430, 646, "kept · score 5 · frame none"))
    s.append(caption(430, 664, "elephant and swan · seed 11 · oil · joint"))
    s.append(box(600, 420, 640, 200, "what a frame defect is",
                 "A printed border, a torn sheet edge, a desk, or a\n"
                 "signature that the model painted into the image.\n"
                 "The subject can read perfectly and the cell still goes:\n"
                 "the artifact tells you it is a photo of a drawing.\n"
                 "Same pair, same seed, both scored 5. Sixteen cells\n"
                 "at score 4 or 5 died on this flag alone.",
                 fill=NOTE_FILL, stroke=NOTE_STROKE, sub_size=11))
    s.append(note(640, 706, "keeper = one pair + seed + mode · a score alone never made the cut"))
    s.append(legend([(ACCENT_TINT, "the gate"), ("#ffffff", "count"),
                     (NOTE_FILL, "cut or side channel")], w, 748))
    s.append("</svg>")
    write(out, "review", "\n".join(s), w, h)


def fig_print(out):
    prime = photo_uri(STATIC / "elephant-swan-prime.webp", 300)
    view1 = photo_uri(STATIC / "elephant-swan-view.webp", 300)
    view2 = photo_uri(CLEAN / "s5-elephant_swan-seed11-oil-neg_off_joint-final-view2.png", 300)
    w, h = 1280, 720
    s = [svg_open("print", "Printing a flip illusion",
                  "Print the prime on plain paper, put it on a desk, and turn the sheet "
                  "180 degrees to see the second subject.", w, h)]
    s.append(eyebrow(40, 44, "FABRICATION · FLIP ONLY"))
    s.append(heading(40, 78, "Print it, turn it"))
    for x1, x2 in [(220, 250), (470, 510), (690, 740), (920, 960)]:
        s.append(arrow(x1, 300, x2, 300))
    s.append(photo(prime, 40, 210, 180, "prime_1.png · 256 px"))
    s.append(box(250, 220, 220, 160, "any laser printer", "plain paper\nno film needed"))
    s.append(photo(view1, 510, 210, 180, "the sheet on a desk"))
    s.append(box(740, 220, 180, 160, "turn the sheet", "180 degrees"))
    s.append(photo(view2, 960, 210, 180, "the second subject"))
    s.append(box(40, 480, 1200, 100, "other illusion types need more than paper",
                 "rotate and hidden overlays need transparency film and a backlight.\n"
                 "Flip is the only type that works on one plain sheet, which is why the gallery is flip.",
                 fill=NOTE_FILL, stroke=NOTE_STROKE, sub_size=11))
    s.append(legend([(INPUT_FILL, "input"), ("#ffffff", "step"), (NOTE_FILL, "note")], w, 648))
    s.append("</svg>")
    write(out, "print", "\n".join(s), w, h)


OIL = LOCAL / "campaigns/window2/runs/window2/a_forked_oil/elephant_swan"
SKETCH = LOCAL / "campaigns/window2/runs/window2/a_forked_reference_sketch/elephant_swan"
SEED_SCORES = {("joint", 11): 5, ("joint", 23): 3, ("joint", 37): 4,
               ("indep", 11): 0, ("indep", 23): 0, ("indep", 37): 3}


def fig_seeds(out):
    """Same pair, same prompts, same budget. Only the seed and the mode move."""
    w, h = 1280, 720
    s = [svg_open("seeds", "The seed decides as much as the recipe",
                  "One pair at three seeds, baked twice: joint Dream and independent "
                  "targets. Human scores run from zero to five with nothing else changed.",
                  w, h)]
    s.append(eyebrow(40, 44, "ABLATION · ONE PAIR, SIX CELLS"))
    s.append(heading(40, 78, "A keeper is a seed, not a recipe"))
    cols = [(11, 210), (23, 560), (37, 910)]
    rows = [("joint Dream", "arm_neg_off_joint", 150), ("independent targets", "arm_neg_off_indep", 400)]
    for seed, x in cols:
        s.append(note(x + 155, 138, f"seed {seed}", 11, fill=INK))
    for seed, x in cols:
        s.append(note(x + 155, 158, "upright · turned", 9, fill=SOFT))
    for label, arm, y in rows:
        s.append(f'<rect x="40" y="{y + 30}" width="150" height="125" rx="6" fill="{NOTE_FILL}"/>')
        s.append(note(115, y + 84, label.split()[0], 12, fill=INK))
        s.append(note(115, y + 102, label.split()[1], 12, fill=INK))
        for seed, x in cols:
            base = OIL / f"seed_{seed}" / "attempt_001" / arm / "ckpt_final"
            s.append(f'<image href="{photo_uri(base / "derived_1.png", 260)}" x="{x}" '
                     f'y="{y + 20}" width="145" height="145"/>')
            s.append(f'<image href="{photo_uri(base / "derived_2.png", 260)}" x="{x + 165}" '
                     f'y="{y + 20}" width="145" height="145"/>')
            score = SEED_SCORES[(arm.split("_")[-1], seed)]
            keeper = score >= 4
            s.append(f'<rect x="{x + 110}" y="{y + 178}" width="100" height="22" rx="11" '
                     f'fill="{ACCENT_TINT if keeper else NOTE_FILL}" '
                     f'stroke="{ACCENT if keeper else NOTE_STROKE}" stroke-width="1"/>')
            s.append(f'<text x="{x + 160}" y="{y + 193}" fill="{ACCENT if keeper else MUTED}" '
                     f'font-size="11" font-family="{MONO}" text-anchor="middle">score {score}</text>')
    s.append(box(40, 616, 1200, 52,
                 "elephant and swan · oil · no negative prompt · 5000 SDS steps + 1 Dream round · "
                 "only the seed and the Dream mode change",
                 None, fill=NOTE_FILL, stroke=NOTE_STROKE, name_size=11))
    s.append(legend([(ACCENT_TINT, "would be a keeper"), (NOTE_FILL, "cut")], w, 700))
    s.append("</svg>")
    write(out, "seeds", "\n".join(s), w, h)


FAILURES = [
    (OIL / "seed_37/attempt_001/arm_neg_on_joint/ckpt_final", "score 2 · the turn is confused",
     "Upright is a clean elephant.", "Turned, swan and elephant overlap.", "seed 37 · joint · oil"),
    (OIL / "seed_23/attempt_001/arm_neg_off_indep/ckpt_final", "score 0 · only the turn reads",
     "Upright resolves into nothing.", "Turned, the swan is clean.", "seed 23 · independent · oil"),
    (SKETCH / "seed_11/attempt_001/arm_neg_off_indep/ckpt_final", "score 5 · cut on the frame flag",
     "Both subjects read here.", "Pencils and a desk are painted in.", "seed 11 · independent · sketch"),
]


def fig_failures(out):
    """Three real cut cells, each with what the reviewer saw."""
    w, h = 1280, 656
    s = [svg_open("failures", "What the human gate catches",
                  "Three real cells that did not become keepers, each with both views and "
                  "the reason it was cut.", w, h)]
    s.append(eyebrow(40, 44, "FAILURE CASES · REAL CUT CELLS"))
    s.append(heading(40, 78, "What gets cut, and why"))
    for i, (base, verdict, line1, line2, prov) in enumerate(FAILURES):
        x = 40 + i * 410
        s.append(f'<image href="{photo_uri(base / "derived_1.png", 300)}" x="{x}" y="{170}" '
                 f'width="170" height="170"/>')
        s.append(f'<image href="{photo_uri(base / "derived_2.png", 300)}" x="{x + 185}" y="{170}" '
                 f'width="170" height="170"/>')
        s.append(caption(x + 85, 360, "upright"))
        s.append(caption(x + 270, 360, "turned"))
        s.append(f'<rect x="{x}" y="{388}" width="355" height="26" rx="4" fill="{NOTE_FILL}"/>')
        s.append(f'<text x="{x + 12}" y="{406}" fill="{INK}" font-size="12" font-weight="600" '
                 f'font-family="{SANS}">{verdict}</text>')
        s.append(f'<text x="{x}" y="{440}" fill="{MUTED}" font-size="11" '
                 f'font-family="{SANS}">{line1}</text>')
        s.append(f'<text x="{x}" y="{460}" fill="{MUTED}" font-size="11" '
                 f'font-family="{SANS}">{line2}</text>')
        s.append(f'<text x="{x}" y="{484}" fill="{SOFT}" font-size="9" '
                 f'font-family="{MONO}">{prov}</text>')
    s.append(box(40, 526, 1200, 92, "the score and the frame flag are two separate judgements",
                 "The score asks whether both subjects read. The frame flag asks whether the "
                 "image pretends to be\na photo of a drawing. A cell needs 4 or 5 on the score "
                 "and no disqualifying frame. The third cell\nscored 5 and was still cut.",
                 fill=NOTE_FILL, stroke=NOTE_STROKE, sub_size=11))
    s.append("</svg>")
    write(out, "failures", "\n".join(s), w, h)


SYMBOL_GROUPS = [
    ("the prime and its views", [
        ("p", "the printable prime: the one sheet you actually print",
         "RGB in [0,1] · (1,3,512,512), 256 px here", "FourierFeatureNetwork.image"),
        ("θ", "the prime network's weights, the only tensor that trains",
         "263,683 by default · 199,683 here", "optimize_illusion"),
        ("x, y", "the pixel coordinates the network reads, one pair per pixel",
         "512x512 in [-1,1] · 256x256 in [0,1) here", "FourierFeatureNetwork.image"),
        ("B", "fixed random projection. It decides which frequencies exist",
         "2 x 256, scale 10 · 2 x 128 here · never trained", "register_buffer(frequencies)"),
        ("v", "Fourier features: the coordinates wrapped into waves",
         "512-d by default · 256-d here", "v = [sin(2πBx) ‖ cos(2πBx)]"),
        ("σ", "sigmoid. It squashes the network output into printable RGB",
         "output in [0,1]", "torch.sigmoid(mlp(v))"),
        ("a₁, a₂", "the two arrangements: leave the sheet, and turn it 180 degrees",
         "fixed, differentiable, physically real", "ILLUSIONS[flip], _flip"),
        ("d₁, d₂", "the derived views: what a person sees before and after the turn",
         "d₁ = a₁(p) = p · d₂ = rot₁₈₀(p)", "_flip"),
    ]),
    ("one Score Distillation step", [
        ("z", "the derived view encoded into the diffusion latent space",
         "(1,4,64,64)", "DiffusionAdapter.encode_latent"),
        ("t", "the timestep drawn fresh each step, shared by both views",
         "integer, 2% to 98% of T = 1000", "randint(0.02T, 0.98T)"),
        ("ε", "the noise actually added to the latent this step",
         "same shape as z, standard normal", "add_training_noise"),
        ("z_t", "the latent after that noise: the only thing the UNet is shown",
         "(1,4,64,64)", "z_t = sched(z, ε, t)"),
        ("εc, εu", "what the frozen UNet predicts with the prompt, and without it",
         "one CFG-doubled forward returns both", "sds_loss_batch"),
        ("G", "guidance scale: how hard the prompt pulls the prediction",
         "100 by default · 60 here", "--sds-guidance"),
        ("εcfg", "the guided prediction the two are mixed into",
         "εu + G·(εc − εu)", "compute_sds_gradient"),
        ("ᾱt", "the scheduler's cumulative alpha at step t",
         "0.9991 at t = 0 down to 0.0047 at t = 999", "alphas_cumprod[t]"),
        ("w(t)", "the timestep weight. It is not the guidance scale",
         "1 − ᾱt, so 0.0009 to 0.9953", "sds_timestep_weight"),
        ("r", "the residual, used directly as the gradient on the latent",
         "detached · scaled by 0.1 here", "compute_sds_gradient"),
        ("L", "the surrogate loss whose gradient is exactly r",
         "(z · r.detach()).sum()", "_sds_loss_from_latent"),
        ("η", "the step the optimizer takes on θ, and on nothing else",
         "Adam 1e-3 by default · SGD 1e-4 here", "resolve_learning_rates"),
    ]),
    ("Dream Target and joint Dream", [
        ("s", "SDEdit strength: how far the target may drift from the view",
         "0.95 to 0.05 over 8 rounds · [0.95] here", "strength_schedule"),
        ("z (Dream)", "the dreamed target, frozen for the whole round",
         "an image, not a latent", "DiffusionAdapter.sdedit"),
        ("L (Dream)", "the round loss that pulls each view onto its target",
         "(1 − SSIM) + MSE, 300 steps", "image_similarity_loss"),
        ("xA, xB", "the decoded x₀ predictions during one joint denoise step",
         "pixel space, not latent", "DiffusionAdapter.sdedit_joint"),
        ("c", "the consensus image that both Dream targets are made from",
         "(xA + rot₁₈₀(xB)) / 2", "reconcile_flip"),
    ]),
]


def fig_symbols(out):
    """Every symbol the other figures use, with its value and its source."""
    w, h = 1280, 840
    s = [svg_open("symbols", "Every symbol on this page",
                  "A reference table: what each symbol means, the value or shape it "
                  "carries, and the function that computes it.", w, h)]
    s.append(eyebrow(40, 44, "REFERENCE · READ THE OTHER FIGURES WITH THIS"))
    s.append(heading(40, 78, "What each symbol means"))
    heads = [(44, "SYMBOL"), (168, "WHAT IT IS"), (658, "RANGE, SHAPE OR VALUE"),
             (962, "WHERE IT IS COMPUTED")]
    for x, label in heads:
        s.append(f'<text x="{x}" y="112" fill="{SOFT}" font-size="9" font-family="{MONO}" '
                 f'letter-spacing="0.12em">{label}</text>')
    s.append(f'<line x1="40" y1="120" x2="1240" y2="120" stroke="{RULE}" stroke-width="0.8"/>')
    y = 142
    for title, rows in SYMBOL_GROUPS:
        s.append(f'<text x="44" y="{y}" fill="{ACCENT}" font-size="11" font-family="{MONO}" '
                 f'letter-spacing="0.1em">{title.upper()}</text>')
        y += 20
        for i, (sym, meaning, value, source) in enumerate(rows):
            if i % 2:
                s.append(f'<rect x="40" y="{y - 15}" width="1200" height="22" '
                         f'fill="rgba(45,49,66,0.02)"/>')
            s.append(f'<text x="44" y="{y}" fill="{INK}" font-size="12" font-weight="600" '
                     f'font-family="{MONO}">{sym}</text>')
            s.append(f'<text x="168" y="{y}" fill="{MUTED}" font-size="11" '
                     f'font-family="{SANS}">{meaning}</text>')
            s.append(f'<text x="658" y="{y}" fill="{INK}" font-size="10" '
                     f'font-family="{MONO}">{value}</text>')
            s.append(f'<text x="962" y="{y}" fill="{SOFT}" font-size="10" '
                     f'font-family="{MONO}">{source}</text>')
            y += 22
        y += 18
    s.append(f'<line x1="40" y1="{y - 22}" x2="1240" y2="{y - 22}" stroke="{RULE}" stroke-width="0.8"/>')
    s.append(note(44, y, "Function names are worker/worker/illusions.py on PR #118. "
                         "\"here\" means the recipe that baked this gallery.", 10, anchor="start"))
    s.append("</svg>")
    write(out, "symbols", "\n".join(s), w, h)


FIGURES = {
    "architecture": fig_architecture,
    "ffn": fig_ffn,
    "sds": fig_sds,
    "symbols": fig_symbols,
    "two-phase": fig_two_phase,
    "dream": fig_dream,
    "joint": fig_joint,
    "workflow": fig_workflow,
    "recipe": fig_recipe,
    "review": fig_review,
    "seeds": fig_seeds,
    "failures": fig_failures,
    "print": fig_print,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=STATIC)
    parser.add_argument("--only", nargs="*", choices=sorted(FIGURES), default=None)
    parser.add_argument("--keep-png", action="store_true",
                        help="leave the intermediate png and html next to each webp")
    args = parser.parse_args()
    global KEEP_PNG
    KEEP_PNG = args.keep_png
    if not LOCAL.exists():
        sys.exit(f"research exports missing: {LOCAL}")
    for name in args.only or FIGURES:
        FIGURES[name](args.out)


if __name__ == "__main__":
    main()
