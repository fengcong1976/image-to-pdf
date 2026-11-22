"""批处理编排：收集 → 逐张增强编码 → 组装 PDF → 体积不达标就重排。

两条硬约束：
1. 原图只读。所有输出写进独立的 out 目录，命名不覆盖已有文件。
2. 取消不产出半截 PDF。PDF 先写临时文件，成功后才原子改名。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import annotate, imaging
from .pdfbuild import (
    auto_page_size, build_pdf, caption_band, cells, effective_dpi, fit_rect,
    page_size, page_texts, text_reserve,
)
from .presets import (
    COLOR_NAMES, LAYOUT_NAMES, ORIENTATION_NAMES, PAPER_AUTO, SHADOW_NAMES,
    ConvertOptions,
)

OUT_DIRNAME = "out"
MAX_ATTEMPTS = 4
HASH_LIMIT = 20 * 1024 * 1024   # 超过 20MB 的图不整份算哈希，改记 size+mtime
QUALITY_FLOOR = 60
# 体积阶梯重试时复用「增强后的图」：增强与 DPI/质量无关，没理由每轮重跑一遍。
# 只在同一批内缓存，且给张数与单图像素都设上限，避免大批量把内存吃光。
ENHANCE_CACHE_MAX = 8
ENHANCE_CACHE_PIXELS = 40_000_000


# ---------------------------------------------------------------- 收集与排序


@dataclass
class ImageInfo:
    path: Path
    bytes: int
    width: int
    height: int
    taken: datetime | None = None
    # 「编辑这张」里单独设的修复参数覆盖；None / 空 = 完全跟随整批参数。
    # 只放图像处理项，纸张 / 拼版 / DPI / 体积属于整批，不在这里分叉。
    tune: dict | None = None
    # 成品预览里手动拖出来的位置：{"x","y","w","h"}，单位是页面的比例（0–1，
    # 左上角起）。None = 按拼版自动落格。它只决定「这张在它那一页上摆在哪」，
    # 在哪一页仍由列表顺序决定——否则拖一下顺序就全乱，比不给拖更糟。
    place: dict | None = None
    # 这一张的图注。空 = 用文件名清洗后的结果（见 caption_for）。
    # 只影响版面上那行小字和 PDF 书签，不参与任何图像处理。
    caption: str = ""


def natural_key(text: str):
    """文件名自然排序：IMG_2 排在 IMG_10 前面，而不是按字符比。"""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


# 相机、手机、微信、截图工具自动起的名字，整段都是设备给的编号，对读者没有
# 信息量。当图注用只会占地方，所以进版面前先剥掉。
_CAPTION_PREFIX = re.compile(
    r"^(?:img|dsc|dscf|dscn|pxl|mvimg|vid|photo|image|screenshot|"
    r"screen[\s_-]?shot|微信图片|微信截图|截图|照片|未命名|新建)[\s_\-]*",
    re.IGNORECASE,
)
# 一段连续 6 位以上的数字 = 日期或时间戳（20261109 / 20261109124512）
_CAPTION_STAMP = re.compile(r"(?<!\d)\d{6,}(?!\d)")


def clean_caption(name: str, limit: int = 60) -> str:
    """把文件名揉成一句能读的图注；揉不出有意义的字就返回空字符串。

    「IMG_20261109_124512」这类整段都是时间戳的名字洗完什么都不剩，返回空——
    这时版面上干脆不画那一行，比印一串数字强。而「合同-第3页」洗完还是
    「合同 第3页」，照常显示。
    """
    s = _CAPTION_PREFIX.sub("", str(name or "").strip())
    s = re.sub(r"[_\-.]+", " ", s)
    s = _CAPTION_STAMP.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_")
    if not s:
        return ""
    # 分隔符先归一之后整串只剩数字（2026 11 09），说明这名字本来就是日期，
    # 不是「这张图是什么」。留着当图注只会让人误以为它是内容。
    if all(tok.isdigit() for tok in s.split(" ")):
        return ""
    return s[:limit]


def caption_for(info: ImageInfo) -> str:
    """这张图版面上那行小字：用户手填的优先，没填才从文件名里洗。"""
    manual = str(getattr(info, "caption", "") or "")
    manual = " ".join(manual.split())
    if manual:
        return manual[:60]
    return clean_caption(info.path.stem)


def caption_band_for(info: ImageInfo, opt: ConvertOptions,
                     page_band: float | None = None) -> float:
    """这张图要给它留多高的图注带（pt）；0 = 不留。

    page_band 是「本页的决断」：同页只要有一张有图注，整页统一留高，图才会
    一般大。为 None 时按单张自己算（命令行逐张预览、自检用得上）。
    """
    if not opt.captions:
        return 0.0
    if page_band is not None:
        return max(0.0, float(page_band))
    return caption_band(opt.caption_size) if caption_for(info) else 0.0


def page_caption_bands(items: list[ImageInfo], opt: ConvertOptions) -> list[float]:
    """每一页要不要给图注留高：是 → 一个 band；否 → 0。

    只给「有字的那张」留会导致同页图大小不一，看着像没对齐；全页都没字还硬
    留，又白白把图缩小一圈。所以按页统一决断一次。
    """
    opt = opt.normalized()
    per = max(1, opt.per_page)
    band = caption_band(opt.caption_size) if opt.captions else 0.0
    if band <= 0 or not items:
        return [0.0] * ((len(items) + per - 1) // per)
    return [band if any(caption_for(i) for i in items[s:s + per]) else 0.0
            for s in range(0, len(items), per)]


def collect_images(inputs, *, sort_by_exif: bool = True) -> tuple[list[ImageInfo], list[str]]:
    """把「文件 / 文件夹」摊平成图片清单，返回 (清单, 被跳过的说明)。"""
    from PIL import Image

    files: list[Path] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            for root, dirs, names in os.walk(p):
                # 不进 out：那是本工具自己的产物目录（PDF、report.json，以及
                # 「应用」时存下的修改后图）。走进去会把成品当原图再加一遍。
                dirs[:] = [d for d in dirs
                           if not d.startswith(".") and d.lower() != OUT_DIRNAME]
                for name in sorted(names, key=natural_key):
                    if name.startswith("."):
                        continue
                    fp = Path(root) / name
                    if fp.suffix.lower() in imaging.SUPPORTED_EXT:
                        key = str(fp.resolve()).lower()
                        if key not in seen:
                            seen.add(key)
                            files.append(fp)
        elif p.is_file():
            key = str(p.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            if p.suffix.lower() in imaging.SUPPORTED_EXT:
                files.append(p)
            else:
                skipped.append(f"{p.name}：格式不支持")
        else:
            # 既不是目录也不是文件（超长路径 / 无权限 UNC / 已删除）：pathlib
            # 会把 OSError 吞成 False，以前这类输入静默消失、界面毫无反馈
            skipped.append(f"{p.name or str(p)[:40]}：路径不存在或无法读取")

    infos: list[ImageInfo] = []
    for fp in files:
        try:
            with Image.open(fp) as im:
                w, h = im.size
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{fp.name}：打不开（{type(exc).__name__}）")
            continue
        taken = imaging.read_exif_time(fp) if sort_by_exif else None
        infos.append(ImageInfo(path=fp, bytes=fp.stat().st_size, width=w, height=h, taken=taken))

    if sort_by_exif:
        # 有拍摄时间的按时间排在前，没有的按文件名自然排序接在后面。
        # 不把两者混在一起排：混排会让一批「扫描件+截图」的顺序变得不可预料。
        with_time = sorted([i for i in infos if i.taken],
                           key=lambda i: (i.taken, natural_key(i.path.name)))
        without = sorted([i for i in infos if not i.taken],
                         key=lambda i: natural_key(i.path.name))
        infos = with_time + without
    else:
        infos = sorted(infos, key=lambda i: natural_key(i.path.name))
    return infos, skipped


# ---------------------------------------------------------------- 报告结构


@dataclass
class ItemReport:
    src: str = ""
    src_bytes: int = 0
    src_px: tuple[int, int] = (0, 0)
    out_px: tuple[int, int] = (0, 0)
    dpi: float = 0.0
    quality: object = 82
    geometry: str = "原样嵌入"
    shadow: str = "关"
    color: str = "彩色"
    page: int = 0
    src_check: str = ""
    digest: str = ""
    ms: int = 0
    status: str = "ok"      # ok / 超出目标 / failed
    note: str = ""
    dst: str = ""           # 存单张图时的落盘路径（出片时为空）


@dataclass
class BatchReport:
    items: list[ItemReport] = field(default_factory=list)
    pdf_path: Path | None = None
    pdf_bytes: int = 0
    pages: int = 0
    target_bytes: int | None = None
    attempts: list[dict] = field(default_factory=list)
    cancelled: bool = False
    elapsed_s: float = 0.0
    summary: str = ""
    skipped: list[str] = field(default_factory=list)
    network_attempts: int = 0
    options: dict = field(default_factory=dict)
    per_image_tunes: dict = field(default_factory=dict)
    per_image_places: dict = field(default_factory=dict)
    per_image_captions: dict = field(default_factory=dict)
    annotations: list[dict] = field(default_factory=list)
    out_dir: Path | None = None

    @property
    def ok_count(self) -> int:
        return sum(1 for i in self.items if i.status != "failed")

    @property
    def failed(self) -> list[ItemReport]:
        return [i for i in self.items if i.status == "failed"]


# ---------------------------------------------------------------- 小工具


def fingerprint(path: Path) -> tuple[str, str]:
    """原图复核凭据：小文件算 SHA-256，大文件退回 size+mtime（并如实写明）。"""
    st = path.stat()
    if st.st_size <= HASH_LIMIT:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest(), "sha256"
    return f"{st.st_size}:{int(st.st_mtime)}", "size+mtime"


def unique_path(path: Path) -> Path:
    """重名不覆盖：out 里已有同名 PDF 就加 (1)(2)。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(1, 1000):
        cand = path.with_name(f"{stem}({n}){suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{stem}({int(time.time())}){suffix}")


