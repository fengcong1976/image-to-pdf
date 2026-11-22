"""转换参数，以及几组可填进参数的推荐值。

设计取向（v1.2.0 起）：
**默认就是通用，不要求用户先回答「这是什么场景」。** 所有参数都是一等公民、
界面上常驻、随时可改。

v1.3.0 起图形界面已经**没有**「一键填值」按钮：顶部那排场景按钮被用户明确要求
删掉，理由是书、合同、试卷、画册都不属于这几类，让预设替用户做决定反而不通用。
现在 PRESETS 只服务于命令行 `cli.py --preset`、引擎自检与旧报告的参数描述，
它们不再代表「图片必须属于这一类」，也不会锁定任何东西。
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    hint: str          # 一句话说明「点它会把参数改成什么」
    paper: str
    dpi: int
    margin_mm: float
    per_page: int      # 1 / 2 / 4
    layout: str        # "single" | "col"（上下） | "row"（左右） | "grid"
    shadow: str        # "off" | "light" | "standard" | "strong"
    color_mode: str    # "color" | "enhance" | "gray" | "bw"
    quality: int       # JPEG 质量
    auto_landscape: bool
    # 下面四项在通用模式下也完全开放；旧预设沿用「全开」的历史行为
    perspective: bool = True
    crop: bool = True
    deskew: bool = True
    orientation: str = "auto"


# 通用：尽量少改动原图内容（不默认去阴影），任何图片放进来都是等比落到 A4 上。
# 纸张可另选「自适应」，页面跟着图片比例走，长图、横幅、截图都不会被压成一条。
PRESETS: dict[str, Preset] = {
    "general": Preset(
        key="general",
        name="通用",
        hint="通用：不假设你拍的是什么。A4 / 200dpi / 一页一张，只自动裁边纠偏；"
             "下面每一项都能自己改，纸张可选「自适应」让页面跟着图片走",
        paper="A4", dpi=200, margin_mm=12.0,
        per_page=1, layout="single",
        shadow="off", color_mode="color", quality=82,
        auto_landscape=True,
    ),
    # 票据去阴影定「标准」而不是「强」：热敏小票和浅蓝印章在强力档下会被吃掉，
    # 宁可少去一点阴影，也不能让金额看不清（见立项卡风险表）。
    "receipt": Preset(
        key="receipt",
        name="票据报销",
        hint="票据报销（一键填值）：200dpi / 12mm / 去阴影标准，适合发票小票；填完仍可随意改",
        paper="A4", dpi=200, margin_mm=12.0,
        per_page=1, layout="single",
        shadow="standard", color_mode="color", quality=82,
        auto_landscape=False, orientation="portrait",
    ),
    "idcard": Preset(
        key="idcard",
        name="证件材料",
        hint="证件材料（一键填值）：300dpi / 8mm / 左右并排一页两张 / 不去阴影；填完仍可随意改",
        paper="A4", dpi=300, margin_mm=8.0,
        per_page=2, layout="row",
        shadow="off", color_mode="color", quality=90,
        auto_landscape=False, orientation="portrait",
    ),
    "homework": Preset(
        key="homework",
        name="作业文档",
        hint="作业文档（一键填值）：200dpi / 10mm / 横图自动横版 / 去阴影标准；填完仍可随意改",
        paper="A4", dpi=200, margin_mm=10.0,
        per_page=1, layout="single",
        shadow="standard", color_mode="color", quality=82,
        auto_landscape=True,
    ),
}

DEFAULT_PRESET = "general"

# 固定纸张；「自适应」不是固定尺寸，单独一个常量，避免 page_size() 解包 None。
PAPERS = {
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "B5": (176.0, 250.0),
    "Letter": (215.9, 279.4),
}
PAPER_AUTO = "自适应"
PAPER_CHOICES = list(PAPERS) + [PAPER_AUTO]

ORIENTATION_NAMES = {
    "auto": "自动（横图用横版）",
    "portrait": "始终竖版",
    "landscape": "始终横版",
}

SHADOW_NAMES = {
    "off": "关",
    "light": "轻",
    "standard": "标准",
    "strong": "强",
}

# 画面效果四档。字典顺序就是界面下拉顺序；normalized() 用它当白名单，
# signature() 用它当预览缓存 key 的一部分，所以新增一档不用改别处。
# enhance = 手机扫描 App 里的「增强」：局部对比度 + 轻锐化，白底深字但保留层次。
COLOR_NAMES = {
    "color": "彩色",
    "scan": "彩色扫描件（拍照修复）",
    "enhance": "增强（扫描件）",
    "gray": "灰度",
    "bw": "黑白（1 位无损）",
}

LAYOUT_NAMES = {
    "single": "一页一张",
    "col": "一页两张（上下）",
    "row": "一页两张（左右）",
    "grid": "一页四张（2×2）",
}

# 页码位置。键名分两段：上/下 + 左/中/右，界面直接按这个顺序排下拉。
PAGE_POSITIONS = {
    "bottom-center": "底部居中",
    "bottom-right": "右下角",
    "bottom-left": "左下角",
    "top-center": "顶部居中",
    "top-right": "右上角",
    "top-left": "左上角",
}

# 页码文字样式。total 是本份 PDF 的总页数，n 是当前页号（含起始页号）。
PAGE_FORMATS = {
    "n": "{n}",
    "n_total": "{n} / {total}",
    "cn": "第 {n} 页",
    "cn_total": "第 {n} 页 / 共 {total} 页",
}


@dataclass
class ConvertOptions:
    """一次转换的全部参数。UI 与 CLI 都构造这个东西交给核心。

    preset 只是「这组参数最初来自哪个一键预设」的标记，不参与任何算法分支；
    真正决定结果的是下面这些字段。无参构造 == 通用默认。
    """

    preset: str = DEFAULT_PRESET
    paper: str = "A4"
    dpi: int = 200
    margin_mm: float = 12.0
    per_page: int = 1
    layout: str = "single"
    shadow: str = "off"
    geometry: bool = True        # 总开关：False 时裁边/纠偏/透视一律关（兼容旧调用）
    perspective: bool = True     # 透视拉正（找纸四角）
    crop: bool = True            # 内容裁边
    deskew: bool = True          # 纠偏
    max_angle: float = 15.0      # 纠偏最大允许角度
    rotate: float = 0.0          # 旋转（度，-180~180）：引擎兼容参数，界面不再暴露
    scale_x: float = 1.0         # 拉伸（宽，0.2~5.0）：引擎兼容参数
    scale_y: float = 1.0         # 拉伸（高）：引擎兼容参数
    orientation: str = "auto"    # auto / portrait / landscape
    auto_landscape: bool = True  # 旧字段：orientation=auto 时是否横图自动转横版
    color_mode: str = "color"
    quality: int = 82
    target_bytes: int | None = None   # None = 不限制
    floor_dpi: int = 150              # 打印可辨认的地板；96 = 只给人看
    sort_by_exif: bool = True

    # 页码与页脚：整份 PDF 的事，和单张图的修复无关。
    # 默认关——证件、票据这类一两页的成品，页码只是噪音。
    page_numbers: bool = False
    page_number_pos: str = "bottom-center"
    page_number_fmt: str = "cn_total"
    page_number_start: int = 1
    page_number_size: float = 10.0
    footer_text: str = ""             # 页脚一行自定义文字，留空不画

    # 一页多张时格子之间的缝；单张一页用不上，但留着以便统一传递
    gap_mm: float = 4.0

    # 图注：每张图下方的一行小字（默认取文件名清洗后的结果，可逐张改）。
    # 默认关——照片、票据这类内容自己会说话，一行说明反而是噪音；
    # 但合同、病历、施工记录这类「图本身看不出是哪一页」的场景很需要它。
    captions: bool = False
    caption_size: float = 9.0

    # PDF 书签（阅读器侧边栏的大纲）：每张图一项，点了跳到它所在的页。
    # 默认开——它不占版面、不增体积（几十项也就几 KB），几十页的扫描件
    # 全靠它跳转，比在版面上印文件名有用得多。
    bookmarks: bool = True

    def copy(self, **kw) -> "ConvertOptions":
        return replace(self, **kw)

    @classmethod
    def from_preset(cls, key: str, **overrides) -> "ConvertOptions":
        """把某组推荐值铺进参数对象。它只是「填值」，不锁定、不分类。"""
        p = PRESETS.get(key, PRESETS[DEFAULT_PRESET])
        opt = cls(
            preset=p.key, paper=p.paper, dpi=p.dpi, margin_mm=p.margin_mm,
            per_page=p.per_page, layout=p.layout, shadow=p.shadow,
            color_mode=p.color_mode, quality=p.quality,
            auto_landscape=p.auto_landscape, orientation=p.orientation,
            perspective=p.perspective, crop=p.crop, deskew=p.deskew,
            geometry=True,
        )
        return replace(opt, **overrides) if overrides else opt

    def signature(self) -> tuple:
        """影响「图像长什么样」的参数指纹；预览缓存用它当 key。

        故意不含 target_bytes / floor_dpi / sort_by_exif：体积阶梯只在出片时
        反推 DPI，不影响当前设置下的预览画面（界面会另行提示这一点）。
        """
        n = self.normalized()
        return (n.paper, n.dpi, n.margin_mm, n.per_page, n.layout, n.shadow,
                n.geometry, n.perspective, n.crop, n.deskew, n.max_angle,
                n.rotate, n.scale_x, n.scale_y,
                n.orientation, n.auto_landscape, n.color_mode,
                n.quality, n.gap_mm, n.captions, n.caption_size,
                n.page_numbers, n.page_number_pos, n.page_number_fmt,
                n.page_number_start, n.page_number_size, n.footer_text)

    def normalized(self) -> "ConvertOptions":
        """把互相打架的组合理一遍，给核心一个自洽的参数集。"""
        per_page = self.per_page if self.per_page in (1, 2, 4) else 1
        layout = self.layout
        if per_page == 1:
            layout = "single"
        elif per_page == 2 and layout not in ("col", "row"):
            layout = "col"
        elif per_page == 4:
            layout = "grid"
        floor = 96 if self.floor_dpi <= 120 else 150
        dpi = max(96, min(600, int(self.dpi)))
        margin = max(0.0, min(40.0, float(self.margin_mm)))
        quality = max(40, min(95, int(self.quality)))
        paper = self.paper if self.paper in PAPERS or self.paper == PAPER_AUTO else "A4"
        # 自适应页面跟着单张图走，多张拼一页没有唯一比例，强制回到一页一张
        if paper == PAPER_AUTO and per_page != 1:
            per_page, layout = 1, "single"
        shadow = self.shadow if self.shadow in SHADOW_NAMES else "off"
        color = self.color_mode if self.color_mode in COLOR_NAMES else "color"
        target = self.target_bytes
        if target is not None and target <= 0:
            target = None
        orientation = self.orientation if self.orientation in ORIENTATION_NAMES else "auto"
        auto_landscape = self.auto_landscape if orientation == "auto" else (orientation == "landscape")
        geometry = bool(self.geometry)
        # 总开关关掉时，三个细分项一律关；细分项全关时总开关也归 False
        perspective = bool(self.perspective) and geometry
        crop = bool(self.crop) and geometry
        deskew = bool(self.deskew) and geometry
        if not (perspective or crop or deskew):
            geometry = False
        max_angle = max(0.5, min(30.0, float(self.max_angle)))
        rotate = round(max(-180.0, min(180.0, float(self.rotate))), 1)
        scale_x = round(max(0.2, min(5.0, float(self.scale_x))), 3)
        scale_y = round(max(0.2, min(5.0, float(self.scale_y))), 3)
        pos = self.page_number_pos if self.page_number_pos in PAGE_POSITIONS else "bottom-center"
        fmt = self.page_number_fmt if self.page_number_fmt in PAGE_FORMATS else "cn_total"
        start = max(0, min(9999, int(self.page_number_start)))
        size = max(6.0, min(24.0, float(self.page_number_size)))
        gap = max(0.0, min(20.0, float(self.gap_mm)))
        footer = (self.footer_text or "").strip()[:80]
        cap_size = max(6.0, min(18.0, float(self.caption_size)))
        return replace(
            self, per_page=per_page, layout=layout, floor_dpi=floor, dpi=dpi,
            margin_mm=margin, quality=quality, paper=paper, shadow=shadow,
            color_mode=color, target_bytes=target, orientation=orientation,
            auto_landscape=auto_landscape, geometry=geometry,
            perspective=perspective, crop=crop, deskew=deskew,
            max_angle=max_angle, rotate=rotate, scale_x=scale_x, scale_y=scale_y,
            page_number_pos=pos, page_number_fmt=fmt,
            page_number_start=start, page_number_size=size, gap_mm=gap,
            footer_text=footer, caption_size=cap_size,
            captions=bool(self.captions), bookmarks=bool(self.bookmarks),
        )
