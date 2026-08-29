"""Generate a 1280x640 social-preview PNG for the context-m repo.

Layout:
  - dark background (#0A0E1A — deep navy/black)
  - top-left: "cortexm" logo-style title + tagline
  - bottom-left: 0.948 LongMemEval callout
  - bottom-right: pip install cortexm
  - right side: ASCII-art memory hash chain (visual hook)
  - border: subtle 1px accent line

Uses Pillow + Noto Sans / DejaVu Sans (already installed system fonts).
"""
from __future__ import annotations
from PIL import Image, ImageDraw, ImageFont

OUT = "/home/z/my-project/download/cortexm_social_preview.png"

W, H = 1280, 640
BG = (10, 14, 26)         # #0A0E1A — deep navy
FG = (235, 240, 250)      # near-white
ACCENT = (96, 165, 250)   # #60A5FA — bright blue
GREEN = (74, 222, 128)     # #4ADE80 — for the 0.948 callout
MUTED = (130, 140, 160)   # gray for secondary text


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    paths = []
    if bold:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
    else:
        paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # --- top border accent
    draw.rectangle([0, 0, W, 4], fill=ACCENT)
    draw.rectangle([0, H - 4, W, H], fill=ACCENT)

    # --- logo title (top-left)
    title_font = _font(96, bold=True)
    tag_font = _font(36, bold=False)
    install_font = _font(42, bold=True)
    metric_font = _font(72, bold=True)
    metric_label_font = _font(28, bold=False)
    code_font = _font(28, bold=False)

    # title
    draw.text((80, 110), "cortexm", font=title_font, fill=FG)

    # tagline below title
    draw.text((80, 230), "Deterministic agent memory. μ=0. Free, local, forever.",
              font=tag_font, fill=MUTED)
    draw.text((80, 280), "Same result every time.",
              font=tag_font, fill=MUTED)

    # --- install command (bottom-left)
    # Draw as a "code block" with a subtle background rectangle
    cmd = "pip install cortexm"
    box_x, box_y = 80, 430
    box_w, box_h = 520, 70
    draw.rectangle([box_x - 12, box_y - 12, box_x + box_w, box_y + box_h],
                   fill=(20, 26, 40))
    # accent left border on the code block
    draw.rectangle([box_x - 12, box_y - 12, box_x - 6, box_y + box_h], fill=ACCENT)
    draw.text((box_x, box_y + 8), f"$ {cmd}", font=install_font, fill=GREEN)

    # --- right side: ASCII hash chain (the μ=0 visual hook)
    chain_x = 820
    chain_y = 130
    chain_lines = [
        "00:00 → add()",
        "  source text",
        "  ↓ BLAKE3",
        "  3f2a91c2…",
        "  ↓ pattern",
        "  (Alice,",
        "   works_at,",
        "   Google)",
        "  ↓ VSA bind",
        "  hologram",
        "  ↓ retrievable",
        "  ✓ proven",
    ]
    for i, line in enumerate(chain_lines):
        color = ACCENT if line.strip().startswith(("↓", "✓", "00:")) else FG
        draw.text((chain_x, chain_y + i * 28), line, font=code_font, fill=color)

    # --- bottom-right: 0.948 callout
    metric_x = 820
    metric_y = 460
    draw.text((metric_x, metric_y), "0.948", font=metric_font, fill=GREEN)
    draw.text((metric_x, metric_y + 80), "canonical LongMemEval · 154 Q · μ=0 · $0",
              font=metric_label_font, fill=MUTED)

    # tiny attribution bottom-left
    draw.text((80, 580), "github.com/ssmurfgg04-gif/context-m",
              font=_font(20), fill=MUTED)

    img.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT} ({W}x{H})")
    # Print size for verification
    import os
    print(f"size: {os.path.getsize(OUT)} bytes")


if __name__ == "__main__":
    main()