def default_out_dir(items: list[ImageInfo], out_dir=None) -> Path:
    """默认落在第一张图旁边的 out 文件夹；一批图跨多个目录时也是这样。"""
    if out_dir:
        return Path(out_dir)
    if not items:
        return Path.cwd() / OUT_DIRNAME
    return items[0].path.parent / OUT_DIRNAME


# 单图允许覆盖的字段白名单：只放「这张图长什么样」的修复项。
# 纸张/拼版/清晰度/体积是整批的事，分叉会让页码与体积阶梯失去意义。
TUNE_KEYS = frozenset({
    "geometry", "perspective", "crop", "deskew", "max_angle",
    "shadow", "color_mode", "rotate", "scale_x", "scale_y",
})


def effective_options(info: ImageInfo, opt: ConvertOptions) -> ConvertOptions:
    """把「编辑这张」里单独设的修复项合到整批参数上，其余一律沿用整批。

    两层保险：白名单过滤掉拼错/多余的键（否则 dataclasses.replace 会直接抛），
    再 normalized() 一次，保证 geometry 总开关与三个细分项彼此自洽。
    没有覆盖时返回的就是调用方传进来的那组参数（只做归一化）。
    """
    tune = getattr(info, "tune", None)
    if not isinstance(tune, dict) or not tune:
        return opt.normalized()
    clean = {k: v for k, v in tune.items() if k in TUNE_KEYS}
    if not clean:
        return opt.normalized()
    return opt.copy(**clean).normalized()


def _quality_label(opt: ConvertOptions) -> object:
    return "无损（1 位 PNG）" if opt.color_mode == "bw" else opt.quality


PLACE_MIN = 0.02      # 手动排版最小边长（占页面比例）：再小就点不着了


