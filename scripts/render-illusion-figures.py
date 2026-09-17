"""Build the six /illusions figures as drawio sources, then export + composite.

Each figure is drawio boxes/arrows/math (script-generated XML) with dashed
photo wells that PIL fills with real keeper photos after export. Headless
drawio cannot load images, hence the two steps.

Usage (from repo root):
  python3 scripts/render-illusion-figures.py --out /tmp/ill/draw
  # exports PNGs with drawio, composites photos, writes <name>.png + <name>.drawio

Photo wells read from frontend/static/illusions (committed) and from the
gitignored research exports (.local/illusion-reliability/...). Missing
photos leave dashed wells: structure still renders, photos need the
research checkout.

Figure sources of truth:
  architecture/ffn/sds/joint .. elephant-swan keeper (seed 11, oil, joint)
  two-phase/dream ............ window-2 giraffe-penguin calibration smoke run
"""

import argparse
import html
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static" / "illusions"
CLEAN = ROOT / ".local" / "illusion-reliability" / "keepers" / "window2-2026-08-clean"
SMOKE = ROOT / ".local" / "illusion-reliability" / "campaigns" / "window2" / "smoke"
SMOKE_ARM = SMOKE / "arm_neg_on_indep"

FONT = "DejaVu Sans"

C_TRAIN = ("#dbeafe", "#2563eb", "#1e3a8a")
C_FROZEN = ("#ffffff", "#64748b", "#0f172a")
C_INPUT = ("#dcfce7", "#16a34a", "#14532d")
C_MID = ("#fee2e2", "#dc2626", "#7f1d1d")
C_NOTE = ("#fffbeb", "#d97706", "#78350f")
C_EDGE = "#334155"
C_GRAY = "#64748b"


def esc(text):
    return html.escape(text, quote=True).replace("\n", "&#10;")


