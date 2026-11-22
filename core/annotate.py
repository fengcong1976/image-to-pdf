"""页面标注：文字块、方框、箭头、涂黑遮盖、签名图章。

这些是「除了图片之外用户往纸上加的东西」。五种东西共用一个对象：
kind + 所在页 + 页面上的矩形（0–1 比例）+ 样式。位置存比例而不是 pt，
换纸张、改边距、改拼版之后标注仍在相对位置——不然每动一个参数都要重摆一遍。

绘制有三个出口，读的是同一份 dict：
- PDF（PyMuPDF）：`draw_pdf`
- 整页预览（PIL）：`draw_pil`，供命令行、自检和翻页缓存
- 画布（Qt）：在 main.SheetView 里，几何换算仍调这里的 `to_pt`

为什么不做自由手绘：鼠标写字难看，还要处理撤销栈、缩放后的线宽失真、
路径落进 PDF 之后无法再选中编辑。同样想标一个地方，方框和箭头可拖、可改、
可删，矢量进 PDF，成本还低一个数量级。
"""
from __future__ import annotations

import os
from pathlib import Path

KINDS = {
    "text": "文字",
    "box": "方框",
    "arrow": "箭头",
    "redact": "涂黑",
    "stamp": "签名图",
}

# 界面上给的一组颜色；存的是 #rrggbb，PDF / PIL / Qt 各自转一次
COLORS = [
    ("黑", "#000000"),
    ("红", "#d0021b"),
    ("蓝", "#1b58c8"),
    ("白", "#ffffff"),
    ("黄", "#f2b705"),
]

# 各种标注的最小尺寸（页面比例）。太小了既看不清也点不中，拖到这个值就停。
MIN_W = 0.012
MIN_H = 0.006

_DEFAULT = {
    "kind": "box",
    "page": 0,
    "x": 0.1,
    "y": 0.1,
    "w": 0.3,
    "h": 0.1,
    "text": "",
    "size": 14.0,
    "color": "#d0021b",
    "align": "left",
    "fill": False,
    "width": 1.6,
    "flip": False,
    "src": "",
}