def manual_rect(info: ImageInfo, opt: ConvertOptions,
                page_w: float, page_h: float
                ) -> tuple[float, float, float, float] | None:
    """把「手动拖出来的位置」换算成页面上的矩形（pt）；没有就 None。

    存的是页面比例而不是绝对点数，换纸张、改边距、改 DPI 之后位置仍然成立，
    不会因为换个纸型就跑到页外。越界的值在这里夹回来——宁可位置差一点，
    也不能让一张图跑到纸外面变成看不见。
    「自适应」纸张每页就是这一张图本身，手动位置没有意义，直接忽略。
    """
    place = getattr(info, "place", None)
    if not isinstance(place, dict) or opt.paper == PAPER_AUTO:
        return None
    try:
        x = float(place["x"]); y = float(place["y"])
        w = float(place["w"]); h = float(place["h"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        return None
    w = max(PLACE_MIN, min(1.0, w))
    h = max(PLACE_MIN, min(1.0, h))
    x = max(0.0, min(1.0 - w, x))
    y = max(0.0, min(1.0 - h, y))
    return (x * page_w, y * page_h, (x + w) * page_w, (y + h) * page_h)


def _geometry_label(res: imaging.EnhanceResult) -> str:
    return " + ".join(res.steps) if res.steps else "原样嵌入"


def edited_copy(info: ImageInfo) -> Path | None:
    """原图旁边 out 里那张「修改后的图」，没有就返回 None。

    规则来自用户的直白要求：编辑过的图就用编辑过的那张，没编辑过才用原图。
    所以出片、预览、成品预览的取图入口都先问一次这里。

    两个回退条件，都是为了别把过期结果悄悄带进 PDF：
    1. 修改后的图比原图还旧 —— 原图后来换过了，这张已经过期，回去用原图。
    2. 文件读不动 —— 交给上层按原图处理，而不是整批失败。
    同名覆盖把时间戳刷新到最新，所以正常编辑过的图不会被第 1 条误判。
    """
    base = info.path.parent / OUT_DIRNAME
    if not base.is_dir():
        return None
    best: Path | None = None
    for suffix in (".jpg", ".png"):
        cand = base / f"{info.path.stem}{suffix}"
        try:
            if not cand.is_file():
                continue
            if best is None or cand.stat().st_mtime > best.stat().st_mtime:
                best = cand
        except OSError:
            continue
    if best is None:
        return None
    try:
        if best.stat().st_mtime + 1e-6 < info.path.stat().st_mtime:
            return None   # 原图比修改后图还新：这张修改后图已经过期
    except OSError:
        return None
    return best


def _enhance(info: ImageInfo, opt: ConvertOptions, src=None,
             *, use_edited: bool | None = None) -> imaging.EnhanceResult:
    """按一组已归一化的参数做修复与画面效果，不改尺寸、不落纸。

    落纸出片（render_page）与存单张图（enhance_only）共用这一处，两种产物
    的观感因此不会分叉——用户存下来的图和 PDF 里看到的是同一条处理链。

    use_edited：编辑过的那张图（原图旁 out 里的同名图）优先。它已经带着
    裁边 / 纠偏 / 去阴影 / 画面效果了，再套一遍修复参数等于二次处理，
    所以这条分支只读文件、不再调 enhance。

    传了 src 就说明调用方已经定好像素来源（编辑窗口要拿原图试新参数），
    这时不再换成修改后的图，否则「改了参数即时重算预览」会看到上一版结果。
    """
    if use_edited is None:
        use_edited = src is None
    if use_edited:
        edited = edited_copy(info)
        if edited is not None:
            try:
                im = imaging.open_image(edited)
            except imaging.UnsupportedImage:
                im = None    # 修改后图坏了：按原图重算，别让整批跟着失败
            if im is not None:
                return imaging.EnhanceResult(
                    image=im,
                    steps=["用修改后的图"],
                    filter="bw" if edited.suffix.lower() == ".png" else "color",
                    notice=f"使用 out 里已保存的修改后图：{edited.name}",
                )
    im = src if src is not None else imaging.open_image(info.path)
    return imaging.enhance(
        im, geometry=opt.geometry, perspective=opt.perspective,
        crop=opt.crop, deskew=opt.deskew, max_angle=opt.max_angle,
        shadow=opt.shadow, color_mode=opt.color_mode,
        extra_rotate=getattr(opt, "rotate", 0.0),
        extra_scale_x=getattr(opt, "scale_x", 1.0),
        extra_scale_y=getattr(opt, "scale_y", 1.0),
    )


def enhance_only(info: ImageInfo, opt: ConvertOptions, index: int = 0,
                 *, use_edited: bool = False):
    """这一张按生效参数处理完的全分辨率图，不缩进格子、不写文件。

    PDF 要的是贴进格子的那张，图片编辑要的是原尺寸的那张。默认读原图
    （use_edited=False）：存图这个动作本身就是「重新算一遍并落盘」。
    返回 (PIL 图, EnhanceResult, ItemReport)，报告里写着实际用到的处理链。
    """
    opt = effective_options(info, opt)
    res = _enhance(info, opt, use_edited=use_edited)
    rep = ItemReport(src=str(info.path), src_bytes=info.bytes,
                     src_px=(info.width, info.height),
                     quality=_quality_label(opt),
                     color=COLOR_NAMES.get(opt.color_mode, opt.color_mode))
    rep.geometry = _geometry_label(res)
    rep.shadow = SHADOW_NAMES.get(res.shadow, "关")
    rep.out_px = res.image.size
    rep.page = index + 1
    return res.image, res, rep


# ---------------------------------------------------------------- 单张处理


def render_page(info: ImageInfo, opt: ConvertOptions, dpi: int, index: int = 0,
                src=None, enhanced_res=None, caption_h: float | None = None):
    """一张图走到「落在这张纸上会长什么样」为止，不写任何文件。

    返回 (page_im, res, cell, page_no, rep, (page_w, page_h))。真正出片
    （`_process_one`）与界面预览都调它——预览必须和成品同一条代码路径，
    否则「看到的」和「出来的」会悄悄分叉，这种 bug 最难被用户说清楚。

    enhanced_res 给体积阶梯的重试轮用：增强结果与 DPI/质量无关，没理由每轮
    重跑一遍找纸、霍夫直线和去阴影；传进来就只重做缩放与编码。

    info.tune 是「编辑选中」写下的单图修复参数；info.place 是在成品预览里手动
    拖出来的位置。两者都在这里统一合并，保证预览、出片、重试轮次走的是同一组
    生效参数与同一个落位。

    caption_h 是图注带的高度：给了就把格子底部让出来（图区变小，图注画在让出的
    那条带里），None 时按这张图自己有没有图注判断。返回的 cell 一律是**图区**。
    """
    opt = effective_options(info, opt)
    band = caption_band_for(info, opt, caption_h)
    if enhanced_res is not None:
        # 已经有增强结果，连原图都不必再读一遍
        res = enhanced_res
    else:
        res = _enhance(info, opt, src)
    rep = ItemReport(src=str(info.path), src_bytes=info.bytes,
                     src_px=(info.width, info.height),
                     quality=_quality_label(opt),
                     color=COLOR_NAMES.get(opt.color_mode, opt.color_mode))
    rep.geometry = _geometry_label(res)
    rep.shadow = SHADOW_NAMES.get(res.shadow, "关")

    w, h = res.image.size
    if opt.paper == PAPER_AUTO:
        # 页面跟着图片比例走，横竖由图片自己决定；normalized() 已保证一页一张
        pw, ph = auto_page_size(w, h, dpi, opt.margin_mm, extra_bottom=band)
    else:
        if opt.orientation == "landscape":
            landscape = True
        elif opt.orientation == "portrait":
            landscape = False
        else:
            # 横图在「一页一张」且允许自动横版时，页面也跟着转，别缩在竖页中间
            landscape = bool(opt.auto_landscape and opt.per_page == 1 and w > h * 1.02)
        pw, ph = page_size(opt.paper, landscape)
    # 页码 / 页脚要占一条纸边，版心得先让出来，否则字会压在图上
    cell_list = cells(pw, ph, opt.margin_mm, opt.per_page, opt.layout,
                      gap_mm=opt.gap_mm, reserve=text_reserve(opt),
                      caption_h=band)
    page_no = index // max(1, opt.per_page)
    slot = index % max(1, opt.per_page)
    cell = cell_list[min(slot, len(cell_list) - 1)]
    manual = manual_rect(info, opt, pw, ph)
    if manual is not None:
        cell = manual       # 手动摆过的这一张，按它的矩形落，不再进格子

    box_w = max(1, int(round((cell[2] - cell[0]) / 72.0 * dpi)))
    box_h = max(1, int(round((cell[3] - cell[1]) / 72.0 * dpi)))
    page_im = imaging.fit_within(res.image, box_w, box_h)

    rect = fit_rect(cell, *page_im.size)
    rep.out_px = page_im.size
    rep.dpi = round(effective_dpi(rect, *page_im.size), 1)
    # 源图本身就不够大时如实说明：不放大凑 DPI，也不假装达标
    if page_im.size == res.image.size and rep.dpi < dpi * 0.92:
        rep.note = (f"源图分辨率不足：实际 {rep.dpi:.0f}dpi（目标 {dpi}dpi），"
                    f"已按原像素出片，凑近重拍可提高清晰度")
    rep.page = page_no + 1
    return page_im, res, cell, page_no, rep, (pw, ph)


def preview_page(info: ImageInfo, opt: ConvertOptions, dpi: int | None = None,
                 index: int = 0):
    """界面预览：返回 (原图, 落纸后的图, 报告, (页宽, 页高, 落位框))。

    原图是**未处理**的那一张，用来对比；页宽页高与落位框（单位都是 pt）给
    界面画「整页缩略」用——不然 2 张拼一页、4 张拼一页的排法根本看不出来。
    """
    opt = effective_options(info, opt)
    src = imaging.open_image(info.path)
    page_im, _res, cell, _page, rep, (pw, ph) = render_page(
        info, opt, int(dpi or opt.dpi), index, src=src)
    return src, page_im, rep, (pw, ph, cell)


def save_processed_images(items: list[ImageInfo], opt: ConvertOptions, *,
                          out_dir=None, progress=None,
                          cancel=None) -> list[ItemReport]:
    """把「修改后的图」存成图片，默认落在各自原图旁的 out 目录。

    与出片的两个关键差别：
    1. 不贴纸、不缩进格子，存的是全分辨率的那张。点「应用」是为了拿到图，
       不是为了从 PDF 里再抠一张。
    2. 同名覆盖，而不是像 PDF 那样加 (1)(2)(3)。out 里的这张图随时能再算
       一遍，反复调参数不该堆出一串副本。

    唯一的例外：目标文件正好就是被编辑的原图（比如原图本身放在 out 里），
    这时退让成「名字-修改后」并避让重名——原图一律只读这条不能破。

    存下来的这张会被出片优先采用（见 edited_copy）：编辑过的图就用编辑过的
    那张，没编辑过才用原图。

    progress(done, total, ItemReport) 每存完一张回调一次；cancel() 返回 True
    即停止。返回逐张报告，成功的那几张 dst 里是落盘路径。
    """
    opt = opt.normalized()
    reports: list[ItemReport] = []
    written: list[Path] = []   # 取消时回收本轮落盘的文件，不留半截
    total = len(items)
    cancelled = False
    for i, info in enumerate(items):
        if cancel is not None and cancel():
            cancelled = True
            break
        t0 = time.perf_counter()
        try:
            im, res, rep = enhance_only(info, opt, i)
            dst_dir = Path(out_dir) if out_dir else info.path.parent / OUT_DIRNAME
            dst_dir.mkdir(parents=True, exist_ok=True)
            suffix = ".png" if res.filter == "bw" else ".jpg"
            dst = dst_dir / f"{info.path.stem}{suffix}"
            try:
                clash = dst.resolve() == info.path.resolve()
            except OSError:
                clash = False
            if clash:
                dst = unique_path(dst_dir / f"{info.path.stem}-修改后{suffix}")
            imaging.save_page_image(im, dst, color_mode=res.filter,
                                    quality=opt.quality)
            # 同一张图只留一份修改后的版本：换了画面效果后缀会变，旧的留着
            # 会让「有则优先」在 jpg / png 之间摇摆在两代结果之间。
            stale = dst.with_suffix(".png" if suffix == ".jpg" else ".jpg")
            if stale.is_file() and stale != dst:
                try:
                    stale.unlink()
                except OSError:
                    pass
            written.append(dst)
            rep.dst = str(dst)
            rep.ms = int((time.perf_counter() - t0) * 1000)
        except imaging.UnsupportedImage as exc:
            rep = ItemReport(src=str(info.path), status="failed", note=str(exc))
        except Exception as exc:  # noqa: BLE001 — 一张存不下来不能拖垮整批
            rep = ItemReport(src=str(info.path), status="failed",
                             note=f"保存失败：{type(exc).__name__}: {exc}")
        reports.append(rep)
        if progress is not None:
            progress(i + 1, total, rep)
    if cancelled:
        # 半截的「修改后的图」比没有更糟：下一次出片 edited_copy 会优先用它，
        # 而且不再套修复参数，用户改的设置等于白改。
        for dst in written:
            try:
                dst.unlink(missing_ok=True)
            except OSError:
                pass
        reports.clear()
    return reports


def sheet_count(items: list[ImageInfo], opt: ConvertOptions) -> int:
    """这批图按当前拼版会出几页（空清单算 0 页）。"""
    if not items:
        return 0
    per = max(1, opt.normalized().per_page)
    return (len(items) + per - 1) // per


# 预览里画页码要真中文字体；找不到就退回位图字体，中文会变方框但不会崩。
FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)
_font_cache: dict[int, object] = {}


def _preview_font(px: int):
    from PIL import ImageFont  # noqa: PLC0415 — 只在预览时用

    px = max(6, int(px))
    hit = _font_cache.get(px)
    if hit is not None:
        return hit
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, px)
            _font_cache[px] = font
            return font
        except OSError:
            continue
    font = ImageFont.load_default()
    _font_cache[px] = font
    return font