class Figure:
    def __init__(self, name, width, height):
        self.name = name
        self.width = width
        self.height = height
        self.cells = []
        self.wells = []
        self._next = 2

    def _id(self):
        self._next += 1
        return str(self._next)

    def rect(self, x, y, w, h, text="", colors=C_FROZEN, size=15, rounded=1, dashed=0, bold=0):
        fill, edge, tcolor = colors
        style = (
            f"rounded={rounded};whiteSpace=wrap;html=1;fillColor={fill};"
            f"strokeColor={edge};fontColor={tcolor};fontSize={size};"
            f"fontFamily={FONT};"
        )
        if dashed:
            style += "dashed=1;dashPattern=5 4;"
        if bold:
            style += "fontStyle=1;"
        cid = self._id()
        self.cells.append(
            f'<mxCell id="{cid}" value="{esc(text)}" style="{style}" '
            f'vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" '
            f'height="{h}" as="geometry"/></mxCell>'
        )
        return cid

    def text(self, x, y, w, h, text, size=15, color="#0f172a", bold=0, align="center"):
        style = (
            "text;html=1;whiteSpace=wrap;fillColor=none;strokeColor=none;"
            f"fontColor={color};fontSize={size};fontFamily={FONT};"
            f"align={align};verticalAlign=middle;"
        )
        if bold:
            style += "fontStyle=1;"
        cid = self._id()
        self.cells.append(
            f'<mxCell id="{cid}" value="{esc(text)}" style="{style}" '
            f'vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" '
            f'height="{h}" as="geometry"/></mxCell>'
        )
        return cid

    def photo(self, x, y, w, h, path, caption=None, cap_colors=C_MID):
        self.wells.append({"x": x, "y": y, "w": w, "h": h, "path": str(path)})
        self.rect(x, y, w, h, "photo", colors=("#ffffff", "#94a3b8", "#94a3b8"), size=11, dashed=1)
        if caption:
            self.rect(x, y + h + 8, w, 34, caption, colors=cap_colors, size=13)

    def edge(self, pts, dashed=0, color=C_EDGE, width=2):
        style = f"endArrow=classic;html=1;strokeColor={color};strokeWidth={width};rounded=1;"
        if dashed:
            style += "dashed=1;dashPattern=5 4;"
        cid = self._id()
        geo = (
            f'<mxGeometry relative="1" as="geometry">'
            f'<mxPoint x="{pts[0][0]}" y="{pts[0][1]}" as="sourcePoint"/>'
            f'<mxPoint x="{pts[-1][0]}" y="{pts[-1][1]}" as="targetPoint"/>'
        )
        for x, y in pts[1:-1]:
            geo += f'<mxPoint x="{x}" y="{y}" as="Array"/>'
        geo += "</mxGeometry>"
        self.cells.append(
            f'<mxCell id="{cid}" value="" style="{style}" edge="1" parent="1">{geo}</mxCell>'
        )
        return cid

    def tag(self, text):
        self.text(30, 8, 900, 26, text, size=13, color=C_GRAY, align="left")

    def legend(self, y=0):
        y = y or self.height - 62
        items = [
            ("■ input", C_INPUT),
            ("■ trainable", C_TRAIN),
            ("■ frozen", C_FROZEN),
            ("■ intermediate", C_MID),
        ]
        w = 250
        x = 60
        for label, colors in items:
            self.rect(x, y, w, 44, label, colors=colors, size=14)
            x += w + 30

    def save_drawio(self, path):
        bg = (
            f'<mxCell id="bg" value="" style="rounded=0;fillColor=#ffffff;'
            f'strokeColor=#ffffff;" vertex="1" parent="1">'
            f'<mxGeometry x="0" y="0" width="{self.width}" height="{self.height}" '
            f'as="geometry"/></mxCell>'
        )
        body = "\n".join([bg] + self.cells)
        xml = (
            f'<mxfile><diagram name="{self.name}" id="{self.name}">'
            f'<mxGraphModel dx="0" dy="0" grid="0" page="1" pageScale="1" '
            f'pageWidth="{self.width}" pageHeight="{self.height}">'
            f'<root><mxCell id="0"/><mxCell id="1" parent="0"/>\n{body}\n'
            f"</root></mxGraphModel></diagram></mxfile>"
        )
        Path(path).write_text(xml, encoding="utf-8")

    def composite(self, png_in, png_out):
        from PIL import Image

        base = Image.open(png_in).convert("RGB")
        # drawio -s 1 export adds a 2px border around the page: undo it.
        offx = (base.size[0] - self.width) // 2
        offy = (base.size[1] - self.height) // 2
        scale = (base.size[0] - 2 * offx) / self.width
        for well in self.wells:
            src = Path(well["path"])
            if not src.is_file():
                print(f"  warn: missing photo {src}, well left dashed")
                continue
            img = Image.open(src).convert("RGB")
            tw, th = round(well["w"] * scale) + 2, round(well["h"] * scale) + 2
            img = img.resize((tw, th))
            base.paste(
                img, (round(offx + well["x"] * scale) - 1, round(offy + well["y"] * scale) - 1)
            )
        base.save(png_out)


HERO_PRIME = STATIC / "elephant-swan-prime.webp"
HERO_VIEW = STATIC / "elephant-swan-view.webp"
HERO_VIEW2 = CLEAN / "s5-elephant_swan-seed11-oil-neg_off_joint-final-view2.png"


def fig_architecture():
    f = Figure("architecture", 1400, 780)
    f.tag("running example · elephant–swan keeper, seed 11, oil, joint")
    f.photo(60, 120, 190, 190, HERO_PRIME, "p · print this", C_INPUT)
    f.rect(320, 140, 190, 110, "a₁(p) = p")
    f.rect(320, 330, 190, 110, "a₂(p) = rot₁₈₀(p)")
    f.photo(580, 90, 190, 190, HERO_VIEW, "d₁ · upright")
    f.photo(580, 340, 190, 190, HERO_VIEW2, "d₂ · inverted")
    f.rect(840, 90, 250, 150, "SDS\nr = w·(ε̂ − ε)\nUNet frozen · CFG 100")
    f.rect(840, 340, 250, 150, "Dream Target\nz = SDEdit(d, s)\nL = (1−SSIM) + MSE")
    f.rect(1150, 200, 220, 110, "θ · FFN weights\nonly thing trained", colors=C_TRAIN)
    f.edge([(250, 215), (320, 195)])
    f.edge([(250, 215), (320, 385)])
    f.edge([(510, 195), (580, 185)])
    f.edge([(510, 385), (580, 435)])
    f.edge([(770, 165), (840, 150)])
    f.edge([(770, 420), (840, 190)])
    f.edge([(770, 200), (840, 400)])
    f.edge([(770, 450), (840, 430)])
    f.edge([(1090, 165), (1150, 230)], dashed=1, color=C_TRAIN[1])
    f.edge([(1090, 415), (1150, 265)], dashed=1, color=C_TRAIN[1])
    f.text(140, 600, 1120, 36, "d₁ = a₁(p) = p · d₂ = a₂(p) = rot₁₈₀(p) · gradients reach θ only")
    f.legend(660)
    return f


