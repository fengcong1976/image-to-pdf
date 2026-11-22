"""生成程序图标（resources/app.ico / app.png）。

图标语义：蓝色圆角底 + 一张纸 + 纸上的照片（山与太阳）+ 右下角 PDF 红标。
32px 下仍能认出「纸 + 彩色图」两件事，不会糊成一团色块。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "resources"
SIZE = 512


def rounded_gradient(size: int) -> Image.Image:
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGB", (1, size))
    px = grad.load()
    top, bottom = (58, 124, 199), (21, 62, 120)
    for y in range(size):
        t = y / (size - 1)
        px[0, y] = (int(top[0] + (bottom[0] - top[0]) * t),
                    int(top[1] + (bottom[1] - top[1]) * t),
                    int(top[2] + (bottom[2] - top[2]) * t))
    grad = grad.resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.22),
                                           fill=255)
    base.paste(grad, (0, 0), mask)
    return base


def build() -> Image.Image:
    im = rounded_gradient(SIZE)
    d = ImageDraw.Draw(im)
    s = SIZE

    # 纸：略向右下，带轻微投影
    paper_box = (int(s * 0.20), int(s * 0.14), int(s * 0.74), int(s * 0.86))
    d.rounded_rectangle((paper_box[0] + 10, paper_box[1] + 14, paper_box[2] + 10, paper_box[3] + 14),
                        radius=14, fill=(12, 32, 60, 90))
    d.rounded_rectangle(paper_box, radius=14, fill=(252, 252, 250))

    # 纸上的照片框：天空 + 山 + 太阳
    ph = (paper_box[0] + 26, paper_box[1] + 30, paper_box[2] - 26, paper_box[1] + 200)
    d.rounded_rectangle(ph, radius=8, fill=(206, 231, 250))
    d.ellipse((ph[2] - 62, ph[1] + 18, ph[2] - 22, ph[1] + 58), fill=(247, 190, 70))
    d.polygon([(ph[0], ph[3]), (ph[0] + (ph[2] - ph[0]) * 0.38, ph[1] + (ph[3] - ph[1]) * 0.42),
               (ph[0] + (ph[2] - ph[0]) * 0.62, ph[3])], fill=(76, 145, 108))
    d.polygon([(ph[0] + (ph[2] - ph[0]) * 0.34, ph[3]),
               (ph[0] + (ph[2] - ph[0]) * 0.66, ph[1] + (ph[3] - ph[1]) * 0.34),
               (ph[2], ph[3])], fill=(56, 118, 90))

    # 正文行
    y = ph[3] + 26
    for i, w in enumerate((0.86, 0.7, 0.78)):
        d.rounded_rectangle((paper_box[0] + 26, y, paper_box[0] + 26 + int((paper_box[2] - paper_box[0] - 52) * w),
                             y + 14), radius=7, fill=(196, 200, 205))
        y += 30

    # PDF 红标
    tag = (int(s * 0.56), int(s * 0.64), int(s * 0.94), int(s * 0.86))
    d.rounded_rectangle((tag[0] + 6, tag[1] + 8, tag[2] + 6, tag[3] + 8), radius=18, fill=(12, 32, 60, 90))
    d.rounded_rectangle(tag, radius=18, fill=(203, 56, 48))
    # 标里写 PDF：两根白竖条看着像「暂停」，一眼读不出这是什么
    font = _font(int((tag[3] - tag[1]) * 0.62))
    text = "PDF"
    box = d.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text(((tag[0] + tag[2] - tw) / 2 - box[0], (tag[1] + tag[3] - th) / 2 - box[1]),
           text, font=font, fill=(255, 255, 255))
    return im


def _font(size: int):
    """优先用系统里的粗体，取不到就退回默认位图字体（小一点但不至于报错）。"""
    for name in ("arialbd.ttf", "seguisb.ttf", "msyhbd.ttc", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    RES.mkdir(parents=True, exist_ok=True)
    im = build()
    png = RES / "app.png"
    ico = RES / "app.ico"
    im.save(png)
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    im.save(ico, sizes=sizes)
    print(f"已生成 {png} 与 {ico}（{len(sizes)} 个尺寸）")


if __name__ == "__main__":
    main()
