"""PDF 版式与组装：纸张、页边距、一页多张的格子、图片在格子里的落位。

这里只管「把已经压好的图排到页面上」，不负责图像处理，也不负责体积反推
（反推在 converter：它要反复重排重编码，才知道最终体积）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .presets import PAGE_FORMATS, PAPERS

MM = 72.0 / 25.4  # 1 毫米 = 2.8346 点
# 自适应页面最长边不超过 20 英寸：超长截图不该产出一张两米长的纸。
AUTO_MAX_PT = 1440.0

# 页码用的 PDF 内置中文字体：不嵌入字体文件，一本 PDF 只多 1 KB 左右。
# 换成嵌入子集实测多出 1.7 MB（整个 Droid Sans Fallback），和「体积上限」打架。
CJK_FONT = "china-s"
TEXT_COLOR = (0.16, 0.16, 0.16)


def page_size(paper: str, landscape: bool = False) -> tuple[float, float]:
    w_mm, h_mm = PAPERS.get(paper, PAPERS["A4"])
    if landscape:
        w_mm, h_mm = h_mm, w_mm
    return (w_mm * MM, h_mm * MM)


def auto_page_size(img_w: int, img_h: int, dpi: int,
                   margin_mm: float = 0.0,
                   extra_bottom: float = 0.0) -> tuple[float, float]:
    """「自适应」纸张：页面比例跟着图片走，图片按原像素 / dpi 落到纸上。

    书页、长图、横幅、网页截图、随手拍的照片都用它——不再被硬塞进 A4 留一大片白。
    返回的页面尺寸已含四周 margin，所以 content_rect 减去 margin 后正好等于
    图片按 dpi 换算的物理尺寸，实际打印分辨率仍等于设定的 dpi。

    extra_bottom 是图注要占的高度（pt）：自适应页不预留，图注就会掉到纸外面，
    或者把页高撑出「图片本身 + 两倍边距」的预期。
    """
    dpi = max(1, int(dpi))
    base_w = max(1, int(img_w)) * 72.0 / dpi
    base_h = max(1, int(img_h)) * 72.0 / dpi
    scale = min(1.0, AUTO_MAX_PT / max(base_w, base_h))
    m = max(0.0, float(margin_mm)) * MM
    extra = max(0.0, float(extra_bottom))
    return (base_w * scale + 2 * m, base_h * scale + 2 * m + extra)


def content_rect(page_w: float, page_h: float, margin_mm: float,
                 reserve: tuple[float, float] = (0.0, 0.0)
                 ) -> tuple[float, float, float, float]:
    """版心。reserve 是给页码/页脚让出的 (顶, 底) 高度，单位 pt。

    不让的话，页码会压在图上——尤其是图把整格填满的时候，这不是理论风险。
    """
    m = max(0.0, margin_mm) * MM
    # 边距再大也得留出能放图的地方，否则算出来是负宽
    m = min(m, page_w * 0.35, page_h * 0.35)
    top_extra = max(0.0, float(reserve[0]))
    bottom_extra = max(0.0, float(reserve[1]))
    y0 = m + top_extra
    y1 = page_h - m - bottom_extra
    # 页面本身很矮时保底：宁可文字离图近一点，也别把版心压没
    if y1 - y0 < page_h * 0.25:
        y0, y1 = m, page_h - m
    return (m, y0, page_w - m, y1)


def cells(page_w: float, page_h: float, margin_mm: float,
          per_page: int = 1, layout: str = "single",
          gap_mm: float = 4.0,
          reserve: tuple[float, float] = (0.0, 0.0),
          caption_h: float = 0.0
          ) -> list[tuple[float, float, float, float]]:
    """把版心切成 per_page 个格子（阅读顺序：从左到右、从上到下）。

    caption_h 是每格底部留给图注的高度（pt）：格子缩到图注带的上方，图注画在
    带子里，所以图不会被自己那行字压住。返回的是**图区**，不含图注带——
    调用方要画图注时自己加回去（基线 = 图区底 + size 的 1.1 倍左右）。
    """
    x0, y0, x1, y1 = content_rect(page_w, page_h, margin_mm, reserve)
    # 版心本来就窄时，固定 4mm 间隙可能把格子挤成负宽；按版心的 20% 封顶
    gap = min(max(0.0, gap_mm) * MM, (x1 - x0) * 0.2, (y1 - y0) * 0.2)
    if per_page <= 1:
        out = [(x0, y0, x1, y1)]
    elif per_page == 2:
        if layout == "row":  # 左右并排：证件正反面
            mid = (x0 + x1 - gap) / 2.0
            out = [(x0, y0, mid, y1), (mid + gap, y0, x1, y1)]
        else:
            mid = (y0 + y1 - gap) / 2.0  # 上下叠放：小票
            out = [(x0, y0, x1, mid), (x0, mid + gap, x1, y1)]
    else:
        # 4 张：2×2
        mid_x = (x0 + x1 - gap) / 2.0
        mid_y = (y0 + y1 - gap) / 2.0
        out = [
            (x0, y0, mid_x, mid_y),
            (mid_x + gap, y0, x1, mid_y),
            (x0, mid_y + gap, mid_x, y1),
            (mid_x + gap, mid_y + gap, x1, y1),
        ]
    return _shrink_for_caption(out, caption_h)


def _shrink_for_caption(boxes, caption_h: float):
    """每格底部让出 caption_h；格子本来就矮时最多让掉 30%，免得把图压没。"""
    h = max(0.0, float(caption_h))
    if h <= 0.0:
        return boxes
    out = []
    for bx0, by0, bx1, by1 in boxes:
        room = (by1 - by0) * 0.3
        out.append((bx0, by0, bx1, by1 - min(h, room)))
    return out


def caption_band(size: float) -> float:
    """图注那一行要占多高（pt）。1.85 倍字号：放得下汉字上下都探出来的部分。"""
    return max(0.0, float(size)) * 1.85


def fit_rect(cell: tuple[float, float, float, float], img_w: int, img_h: int
             ) -> tuple[float, float, float, float]:
    """等比放进格子并居中（contain），不拉伸。"""
    x0, y0, x1, y1 = cell
    cw, ch = x1 - x0, y1 - y0
    if img_w <= 0 or img_h <= 0:
        return cell
    scale = min(cw / img_w, ch / img_h)
    w, h = img_w * scale, img_h * scale
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def effective_dpi(rect: tuple[float, float, float, float], img_w: int, img_h: int) -> float:
    """图片落在纸面上后的实际打印分辨率（按宽算，短边同理）。"""
    w_pt = rect[2] - rect[0]
    if w_pt <= 0:
        return 0.0
    return img_w / (w_pt / 72.0)


@dataclass(frozen=True)
class PageText:
    """页面上的一行文字。坐标单位是 pt，y 是基线；PDF 与界面预览共用这份几何。"""

    text: str
    x: float
    y: float
    size: float
    align: str      # left / center / right


def has_page_text(opt) -> bool:
    return bool(getattr(opt, "page_numbers", False)
                or (getattr(opt, "footer_text", "") or "").strip())


def text_reserve(opt) -> tuple[float, float]:
    """为页码/页脚让出的 (顶, 底) 高度（pt）；不让就是 (0, 0)。

    行高按字号的 2.2 倍——够放下基线、下伸部和一点点留白，又不至于把 A4
    的版心挖掉一大块。
    """
    if not has_page_text(opt):
        return 0.0, 0.0
    size = float(getattr(opt, "page_number_size", 10.0) or 10.0)
    band = size * 2.2
    pos = getattr(opt, "page_number_pos", "bottom-center")
    wants_number = bool(getattr(opt, "page_numbers", False))
    wants_footer = bool((getattr(opt, "footer_text", "") or "").strip())
    top = band if (wants_number and pos.startswith("top")) else 0.0
    bottom = band if ((wants_number and pos.startswith("bottom")) or wants_footer) else 0.0
    return top, bottom


def _band_y(page_h: float, margin_pt: float, size: float, top: bool) -> float:
    """文字基线的 y：落在页边距那条带子里，垂直略微偏下才不显得飘。"""
    offset = max(margin_pt * 0.5, size * 0.8)
    return offset + size * 0.8 if top else page_h - offset


def page_texts(page_w: float, page_h: float, page_no: int, total: int,
               opt) -> list[PageText]:
    """这一页该画哪些字。page_no 是物理页序（从 1 起），total 是本份总页数。

    页码与页脚永远不重叠：页码占了左边，页脚就自动去右边，反之亦然。
    这是「用户勾了页脚又选了左下角页码」时唯一不会互相压字的排法。
    """
    if not has_page_text(opt) or page_no < 1 or total < 1:
        return []
    size = float(getattr(opt, "page_number_size", 10.0) or 10.0)
    m = max(0.0, float(getattr(opt, "margin_mm", 12.0))) * MM
    m = min(m, page_w * 0.35, page_h * 0.35)
    left, right, mid = m + 2.0, page_w - m - 2.0, page_w / 2.0
    out: list[PageText] = []
    footer = (getattr(opt, "footer_text", "") or "").strip()
    footer_slot = "left"

    if getattr(opt, "page_numbers", False):
        pos = getattr(opt, "page_number_pos", "bottom-center")
        vertical, _, align = pos.partition("-")
        align = align or "center"
        start = int(getattr(opt, "page_number_start", 1) or 0)
        n = start + page_no - 1
        last = start + total - 1
        fmt = PAGE_FORMATS.get(getattr(opt, "page_number_fmt", "cn_total"),
                               PAGE_FORMATS["cn_total"])
        text = fmt.format(n=n, total=last)
        x = {"left": left, "center": mid, "right": right}.get(align, mid)
        y = _band_y(page_h, m, size, top=(vertical == "top"))
        out.append(PageText(text, x, y, size, align))
        # 页码占了哪一侧，页脚就让到另一侧；居中时页脚固定靠左
        footer_slot = {"left": "right", "right": "left"}.get(align, "left")

    if footer:
        x = {"left": left, "center": mid, "right": right}[footer_slot]
        y = page_h - max(m * 0.5, size * 0.8)
        out.append(PageText(footer, x, y, size, footer_slot))
    return out


_font_metric: object | None = None


def _text_width(text: str, size: float) -> float:
    """按 PDF 里实际用的那套字量宽度，右对齐 / 居中才真的对得上。"""
    global _font_metric
    if _font_metric is None:
        import pymupdf  # noqa: PLC0415

        try:
            _font_metric = pymupdf.Font(CJK_FONT)
        except Exception:  # noqa: BLE001 — 量不出来就按半角估，不能让出片失败
            _font_metric = False
    if _font_metric:
        return float(_font_metric.text_length(text, size))
    return len(text) * size * 0.5


def draw_texts(page, texts: list[PageText]) -> None:
    """把 page_texts 的结果画到 PyMuPDF 页面上。"""
    for t in texts:
        x = t.x
        if t.align != "left":
            w = _text_width(t.text, t.size)
            x = t.x - (w if t.align == "right" else w / 2.0)
        page.insert_text((x, t.y), t.text, fontname=CJK_FONT,
                         fontsize=t.size, color=TEXT_COLOR)


def ellipsize(text: str, width: float, size: float) -> str:
    """把一行字截到 width 以内，末尾补省略号；一行都放不下就返回空。

    图注是用户自己填的自由文字，长句子直接画会溢出格子压到旁边的图上。
    按 PDF 那套中文字体量宽度，截出来的位置才和实际排版对得上。
    """
    if not text or width <= 0:
        return ""
    if _text_width(text, size) <= width:
        return text
    for cut in range(len(text) - 1, 0, -1):
        cand = text[:cut].rstrip() + "…"
        if _text_width(cand, size) <= width:
            return cand
    return ""


def draw_captions(page, entries) -> None:
    """entries 是 [(文字, 图区矩形, 图注带高, 字号)]，逐条画在格子底部居中。"""
    page_h = float(page.rect.height)
    for text, cell, band, size in entries:
        body = ellipsize(text, (cell[2] - cell[0]) - 4.0, size)
        if not body:
            continue
        w = _text_width(body, size)
        x = (cell[0] + cell[2]) / 2.0 - w / 2.0
        gap = min(size * 1.15, band) if band > 0 else size * 1.15
        # 手动排版把图拖到贴纸底时，字会跟着跑到纸外去——夹回来，宁可挨着图的下沿
        y = min(cell[3] + gap, page_h - 1.5)
        page.insert_text((x, y), body, fontname=CJK_FONT,
                         fontsize=size, color=TEXT_COLOR)


def _bookmark_title(it: dict) -> str:
    """书签那一行字：优先用户写的图注，其次文件名（去扩展名），再也没有就空。"""
    name = str(it.get("bookmark") or "").strip()
    if not name:
        try:
            name = Path(str(it.get("path", ""))).stem
        except (TypeError, ValueError):
            name = ""
    name = " ".join(name.split())
    return name[:60]


@dataclass
class Placement:
    page: int
    slot: int
    rect: tuple[float, float, float, float]
    dpi: float


def build_pdf(items: list[dict], out_path: Path, *, title: str = "",
              subject: str = "", opt=None, annotations=None) -> list[Placement]:
    """按 item 列表组装 PDF，返回每张图的实际落位（供报告核对 DPI）。

    item 需要的键：page / slot / page_w / page_h / cell / path / px。
    图片用 filename 直接嵌入：JPEG 原样以 DCTDecode 存进去，不会被二次编码
    （已实测：PDF 体积 ≈ 内嵌 JPEG 体积 + 几 KB 结构开销）。

    opt 只用来画页码与页脚：文字按物理页序编，页号从 page_number_start 起，
    「共 N 页」写的是末页页号（从第 7 页起编、共 3 页 = 第 7 页 / 共 9 页）。

    annotations 是用户加的标注（文字块 / 方框 / 箭头 / 涂黑 / 签名图），按
    「逻辑页号 → 对象列表」给。它们画在图片与页码之上，所以涂黑真的能盖住字。
    """
    import pymupdf  # noqa: PLC0415 — 惰性导入，命令行只跑 --version 时不用加载

    doc = pymupdf.open()
    try:
        # 只记「逻辑页号 → 文档里的第几页」，页对象现取现用：PyMuPDF 的 Page
        # 在文档结构变化后可能失效，攒一堆 Page 引用最后画页码会直接 assert。
        pages: dict[int, int] = {}
        placements: list[Placement] = []
        caps: dict[int, list[tuple]] = {}
        ordered = sorted(items, key=lambda d: (d["page"], d["slot"]))

        for it in ordered:
            idx = int(it["page"])
            if idx not in pages:
                pages[idx] = len(pages)
                doc.new_page(width=float(it["page_w"]), height=float(it["page_h"]))
            page = doc[pages[idx]]
            px_w, px_h = it["px"]
            cell = tuple(float(v) for v in it["cell"])
            rect = fit_rect(cell, px_w, px_h)
            page.insert_image(pymupdf.Rect(*rect), filename=str(it["path"]))
            placements.append(Placement(page=idx, slot=int(it["slot"]), rect=rect,
                                        dpi=effective_dpi(rect, px_w, px_h)))
            text = str(it.get("caption") or "").strip()
            if text:
                caps.setdefault(idx, []).append(
                    (text, cell, float(it.get("caption_h") or 0.0),
                     float(it.get("caption_size") or 9.0)))

        for idx in sorted(caps):
            page = doc[pages[idx]]
            draw_captions(page, caps[idx])

        if opt is not None and has_page_text(opt) and pages:
            total = len(pages)
            for idx in sorted(pages):
                page = doc[pages[idx]]
                texts = page_texts(page.rect.width, page.rect.height,
                                   pages[idx] + 1, total, opt)
                if texts:
                    draw_texts(page, texts)

        # 标注最后画：涂黑要盖住图，箭头和文字也不该被上面的图埋掉。
        # 页号对不上（用户删图后页数变少）的标注直接跳过——留在数据里，
        # 万一他又把图加回来还能用。
        if annotations:
            from . import annotate  # noqa: PLC0415 — 只在真要画时才加载

            for idx in sorted(annotations):
                if idx not in pages:
                    continue
                page = doc[pages[idx]]
                annotate.draw_pdf(page, annotations[idx],
                                  page.rect.width, page.rect.height)

        # 书签（大纲）：每张图一项，指向它所在的页。多张拼一页时刻意不去重——
        # 「第 5 张是那张发票」这类查找要靠它，合并成一条反而找不到。
        if pages and getattr(opt, "bookmarks", True):
            toc = []
            for it in ordered:
                title = _bookmark_title(it)
                if not title:
                    continue
                toc.append([1, title, pages[int(it["page"])] + 1])
            if toc and len(toc) <= 4000:   # 极端批量下宁可不写，也不写坏文档
                try:
                    doc.set_toc(toc)
                except (ValueError, RuntimeError):
                    pass

        meta = {
            "title": title or "图片转PDF",
            "subject": subject or "由 图片转PDF工具 生成",
            "creator": "图片转PDF工具",
            "producer": "图片转PDF工具 / PyMuPDF",
        }
        doc.set_metadata(meta)
        data = doc.tobytes(garbage=4, deflate=True, clean=True)
    finally:
        doc.close()  # 中间任何一步抛异常也要把文档句柄放掉

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out_path)  # 原子改名：中途失败不会留下半截 PDF
    finally:
        # 写盘失败时别把 .part 留在输出目录里骗人
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return placements
