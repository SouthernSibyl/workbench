"""生成应用图标（256/128/64/48/32/16 多尺寸 ICO）。构建期使用，不进成品。"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icon.ico"
SS = 4  # 超采样倍数


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def render(size: int) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = int(s * 0.055)
    rounded(d, (pad, pad, s - pad, s - pad), int(s * 0.22), (47, 53, 64, 255))

    # 2x2 色块，代表四个面板
    inner = int(s * 0.18)
    gap = int(s * 0.075)
    cell = (s - inner * 2 - gap) // 2
    colors = [
        (232, 176, 75, 255),    # 琥珀
        (242, 240, 235, 255),   # 米白
        (242, 240, 235, 255),   # 米白
        (127, 168, 137, 255),   # 苔绿
    ]
    r = int(cell * 0.24)
    positions = [
        (inner, inner),
        (inner + cell + gap, inner),
        (inner, inner + cell + gap),
        (inner + cell + gap, inner + cell + gap),
    ]
    for (x, y), c in zip(positions, colors):
        rounded(d, (x, y, x + cell, y + cell), r, c)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    sizes = [256, 128, 64, 48, 32, 16]
    base = render(256)
    base.save(OUT, format="ICO", sizes=[(x, x) for x in sizes])
    print("wrote", OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