def rgb(color: str) -> tuple[float, float, float]:
    """#rrggbb → 0–1 的三元组。写错颜色就退回黑色，别让一条输入毁掉出片。"""
    s = str(color or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return (0.0, 0.0, 0.0)
    try:
        return (int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0,
                int(s[4:6], 16) / 255.0)
    except ValueError:
        return (0.0, 0.0, 0.0)


def rgb255(color: str) -> tuple[int, int, int]:
    r, g, b = rgb(color)
    return (round(r * 255), round(g * 255), round(b * 255))


def clamp01(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else float(v))


def make(kind: str, page: int, rect, **style) -> dict:
    """按矩形（页面比例）造一个标注。rect 是 (x, y, w, h)。

    比例之外的样式从 _DEFAULT 补，用户给什么就用什么——不存在「必须传全」
    这种要求，调用方只写自己关心的那几项。

    箭头是唯一的例外：它存的是「起点 + 有符号位移」，位移指向哪，箭头就
    指向哪——水平、垂直、任意斜线都画得出来。别的标注矩形必须正着存
    （x/y 是左上角），箭头要是也把负位移翻成正的，方向信息就丢了。
    """
    x, y, w, h = (float(v) for v in rect)
    if str(kind) == "arrow":
        x = clamp01(x)
        y = clamp01(y)
        w = max(-1.0, min(1.0, w))
        h = max(-1.0, min(1.0, h))
    else:
        # 反过来拖出来的框（左下 → 右上）也要变成正的
        if w < 0:
            x, w = x + w, -w
        if h < 0:
            y, h = y + h, -h
        x = clamp01(x)
        y = clamp01(y)
        w = max(MIN_W, min(1.0 - x, w))
        h = max(MIN_H, min(1.0 - y, h))
    a = dict(_DEFAULT)
    a.update(kind=str(kind) if kind in KINDS else "box", page=max(0, int(page)),
             x=x, y=y, w=w, h=h)
    a.update({k: v for k, v in style.items() if k in _DEFAULT})
    a["kind"] = str(a["kind"]) if a["kind"] in KINDS else "box"
    a["size"] = max(4.0, min(96.0, float(a["size"] or 14.0)))
    a["width"] = max(0.2, min(12.0, float(a["width"] or 1.6)))
    a["align"] = a["align"] if a["align"] in ("left", "center", "right") else "left"
    a["text"] = str(a["text"] or "")[:2000]
    a["src"] = str(a["src"] or "")
    a["fill"] = bool(a["fill"])
    a["flip"] = bool(a["flip"])
    return a


def normalized(a: dict) -> dict:
    """外部（工程文件 / 历史记录）拿回来的对象过一遍这里，缺的补上、错的夹住。"""
    return make(a.get("kind", "box"), a.get("page", 0),
                (a.get("x", 0.1), a.get("y", 0.1), a.get("w", 0.2), a.get("h", 0.1)),
                **{k: v for k, v in a.items()
                   if k not in ("kind", "page", "x", "y", "w", "h")})


def to_pt(a: dict, page_w: float, page_h: float) -> tuple[float, float, float, float]:
    """页面比例 → 纸张上的 pt 矩形。三个绘制出口都从这里取坐标。"""
    x0 = clamp01(a["x"]) * page_w
    y0 = clamp01(a["y"]) * page_h
    x1 = clamp01(a["x"] + a["w"]) * page_w
    y1 = clamp01(a["y"] + a["h"]) * page_h
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def page_items(annos, page_no: int) -> list[dict]:
    """挑出这一页的标注，保持加入顺序（后加的压在上面）。"""
    out = [a for a in (annos or []) if int(a.get("page", 0)) == int(page_no)]
    return out


def hit_test(annos, pt, tol: float = 0.0) -> int:
    """点到了哪个标注？从上往下找，返回在列表里的下标，没中返回 -1。

    pt 是页面比例坐标；tol 是容差（同为比例），细线的方框也点得中。
    箭头是条线不是块：按「点到线段的距离」判，点在包围盒空白处不算命中，
    不然两条交叉的箭头没法各选各的。
    """
    for i in reversed(range(len(annos or []))):
        a = annos[i]
        if a.get("kind") == "arrow":
            p1, p2 = arrow_ends(a)
            if _seg_dist(pt[0], pt[1], p1, p2) <= tol:
                return i
            continue
        if (a["x"] - tol <= pt[0] <= a["x"] + a["w"] + tol
                and a["y"] - tol <= pt[1] <= a["y"] + a["h"] + tol):
            return i
    return -1


def _seg_dist(px: float, py: float, p1, p2) -> float:
    """点到线段的距离。退化的零长线段按点到点算。"""
    import math  # noqa: PLC0415

    ax, ay = p2[0] - p1[0], p2[1] - p1[1]
    l2 = ax * ax + ay * ay
    if l2 <= 1e-12:
        return math.hypot(px - p1[0], py - p1[1])
    t = max(0.0, min(1.0, ((px - p1[0]) * ax + (py - p1[1]) * ay) / l2))
    return math.hypot(px - (p1[0] + t * ax), py - (p1[1] + t * ay))


# ------------------------------------------------------------------ 文字排版


def tokens(text: str) -> list[str]:
    """把一段话切成「不希望在中间断开」的碎片：英文单词、数字、标点各成一块，
    汉字一字一块。这样换行时中文可以任意断，英文单词不会被劈成两半。"""
    out: list[str] = []
    buf = ""
    for ch in str(text or ""):
        if ch == "\n":
            if buf:
                out.append(buf)
                buf = ""
            out.append("\n")
            continue
        if ch.isspace():
            if buf:
                out.append(buf)
                buf = ""
            out.append(" ")
            continue
        # 半角字母数字与常见连接符攒成一个词
        if ch.isascii() and (ch.isalnum() or ch in "-_/.:@#%&+=~!?'\"()[]"):
            buf += ch
            continue
        if buf:
            out.append(buf)
            buf = ""
        out.append(ch)
    if buf:
        out.append(buf)
    return out


def _hard_break(line: str, width: float, size: float,
                measure) -> list[str]:
    """把一行按字符硬切开，保证每段都塞得进 width。

    为什么需要它：tokens() 会把「fgsgfsdfg」这种连写串当成一个词，wrap_text
    只在「已经有一行、再塞一个词塞不下」时才拆词；整段只有一个词时根本走不到
    那段逻辑，于是字溢出框外——用户看到字在框右边，点它却选不中（命中按框算）。
    """
    out: list[str] = []
    cur = line
    while cur and measure(cur, size) > width:
        cut = len(cur)
        while cut > 1 and measure(cur[:cut], size) > width:
            cut -= 1
        out.append(cur[:cut])
        cur = cur[cut:]
    if cur:
        out.append(cur)
    return out


def wrap_text(text: str, width: float, size: float, measure) -> list[str]:
    """把文字折进 width（pt），返回逐行文本。measure(s, size) 返回字符串宽度。

    measure 由调用方提供：PDF 用 china-s 的真实字宽，画布用 QFontMetrics，
    预览用 PIL 字体。三者各自量，折行位置才能和自己那一套渲染对得上。
    """
    if width <= 0 or not str(text or "").strip():
        return []
    lines: list[str] = []
    cur = ""
    for tk in tokens(text):
        if tk == "\n":
            lines.extend(_hard_break(cur.rstrip(), width, size, measure))
            cur = ""
            continue
        if tk == " " and not cur:
            continue      # 行首不留空格
        cand = cur + tk
        if not cur or measure(cand, size) <= width:
            cur = cand
            continue
        lines.extend(_hard_break(cur.rstrip(), width, size, measure))
        cur = "" if tk == " " else tk
    lines.extend(_hard_break(cur.rstrip(), width, size, measure))
    return [ln for ln in lines if ln.strip()]


LINE_H = 1.32   # 行高 / 字号；中文在这个比例下不挤，英文也不散


def text_layout(a: dict, box, measure) -> tuple[list[tuple[float, float, str]], float]:
    """算出一段文字在框里逐行的基线与水平偏移。

    返回 ([(x, baseline_y, 行文本)], 行高)。垂直方向：字少时居中，字多时从顶
    往下排并**丢掉放不下的行**——宁可少一行，也不把字画到框外面压住别的图。
    """
    x0, y0, x1, y1 = box
    size = float(a["size"])
    lh = size * LINE_H
    lines = wrap_text(a["text"], (x1 - x0) - 2.0, size, measure)
    if not lines:
        return [], lh
    max_lines = max(1, int((y1 - y0) // lh))
    lines = lines[:max_lines]
    block_h = len(lines) * lh
    # 上留 size 的 0.2 做视觉呼吸，整体在框内垂直居中
    top = y0 + max(0.0, ((y1 - y0) - block_h) / 2.0)
    out = []
    for i, ln in enumerate(lines):
        w = measure(ln, size)
        if a["align"] == "center":
            tx = (x0 + x1) / 2.0 - w / 2.0
        elif a["align"] == "right":
            tx = x1 - w
        else:
            tx = x0
        out.append((tx, top + i * lh + size * 0.98, ln))
    return out, lh


def fit_text(a: dict, page_w: float, page_h: float) -> dict:
    """把文字框长到装得下这些字（只长不缩，也不越出纸面）。

    两个现实问题逼出这个函数：
    1. 「点一下放字」给的默认框是 180×26pt，写三行就被裁掉一半——用户当然
       以为字丢了；
    2. 框比最长的那个词还窄时，wrap_text 只能一个字一行，竖成一条线。

    所以宽度先放到「最长的词塞得下」，再按这个宽度算行数定高度；位置尽量
    不动，实在放不下就往上挪，保证整段都在纸面内。量宽用 PDF 那一套字宽，
    因为最终印出来的就是那个。
    """
    text = str(a.get("text") or "")
    if not text.strip():
        return dict(a)
    size = float(a.get("size") or 14.0)
    x0, y0, x1, y1 = to_pt(a, page_w, page_h)
    w, h = x1 - x0, y1 - y0
    from .pdfbuild import _text_width  # noqa: PLC0415 — 避免模块级循环导入

    def measure(s: str, size_: float) -> float:
        return _text_width(s, size_)

    longest = max((measure(t, size) for t in tokens(text) if t.strip()),
                  default=0.0)
    # 至少放过最长的词，再加一点边，免得只剩一个字符一行
    w = max(w, min(longest + 4.0, max(MIN_W * page_w, page_w - x0 - 2.0)))
    lines = wrap_text(text, w - 2.0, size, measure)
    need_h = len(lines) * size * LINE_H + 4.0
    h = max(h, need_h)
    # 放不下就往上挪：宁可盖住上面一点，也别让最后一行掉出纸外
    if y0 + h > page_h - 2.0:
        y0 = max(2.0, page_h - 2.0 - h)
    w = min(w, page_w - x0 - 2.0)
    h = min(h, page_h - y0 - 2.0)
    out = dict(a)
    out.update(x=x0 / page_w, y=y0 / page_h,
               w=max(MIN_W, w / page_w), h=max(MIN_H, h / page_h))
    return out


def arrow_ends(a, page_w: float = 1.0, page_h: float = 1.0):
    """箭头的起止点，返回 (起点, 终点)。

    位移 (w, h) 带方向：从起点 (x, y) 指向 (x+w, y+h)，用户往哪拖箭头就朝
    哪——不再像旧版那样被压在矩形的两条对角线上（只画得出 ↘ 和 ↖）。
    page 尺寸给 1.0 时返回的就是页面比例坐标，给实际 pt 就是纸面坐标。

    旧数据用「正矩形 + flip」表达方向（flip=True 从右下指向左上），这里照旧
    认得；新版创建的箭头 flip 恒为 False，方向全在位移的符号里。
    """
    x = clamp01(a["x"]) * page_w
    y = clamp01(a["y"]) * page_h
    x2 = x + float(a["w"]) * page_w
    y2 = y + float(a["h"]) * page_h
    if bool(a.get("flip")):
        return (x2, y2), (x, y)
    return (x, y), (x2, y2)


# ------------------------------------------------------------------ PDF 出口


def _measure_pdf(text: str, size: float) -> float:
    from .pdfbuild import _text_width  # noqa: PLC0415 — 避免模块级循环导入

    return _text_width(text, size)


def draw_pdf(page, annos, page_w: float, page_h: float) -> None:
    """把标注画进 PyMuPDF 页面。页码/图注之后调用，所以标注压在最上层。"""
    import pymupdf  # noqa: PLC0415

    from .pdfbuild import CJK_FONT  # noqa: PLC0415

    for a in annos or []:
        kind = a.get("kind")
        if kind == "arrow":
            # 箭头是「起点+位移」，不是矩形：水平/垂直箭头的包围盒宽或高为 0，
            # 过下面的宽高检查会被当成空框跳掉，所以单独走
            try:
                p1, p2 = arrow_ends(a, page_w, page_h)
                _arrow_pdf(page, p1, p2, rgb(a["color"]), float(a["width"]))
            except (RuntimeError, ValueError, OSError):
                continue
            continue
        box = to_pt(a, page_w, page_h)
        if box[2] - box[0] < 0.5 or box[3] - box[1] < 0.5:
            continue
        col = rgb(a["color"])
        try:
            if kind == "redact":
                # 纯色块盖住：图片型 PDF 没有文字层可删，覆盖就是能做到的全部
                page.draw_rect(pymupdf.Rect(*box), color=None, fill=col,
                               width=0, overlay=True)
            elif kind == "box":
                page.draw_rect(pymupdf.Rect(*box), color=col,
                               fill=col if a["fill"] else None,
                               width=float(a["width"]), overlay=True)
            elif kind == "stamp":
                if _src_ok(a["src"]):
                    _stamp_pdf(page, pymupdf.Rect(*box), a["src"])
            elif kind == "text":
                _text_pdf(page, a, box, col, CJK_FONT)
        except (RuntimeError, ValueError, OSError):
            # 一条标注画坏了不该让整本 PDF 出不来：跳过，其余照画
            continue


def load_stamp(path: str):
    """读签名图并裁掉四周的透明留白；不是透明图就原样返回 RGB 版。

    裁边是为了「框多大签名就多大」：用户截的签名图总带一大片空白，不裁的话
    拖到再大的框，真迹也只占中间一小块，缩放完全失去意义。
    """
    from PIL import Image  # noqa: PLC0415

    im = Image.open(path)
    im.load()
    has_alpha = im.mode in ("RGBA", "LA", "P") or "transparency" in im.info
    if not has_alpha:
        return im.convert("RGB"), None
    im = im.convert("RGBA")
    alpha = im.getchannel("A")
    if alpha.getextrema()[1] < 8:
        return None, None      # 整张全透明，画了也看不见
    box = alpha.getbbox()
    if box:
        im = im.crop(box)
        alpha = im.getchannel("A")
    if alpha.getextrema()[0] >= 250:
        return im.convert("RGB"), None    # 裁完发现是实心图，省掉遮罩
    return im.convert("RGB"), alpha


def _stamp_pdf(page, box, path: str) -> None:
    """插入签名/图章。

    PyMuPDF 的 insert_image(filename=…) 走「原样嵌入」快路径，PNG 的 alpha
    通道会被直接丢掉——实测透明区域会变成实心色块。改用 stream 传一份 RGBA
    PNG，它才会解码并生成 smask；比手工拆 RGB + 遮罩两张图更省事，也不会有
    「遮罩被当成第二张图贴上去」这种怪事。
    """
    import io  # noqa: PLC0415

    im, alpha = load_stamp(path)
    if im is None:
        return
    if alpha is not None:
        im = im.convert("RGBA")
        im.putalpha(alpha)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    page.insert_image(box, stream=buf.getvalue(), keep_proportion=True,
                      overlay=True)


def _arrow_pdf(page, p1, p2, col, width: float) -> None:
    """一条线加一个实心三角头。头的大小随线宽走，粗箭头才不显得头小身子大。"""
    import math

    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(dx, dy)
    if length < 1.0:
        return
    head = max(6.0, min(22.0, length * 0.22 + width * 2.0))
    ux, uy = dx / length, dy / length
    base = (p2[0] - ux * head, p2[1] - uy * head)
    # 线只画到三角底边，否则粗线会从箭头尖里戳出来
    sh = page.new_shape()
    sh.draw_line(p1, base)
    sh.finish(color=col, width=width, lineCap=1, closePath=False)
    sh.commit()

    half = head * 0.42
    nx, ny = -uy, ux
    tri = [
        (p2[0], p2[1]),
        (base[0] + nx * half, base[1] + ny * half),
        (base[0] - nx * half, base[1] - ny * half),
    ]
    sh2 = page.new_shape()
    sh2.draw_polyline(tri)
    sh2.finish(color=col, fill=col, closePath=True)
    sh2.commit()


def _text_pdf(page, a: dict, box, col, font: str) -> None:
    items, _lh = text_layout(a, box, _measure_pdf)
    for x, y, line in items:
        page.insert_text((x, y), line, fontname=font,
                         fontsize=float(a["size"]), color=col)


def _src_ok(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path)
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------------ 预览出口


def draw_pil(sheet, annos, page_w: float, page_h: float, scale: float,
             font_for) -> None:
    """把标注画进整页 PIL 图（preview_sheet 用）。

    font_for(px) 返回 PIL 字体；文字度量和折行走同一个 font，所以预览里
    折行位置和 PDF 里基本一致（两套字体宽度略有差别，看不出来）。
    """
    from PIL import Image, ImageDraw  # noqa: PLC0415

    dr = ImageDraw.Draw(sheet, "RGBA")
    for a in annos or []:
        kind = a.get("kind")
        if kind == "arrow":
            p1, p2 = arrow_ends(a, page_w, page_h)
            _arrow_pil(dr, p1, p2, rgb255(a["color"]), float(a["width"]),
                       scale)
            continue
        box = to_pt(a, page_w, page_h)
        px = tuple(round(v * scale) for v in box)
        if px[2] <= px[0] or px[3] <= px[1]:
            continue
        col = rgb255(a["color"])
        if kind == "redact":
            dr.rectangle(px, fill=col + (255,))
        elif kind == "box":
            w = max(1, round(float(a["width"]) * scale))
            if a["fill"]:
                dr.rectangle(px, fill=col + (255,))
            else:
                dr.rectangle(px, outline=col + (255,), width=w)
        elif kind == "stamp":
            _stamp_pil(sheet, a, px)
        elif kind == "text":
            _text_pil(dr, a, box, col, scale, font_for)


def _arrow_pil(dr, p1, p2, col, width: float, scale: float) -> None:
    import math

    p1 = (p1[0] * scale, p1[1] * scale)
    p2 = (p2[0] * scale, p2[1] * scale)
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = math.hypot(dx, dy)
    if length < 1.0:
        return
    head = max(6.0 * scale, min(22.0 * scale, (length * 0.22 + width * scale * 2.0)))
    ux, uy = dx / length, dy / length
    base = (p2[0] - ux * head, p2[1] - uy * head)
    dr.line([p1, base], fill=col + (255,), width=max(1, round(width * scale)))
    half = head * 0.42
    nx, ny = -uy, ux
    dr.polygon([p2, (base[0] + nx * half, base[1] + ny * half),
                (base[0] - nx * half, base[1] - ny * half)], fill=col + (255,))


def _text_pil(dr, a: dict, box, col, scale: float, font_for) -> None:
    size_px = max(6, round(float(a["size"]) * scale))
    font = font_for(size_px)

    def measure(s, _size):
        try:
            return float(font.getlength(s))
        except AttributeError:      # 位图字体没有 getlength
            return len(s) * size_px * 0.55

    # 折行按像素量，所以把框宽也换成像素
    px_box = (box[0] * scale, box[1] * scale, box[2] * scale, box[3] * scale)
    lines = wrap_text(a["text"], (px_box[2] - px_box[0]) - 2.0 * scale, size_px,
                      measure)
    if not lines:
        return
    lh = size_px * LINE_H
    max_lines = max(1, int((px_box[3] - px_box[1]) // lh))
    lines = lines[:max_lines]
    top = px_box[1] + max(0.0, ((px_box[3] - px_box[1]) - len(lines) * lh) / 2.0)
    for i, ln in enumerate(lines):
        w = measure(ln, size_px)
        if a["align"] == "center":
            x = (px_box[0] + px_box[2]) / 2.0 - w / 2.0
        elif a["align"] == "right":
            x = px_box[2] - w
        else:
            x = px_box[0]
        try:
            dr.text((x, top + i * lh + size_px * 0.98), ln, font=font,
                    fill=col + (255,), anchor="ls")
        except (ValueError, TypeError):
            dr.text((x, top + i * lh), ln, font=font, fill=col + (255,))


def _stamp_pil(sheet, a: dict, px) -> None:
    from PIL import Image, ImageOps  # noqa: PLC0415

    path = a.get("src") or ""
    if not _src_ok(path):
        return
    try:
        im, alpha = load_stamp(path)
    except (OSError, ValueError):
        return
    if im is None:
        return
    if alpha is not None:
        im = im.convert("RGBA")
        im.putalpha(alpha)
    box_w, box_h = px[2] - px[0], px[3] - px[1]
    im = ImageOps.contain(im, (max(1, box_w), max(1, box_h)), Image.LANCZOS)
    # 等比放进框里并居中：签名章不该被拉扁
    ox = px[0] + (box_w - im.width) // 2
    oy = px[1] + (box_h - im.height) // 2
    if im.mode == "RGBA":
        sheet.paste(im, (ox, oy), im)
    else:
        sheet.paste(im, (ox, oy))


# ------------------------------------------------------------------ 报告


def describe(annos) -> list[dict]:
    """写进 report.json 的那份：去掉字体度量用不到的字段，只留能复现的东西。"""
    out = []
    for a in annos or []:
        item = {k: a[k] for k in ("kind", "page", "x", "y", "w", "h", "color")}
        for k in ("text", "size", "align", "fill", "width", "flip"):
            if a.get(k):
                item[k] = a[k]
        if a.get("src"):
            item["src"] = Path(a["src"]).name   # 只留文件名，别把本机路径写进报告
        out.append(item)
    return out


def summary(annos) -> str:
    """状态栏那句话：「3 处标注（文字 2 · 涂黑 1）」。"""
    if not annos:
        return ""
    counts: dict[str, int] = {}
    for a in annos:
        counts[a.get("kind", "box")] = counts.get(a.get("kind", "box"), 0) + 1
    bits = " · ".join(f"{KINDS.get(k, k)} {v}" for k, v in counts.items())
    return f"{len(annos)} 处标注（{bits}）"