def fig_ffn():
    f = Figure("ffn", 1400, 470)
    f.tag("one prime = one network · ≈264k weights")
    f.rect(40, 170, 110, 90, "(x, y)", colors=C_INPUT)
    f.rect(180, 160, 210, 110, "B ∼ N(0, 10²)\n2 × 256 · fixed")
    f.rect(420, 160, 170, 110, "sin + cos\n512-d feats")
    f.rect(620, 140, 300, 150, "MLP 512→256→256→256→3\nReLU ×3 · θ trained", colors=C_TRAIN)
    f.rect(950, 160, 150, 110, "σ · sigmoid\nRGB (1,3,512,512)")
    f.photo(1130, 120, 190, 190, HERO_PRIME, "printable prime", C_INPUT)
    for a, b in [(150, 180), (390, 420), (590, 620), (920, 950), (1100, 1130)]:
        f.edge([(a, 215), (b, 215)])
    f.text(140, 390, 1120, 40, "v = [ sin(2πBx) ‖ cos(2πBx) ] · RGB = σ(MLP(v))")
    return f


def fig_sds():
    f = Figure("sds", 1400, 660)
    f.tag("one SDS step · sds_loss_batch · all views share one UNet forward")
    f.photo(40, 150, 160, 160, HERO_VIEW, "d · derived view")
    f.rect(240, 160, 160, 140, "VAE enc\nz: (1,4,64,64)")
    f.rect(440, 160, 170, 140, "+ ε\nt ∈ 0.02…0.98")
    f.rect(650, 130, 250, 200, "UNet · CFG-doubled ×1\nG = 100")
    f.rect(700, 112, 150, 30, "FROZEN", colors=C_NOTE, size=13, bold=1)
    f.rect(940, 160, 180, 140, "r = w·(ε̂c − ε̂u)")
    f.rect(1160, 160, 200, 140, "θ −= η·∇θ\n(z·r̄).sum()", colors=C_TRAIN)
    for a, b in [(200, 240), (400, 440), (610, 650), (900, 940), (1120, 1160)]:
        f.edge([(a, 230), (b, 230)])
    f.text(
        140, 420, 1120, 36, "z_t = sched(z, ε, t) · ε̂ = UNet(z_t, t, prompt) · no grad through UNet"
    )
    f.text(60, 478, 1280, 30, "symbols", size=14, bold=1, align="left")
    left = (
        "ε · noise sampled from N(0, I)\n"
        "ε̂c / ε̂u · UNet guess, with / without prompt\n"
        "w · guidance weight (G = 100)\n"
        "r · guided residual, used as the gradient"
    )
    right = (
        "z · view as VAE latent (1, 4, 64, 64)\n"
        "z_t · latent noised to step t by the schedule\n"
        "r̄ · r detached: no gradient into the UNet\n"
        "η · Adam step on θ only (lr 1e-3)"
    )
    f.text(60, 512, 620, 130, left, size=14, align="left")
    f.text(720, 512, 620, 130, right, size=14, align="left")
    return f


def _smoke_derived(step):
    return SMOKE / f"ckpt_sds_{step:04d}" / "derived_1.png"