def _paste_scaled(sheet, im, rect, scale: float) -> None:
    """把一张图按页面矩形贴进整页图；矩形是 pt，scale 是 px/pt。"""
    from PIL import Image, ImageOps  # noqa: PLC0415

    px0 = max(0, round(rect[0] * scale))
    py0 = max(0, round(rect[1] * scale))
    px1 = min(sheet.width, round(rect[2] * scale))
    py1 = min(sheet.height, round(rect[3] * scale))
    if px1 <= px0 or py1 <= py0:
        return
    placed = ImageOps.contain(im, (px1 - px0, py1 - py0), Image.LANCZOS)
    sheet.paste(placed, (px0 + (px1 - px0 - placed.width) // 2,
                         py0 + (py1 - py0 - placed.height) // 2))


def sheet_layers(items: list[ImageInfo], opt: ConvertOptions, page_no: int = 0,
                 dpi: int | None = None) -> dict:
    """第 page_no 页的可绘制元素：白纸（含页码/页脚）+ 每张图的矩形与像素。

    layers 是 [(在 items 里的下标, PIL 图, 页面矩形 pt)]。界面把每张图单独画在
    自己的矩形里，所以「排版微调」拖动时只要改矩形就能跟手，不必重算整页；
    preview_sheet 再把它们贴成一张整页图，给命令行、自检和翻页缓存用。
    """
    empty = {"page_pt": None, "bg": None, "layers": [], "reps": [], "scale": 1.0}
    if not items:
        return empty
    opt = opt.normalized()
    per = max(1, opt.per_page)
    start = max(0, int(page_no)) * per
    part = items[start:start + per]
    if not part:
        return empty

    base_dpi = int(dpi or opt.dpi)
    # 图注按页统一留高：本页只要有一张要写字，整页的图都缩一点，排出来才齐
    band = caption_band(opt.caption_size) if (opt.captions and any(
        caption_for(i) for i in part)) else 0.0
    layers: list[tuple] = []
    reps: list[ItemReport] = []
    page_pt: tuple[float, float] | None = None
    for k, info in enumerate(part):
        page_im, _res, cell, _page, rep, (pw, ph) = render_page(
            info, opt, base_dpi, start + k, caption_h=band)
        if page_pt is None:
            page_pt = (pw, ph)
        # 图注跟着这一层一起交出去：拖动时字要贴着图走，不能焊死在白纸上
        cap = caption_for(info) if opt.captions else ""
        layers.append((start + k, page_im, fit_rect(cell, *page_im.size),
                       cap, float(opt.caption_size)))
        reps.append(rep)

    from PIL import Image  # noqa: PLC0415 — 只在预览时用，命令行不背这个开销

    pw, ph = page_pt
    # 屏幕用不上 200dpi 的原尺寸：整页封到 1600px 内，翻页才不会一卡一卡
    shrink = min(1.0, 1600.0 / max(1.0, pw / 72.0 * base_dpi, ph / 72.0 * base_dpi))
    scale = base_dpi / 72.0 * shrink
    size = (max(1, round(pw * scale)), max(1, round(ph * scale)))
    bg = Image.new("RGB", size, (255, 255, 255))

    texts = page_texts(pw, ph, int(page_no) + 1, sheet_count(items, opt), opt)
    if texts:
        from PIL import ImageDraw  # noqa: PLC0415

        dr = ImageDraw.Draw(bg)
        anchors = {"left": "ls", "center": "ms", "right": "rs"}
        for t in texts:
            font = _preview_font(t.size * scale)
            try:
                dr.text((t.x * scale, t.y * scale), t.text, font=font,
                        fill=(41, 41, 41), anchor=anchors.get(t.align, "ls"))
            except (ValueError, TypeError):
                # 位图字体不吃 anchor，退回左上角对齐，位置略偏但不至于不画
                dr.text((t.x * scale, t.y * scale), t.text, font=font,
                        fill=(41, 41, 41))
    return {"page_pt": page_pt, "bg": bg, "layers": layers, "reps": reps,
            "scale": scale}


def preview_sheet(items: list[ImageInfo], opt: ConvertOptions, page_no: int = 0,
                  dpi: int | None = None, annotations=None):
    """把第 page_no 页整页拼出来，给「预览整份 PDF」翻页看成品。

    和出片干的是同一件事：每张先用 render_page 落到自己的格子（或手动位置），
    再按 build_pdf 的落位规则贴到整页白纸上——所以看到的页就是会生成的那页，
    包括多张拼一页、边距、横版、页码与单张覆盖。

    annotations 只在这一页需要时才画（文字 / 方框 / 涂黑 / 签名图），
    用的是和 PDF 同一份几何换算，位置不会两处不一致。

    返回 (整页图, 本页每张的报告)；页号越界返回 (None, [])。
    """
    data = sheet_layers(items, opt, page_no, dpi)
    if data["bg"] is None:
        return None, []
    sheet = data["bg"].copy()
    for item in data["layers"]:
        _paste_scaled(sheet, item[1], item[2], data["scale"])
    _paste_captions(sheet, data["layers"], data["scale"])
    annos = _group_annos(annotations).get(max(0, int(page_no)), [])
    if annos:
        pw, ph = data["page_pt"]
        annotate.draw_pil(sheet, annos, pw, ph, data["scale"], _preview_font)
    return sheet, data["reps"]


def _paste_captions(sheet, layers, scale: float) -> None:
    """把图注贴进整页图。位置算法和出片时的 draw_captions 一一对应。"""
    from PIL import ImageDraw  # noqa: PLC0415

    from .pdfbuild import ellipsize  # noqa: PLC0415 — 只为换算截断宽度

    dr = ImageDraw.Draw(sheet)
    for item in layers:
        if len(item) < 4 or not item[3]:
            continue
        rect, text, size = item[2], item[3], float(item[4])
        body = ellipsize(text, (rect[2] - rect[0]) - 4.0, size)
        if not body:
            continue
        font = _preview_font(size * scale)
        x = (rect[0] + rect[2]) / 2.0 * scale
        y = min((rect[3] + size * 1.15) * scale, sheet.height - 2)
        try:
            dr.text((x, y), body, font=font, fill=(41, 41, 41), anchor="ms")
        except (ValueError, TypeError):
            dr.text((x, y), body, font=font, fill=(41, 41, 41))


def _process_one(info: ImageInfo, opt: ConvertOptions, dpi: int, quality: int,
                 index: int, workdir: Path, enhanced_res=None, cache: dict | None = None,
                 caption_h: float | None = None):
    """处理一张图：增强 → 定页面 → 重采样 → 落成中间文件。

    caption_h 是这一页的图注带高度（由 convert_batch 按页算好传进来）。
    返回 (item_report, pdf_item) ；失败时 pdf_item 为 None。
    """
    t0 = time.perf_counter()
    opt = effective_options(info, opt)   # 编码格式/页内槽位也要用这张图真正生效的参数
    rep = ItemReport(src=str(info.path), src_bytes=info.bytes,
                     src_px=(info.width, info.height),
                     quality=_quality_label(opt),
                     color=COLOR_NAMES.get(opt.color_mode, opt.color_mode))
    try:
        digest, src_check = fingerprint(info.path)
        rep.digest, rep.src_check = digest, src_check
        page_im, res, cell, page_no, rep, (pw, ph) = render_page(
            info, opt, dpi, index, enhanced_res=enhanced_res, caption_h=caption_h)
        # render_page 自己造了一份报告，把「原图只读」的凭据补回去
        rep.digest, rep.src_check = digest, src_check
        if (cache is not None and index not in cache
                and len(cache) < ENHANCE_CACHE_MAX
                and res.image.width * res.image.height <= ENHANCE_CACHE_PIXELS):
            cache[index] = res
        slot = index % max(1, opt.per_page)

        tmp = workdir / f"{index:05d}.png" if opt.color_mode == "bw" else workdir / f"{index:05d}.jpg"
        imaging.save_page_image(page_im, tmp, color_mode=opt.color_mode, quality=quality)

        pdf_item = {
            "page": page_no, "slot": slot, "page_w": pw, "page_h": ph,
            "cell": cell, "path": tmp, "px": page_im.size,
            # 图注与书签：版面上那行小字与阅读器侧边栏的目录，都不影响图像本身
            "caption": caption_for(info) if opt.captions else "",
            "caption_h": caption_band_for(info, opt, caption_h),
            "caption_size": float(opt.caption_size),
            "bookmark": caption_for(info) or info.path.stem,
        }
        # 原图复核：文件大小时刻都没变才算「原图不动」；哈希只在报告里给出凭据。
        now = info.path.stat()
        if now.st_size != info.bytes:
            rep.status = "failed"
            rep.note = "原图在处理过程中被改动，已停止交付这一批"
            return rep, None
    except imaging.UnsupportedImage as exc:
        rep.status = "failed"
        rep.note = str(exc)
        return rep, None
    except Exception as exc:  # noqa: BLE001 — 一张图坏掉不能拖垮整批
        rep.status = "failed"
        rep.note = f"处理失败：{type(exc).__name__}: {exc}"
        return rep, None

    rep.ms = int((time.perf_counter() - t0) * 1000)
    return rep, pdf_item


# ---------------------------------------------------------------- 体积阶梯


def _next_step(dpi: int, quality: int, size: int, target: int,
               floor: int, color_mode: str) -> tuple[int, int] | None:
    """按实测体积反推下一档：线性尺寸按 sqrt(目标/实际) 缩，质量同步往下让。

    DPI 已经压到地板时，就只能靠质量换体积，这时质量一次多降一点，
    免得 3 轮试完还卡在 q70 上、白白错过 q60 那一档。
    """
    if size <= target:
        return None
    ratio = max(0.05, target / float(size))
    scale = max(0.55, min(0.97, math.sqrt(ratio) * 0.97))
    ndpi = max(floor, int(dpi * scale))
    # 地板只能挡住「继续往下」，绝不能把质量往上抬：用户可能主动选了
    # q45（合法下限 40），旧式 max(60, 45-6)=60 会先升后降来回震荡。
    q_floor = min(QUALITY_FLOOR, quality)
    if color_mode == "bw":
        nq = quality  # 1 位 PNG 没有质量档，只能降 DPI
    elif ndpi >= dpi:
        nq = max(q_floor, quality - 15)
    else:
        nq = max(q_floor, quality - 6)
    if ndpi >= dpi and nq >= quality:
        return None
    return ndpi, nq


# ---------------------------------------------------------------- 主入口


def _group_annos(annotations) -> dict[int, list[dict]]:
    """把标注整理成「页号 → 列表」，顺手把每条都过一遍 normalized。

    界面传的是扁平列表（每条自带 page），也接受已经是分组的 dict——出片时
    两种都有人用，统一在这里归一，后面的代码只认一种形状。
    """
    if not annotations:
        return {}
    out: dict[int, list[dict]] = {}
    if isinstance(annotations, dict):
        for page, lst in annotations.items():
            try:
                idx = max(0, int(page))
            except (TypeError, ValueError):
                continue
            for a in lst or []:
                item = annotate.normalized(a)
                item["page"] = idx
                out.setdefault(idx, []).append(item)
        return out
    for a in annotations:
        item = annotate.normalized(a)
        out.setdefault(int(item["page"]), []).append(item)
    return out


def convert_batch(items: list[ImageInfo], *, out_dir=None, options: ConvertOptions,
                  progress=None, cancel=None, workers: int = 4,
                  pdf_name: str | None = None,
                  annotations=None, skipped: list[str] | None = None,
                  network_audit=None) -> BatchReport:
    """把一批图片转成一份 PDF。

    progress(done, total, ItemReport) 每完成一张回调一次。
    cancel() 返回 True 时立即停止提交，不保留半截 PDF。
    annotations 是用户往页面上加的文字 / 方框 / 箭头 / 涂黑 / 签名图，
    按「逻辑页号 → 列表」给；体积阶梯每一轮都会重新画一遍，因为这些是矢量，
    不随 DPI 变化——落位比例固定，纸变图变它们不变。
    """
    opt = options.normalized()
    report = BatchReport(target_bytes=opt.target_bytes, options=asdict(opt))
    # 收集阶段跳过的文件以前从不写进 report.json；联网审计的 attempts 也是
    # 调用方在返回之后才赋值，报告早就写完了。这里把两者都在写报告前落实。
    report.skipped = list(skipped or [])
    # 逐张单独修复的覆盖写进报告，方便事后核对「这张为什么和别张不一样」
    report.per_image_tunes = {
        str(i.path): dict(i.tune) for i in items
        if isinstance(getattr(i, "tune", None), dict) and i.tune
    }
    # 手动摆过的位置也记进去：拿到 PDF 的人能查到每张图是按什么坐标落的
    report.per_image_places = {
        str(i.path): dict(i.place) for i in items
        if isinstance(getattr(i, "place", None), dict) and i.place
    }
    # 图上印了哪行字也记一笔：图注可能是手填的，事后核对成品要靠它
    if opt.captions:
        report.per_image_captions = {
            str(i.path): caption_for(i) for i in items if caption_for(i)
        }
    # 标注是用户手工摆的，PDF 里看不出「谁画的」，报告里留一份才查得动
    annos_by_page = _group_annos(annotations)
    report.annotations = annotate.describe(
        [a for lst in annos_by_page.values() for a in lst])
    t_start = time.perf_counter()

    if not items:
        report.summary = "没有可转换的图片"
        return report

    dst_dir = default_out_dir(items, out_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    report.out_dir = dst_dir
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    name = pdf_name or f"图片转PDF-{stamp}.pdf"
    # 只取文件名：pathlib 遇到绝对路径会丢掉左侧，../ 会往上一级解析，
    # 用户给的 --name 不能把成品写出 out 目录。顺手补上 .pdf 后缀。
    name = Path(name).name
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    if name in ("", ".", "..", ".pdf"):
        name = f"图片转PDF-{stamp}.pdf"
    out_path = unique_path(dst_dir / name)

    work_root = Path(tempfile.mkdtemp(prefix="img2pdf_"))
    dpi, quality = opt.dpi, opt.quality
    # 图注带按页算好一次：同一页的图必须缩一样多，否则排出来会像没对齐
    bands = page_caption_bands(items, opt)
    per_page = max(1, opt.per_page)
    final_items: list[dict] = []
    placements = []
    pdf_bytes = 0
    cancelled = False
    # 体积阶梯每一轮只换 DPI / JPEG 质量，增强结果可以跨轮复用
    enhance_cache: dict[int, imaging.EnhanceResult] = {}

    try:
        for attempt in range(MAX_ATTEMPTS):
            workdir = work_root / f"pass{attempt}"
            workdir.mkdir(parents=True, exist_ok=True)
            final_items = []          # 每轮重新编码，上一轮的中间文件不参与组装
            round_reports: list[ItemReport | None] = [None] * len(items)

            def run_one(idx: int):
                if cancel is not None and cancel():
                    return idx, None
                band = bands[min(len(bands) - 1, idx // per_page)] if bands else 0.0
                return idx, _process_one(
                    items[idx], opt, dpi, quality, idx, workdir,
                    enhanced_res=enhance_cache.get(idx), cache=enhance_cache,
                    caption_h=band)

            pool = ThreadPoolExecutor(max_workers=max(1, min(workers, len(items))))
            futures = {}
            try:
                # 一张一张提交：取消后不再往队列里塞新任务
                for i in range(len(items)):
                    if cancel is not None and cancel():
                        cancelled = True
                        break
                    futures[pool.submit(run_one, i)] = i
                done = 0
                # 按「谁先算完」收结果，进度不再被最慢那张卡住
                for fut in as_completed(futures):
                    idx, payload = fut.result()
                    done += 1
                    if payload is None:
                        cancelled = True
                        continue
                    rep, pdf_item = payload
                    round_reports[idx] = rep
                    if pdf_item is not None:
                        final_items.append(pdf_item)
                    if progress is not None:
                        progress(done, len(items), rep)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)

            if cancelled:
                break

            report.items = [r for r in round_reports if r is not None]
            if not final_items:
                report.summary = "全部图片都处理失败，没有生成 PDF。"
                return report

            # 组装前把上一次的落位清掉，避免重试轮次叠加
            placements = build_pdf(
                final_items, out_path,
                title=f"图片转PDF 共{len(final_items)}张",
                subject=describe_options(opt), opt=opt,
                annotations=annos_by_page,
            )
            pdf_bytes = out_path.stat().st_size
            report.attempts.append({
                "dpi": dpi, "quality": quality, "bytes": pdf_bytes,
                "pages": len({p.page for p in placements}),
            })

            if opt.target_bytes is None:
                break
            step = _next_step(dpi, quality, pdf_bytes, opt.target_bytes,
                              opt.floor_dpi, opt.color_mode)
            if step is None:
                break
            dpi, quality = step

        report.cancelled = cancelled
        report.pdf_bytes = pdf_bytes
        report.pages = len({p.page for p in placements})
        if cancelled:
            # 取消不留半成品：临时 PDF 删掉，已完成的信息仍写进报告
            out_path.unlink(missing_ok=True)
            report.pdf_path = None
            report.summary = (
                f"已取消：处理了 {len(report.items)} 张，未生成 PDF（原图未改动）。")
            return report

        report.pdf_path = out_path
        report.elapsed_s = round(time.perf_counter() - t_start, 2)
        report.summary = _summary(report, opt)
        if network_audit is not None:
            report.network_attempts = int(getattr(network_audit, "attempts", 0))
        _write_report_json(report, out_path, opt)
        return report
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def _summary(report: BatchReport, opt: ConvertOptions) -> str:
    n = len(report.items)
    failed = len(report.failed)
    pages = report.pages
    mb = report.pdf_bytes / 1048576.0
    parts = [f"共 {n} 张 → {pages} 页 → {mb:.2f} MB"]
    if report.attempts:
        a = report.attempts[-1]
        parts.append(f"末轮 {a['dpi']}dpi/q{a['quality']}，共试 {len(report.attempts)} 轮")
    if opt.target_bytes:
        tgt = opt.target_bytes / 1048576.0
        if report.pdf_bytes <= opt.target_bytes:
            parts.append(f"已达目标 ≤{tgt:.1f} MB")
        else:
            parts.append(f"未达目标 {tgt:.1f} MB：已降到 {opt.floor_dpi}dpi 仍有 {mb:.2f} MB，"
                         f"可调高目标体积、拆成两份、或把拼版张数调大")
    if failed:
        parts.append(f"{failed} 张失败，见下方说明")
    return "；".join(parts)


def _write_report_json(report: BatchReport, pdf_path: Path, opt: ConvertOptions) -> None:
    data = {
        "tool": "图片转PDF工具",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pdf": str(pdf_path),
        "pdf_bytes": report.pdf_bytes,
        "pages": report.pages,
        "total_images": len(report.items),
        "elapsed_s": report.elapsed_s,
        "network_attempts": report.network_attempts,
        "target_bytes": opt.target_bytes,
        "attempts": report.attempts,
        "options": asdict(opt),
        "per_image_tunes": report.per_image_tunes,
        "per_image_places": report.per_image_places,
        "per_image_captions": report.per_image_captions,
        "annotations": report.annotations,
        "items": [asdict(i) for i in report.items],
        "skipped": report.skipped,
    }
    text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        # 每个 PDF 配一份同名报告：同目录里生成第二次也不会把第一次的记录盖掉
        (pdf_path.with_suffix(".report.json")).write_text(text, encoding="utf-8")
    except OSError:
        pass
    try:
        # 保留 report.json 作为「最近一次」的快捷入口（旧版行为，兼容脚本与文档）
        (pdf_path.parent / "report.json").write_text(text, encoding="utf-8")
    except OSError:
        pass  # 报告写不出来不影响 PDF 本身


def describe_options(opt: ConvertOptions) -> str:
    """给报告/界面用的一行参数描述：写的是实际生效的参数，不是预设名。"""
    opt = opt.normalized()
    paper = "自适应纸张" if opt.paper == PAPER_AUTO else opt.paper
    layout = LAYOUT_NAMES.get(opt.layout, opt.layout)
    bits = [f"{paper} {opt.dpi}dpi", f"边距 {opt.margin_mm:g}mm", layout,
            ORIENTATION_NAMES.get(opt.orientation, "自动"),
            f"去阴影{SHADOW_NAMES.get(opt.shadow, opt.shadow)}",
            COLOR_NAMES.get(opt.color_mode, opt.color_mode)]
    if opt.geometry:
        steps = [n for n, on in (("透视", opt.perspective), ("裁边", opt.crop),
                                 ("纠偏", opt.deskew)) if on]
        if steps:
            bits.append("+".join(steps))
    else:
        bits.append("不矫正")
    if opt.target_bytes:
        bits.append(f"目标 ≤{opt.target_bytes / 1048576.0:.1f}MB")
    if opt.page_numbers:
        from .presets import PAGE_FORMATS, PAGE_POSITIONS  # noqa: PLC0415

        bits.append(f"页码 {PAGE_POSITIONS.get(opt.page_number_pos, '底部居中')}"
                    f"（{PAGE_FORMATS.get(opt.page_number_fmt, '{n}').format(n=opt.page_number_start, total='…')} 起）")
    if opt.footer_text:
        bits.append(f"页脚「{opt.footer_text}」")
    if opt.captions:
        bits.append(f"图注 {opt.caption_size:g}pt")
    if opt.bookmarks:
        bits.append("书签")
    return " · ".join(bits)