def fig_two_phase():
    f = Figure("two-phase", 1400, 600)
    f.tag("real checkpoints · giraffe–penguin calibration smoke run")
    f.rect(60, 80, 640, 60, "Phase 1 · SDS · frozen SD 1.5")
    f.rect(730, 80, 130, 60, "↻ fresh\nAdam", colors=C_TRAIN)
    f.rect(890, 80, 450, 60, "Phase 2 · Dream Target · DreamShaper LCM")
    imgs = [
        (_smoke_derived(250), "SDS 250"),
        (_smoke_derived(1000), "SDS 1000"),
        (_smoke_derived(2500), "SDS 2500"),
        (_smoke_derived(5000), "SDS 5000"),
        (SMOKE_ARM / "ckpt_dream_round_01" / "derived_1.png", "Dream r1"),
        (SMOKE_ARM / "ckpt_final" / "derived_1.png", "final"),
    ]
    x = 60
    for path, label in imgs:
        f.photo(x, 180, 180, 180, path)
        f.text(x, 368, 180, 52, label, size=14)
        x += 220
    f.text(
        140,
        470,
        1120,
        40,
        "defaults 500 + 8×300 · gallery recipe 5000 + 1×300 · strength 0.95 → 0.05",
    )
    return f


def fig_dream():
    f = Figure("dream", 1400, 620)
    f.tag("one Dream Target round · real round-1 triple, smoke run")
    f.photo(60, 150, 200, 200, _smoke_derived(5000), "d · entering Dream")
    f.rect(310, 160, 250, 180, "SDEdit(d, s, prompt)\nLCM · CFG 2\ns = 0.95 → 0.05")
    f.photo(
        610,
        150,
        200,
        200,
        SMOKE_ARM / "ckpt_dream_round_01" / "target_1.png",
        "z · frozen this round",
    )
    f.rect(860, 160, 220, 180, "L = (1−SSIM) + MSE\n300 steps")
    f.photo(
        1130,
        150,
        200,
        200,
        SMOKE_ARM / "ckpt_dream_round_01" / "derived_1.png",
        "d′ · regressed to z",
    )
    for a, b in [(260, 310), (560, 610), (810, 860), (1080, 1130)]:
        f.edge([(a, 250), (b, 250)])
    f.edge([(1230, 350), (1230, 500), (435, 500), (435, 340)], dashed=1)
    f.text(560, 486, 560, 36, "next round re-dreams from d′", size=14)
    f.text(140, 540, 1120, 36, "8 rounds default · the gallery used 1")
    return f


def fig_joint():
    f = Figure("joint", 1000, 1400)
    f.tag("joint Dream · elephant–swan keeper (joint mode)")
    f.photo(210, 70, 220, 220, HERO_VIEW, "vA · upright")
    f.photo(570, 70, 220, 220, HERO_VIEW2, "vB · as rot₁₈₀")
    f.rect(210, 360, 580, 80, "decode predicted x₀ · both views")
    f.rect(210, 470, 580, 80, "rot₁₈₀(vB prediction) → upright frame")
    f.rect(210, 580, 580, 100, "c = (x̂A + rot₁₈₀(x̂B)) / 2\npixel space, not latent")
    f.photo(390, 730, 220, 220, HERO_PRIME, "consensus lives in the prime", C_INPUT)
    f.photo(210, 1040, 220, 220, HERO_VIEW, "target A")
    f.photo(570, 1040, 220, 220, HERO_VIEW2, "target B · as rot₁₈₀")
    f.edge([(320, 336), (400, 360)])
    f.edge([(680, 336), (600, 360)])
    f.edge([(500, 440), (500, 470)])
    f.edge([(500, 550), (500, 580)])
    f.edge([(500, 680), (500, 730)])
    f.edge([(500, 950), (320, 1040)])
    f.edge([(500, 950), (680, 1040)])
    f.rect(
        140,
        1310,
        720,
        56,
        "VAE does not commute with rot₁₈₀ · latent error 0.78–0.97",
        colors=C_NOTE,
        size=14,
    )
    return f


FIGURES = {
    "architecture": fig_architecture,
    "ffn": fig_ffn,
    "sds": fig_sds,
    "two-phase": fig_two_phase,
    "dream": fig_dream,
    "joint": fig_joint,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output dir for .drawio + .png")
    ap.add_argument("--drawio", default="drawio")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, build in FIGURES.items():
        fig = build()
        src = out / f"{name}.drawio"
        raw = out / f"{name}-raw.png"
        final = out / f"{name}.png"
        fig.save_drawio(src)
        subprocess.run(
            [args.drawio, "-x", "-f", "png", "-s", "1", "-o", str(raw), str(src)], check=True
        )
        fig.composite(raw, final)
        print(f"{name}: {final}")


if __name__ == "__main__":
    main()
