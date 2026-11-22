"""图像侧处理：EXIF 转正、找纸、透视矫正、纠偏、去阴影、重采样与编码。

这里只做「把手机拍的照片变成一张像扫描件的图」，不碰 PDF 结构（那是 pdfbuild）。
OpenCV 采用惰性导入：用户关掉「自动矫正」时，冷启动连 cv2 都不加载。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

SUPPORTED_EXT = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".bmp", ".webp",
    ".tif", ".tiff", ".heic", ".heif",
}

# 检测到的四边形面积占比低于这个值就认为「找到的是画面里的杂物，不是那张纸」。
MIN_PAPER_AREA = 0.12
# 透视矫正后的上限，避免把 A4 拉到 6000px 白白变胖。
MAX_WARP_SIDE = 6000


class UnsupportedImage(Exception):
    """格式不支持 / 文件损坏 / 打不开：都要给用户一句能照着做的提示。"""


_cv2_mod = None
_heif_state: bool | None = None


def cv():
    """惰性导入 cv2（约 0.3s）。核心逻辑一律通过这个入口拿模块。"""
    global _cv2_mod
    if _cv2_mod is None:
        import cv2  # noqa: PLC0415 — 故意延迟到这里
        _cv2_mod = cv2
    return _cv2_mod


def ensure_heif() -> bool:
    """装了 pillow-heif 就能读 iPhone 的 HEIC；没装则明确告诉用户怎么办。"""
    global _heif_state
    if _heif_state is None:
        try:
            import pillow_heif  # noqa: PLC0415

            pillow_heif.register_heif_opener()
            _heif_state = True
        except Exception:
            _heif_state = False
    return _heif_state


# ---------------------------------------------------------------- 读图


def open_image(path: str | Path) -> Image.Image:
    """读成 RGB，并按 EXIF 方向转正。失败一律抛 UnsupportedImage（带人话原因）。"""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix not in SUPPORTED_EXT:
        raise UnsupportedImage(f"不支持这个格式（{suffix or '无扩展名'}），请先转成 JPG 或 PNG")
    if suffix in (".heic", ".heif") and not ensure_heif():
        raise UnsupportedImage("HEIC/HEIF 需要额外的解码组件，当前环境不可用，请先转成 JPG")

    try:
        im = Image.open(p)
        im.load()  # 截断文件在这里就暴露，不要等到后面算一半才炸
    except Exception as exc:  # noqa: BLE001 — 各种解码器抛的异常类型不统一
        raise UnsupportedImage(f"打不开这张图：{type(exc).__name__}") from exc

    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass  # EXIF 坏了不影响出片，按原方向走

    return _to_rgb(im)


def _to_rgb(im: Image.Image) -> Image.Image:
    """统一成 8 位 RGB：CMYK / 16 位 / 调色板 / 透明底都要能正确落地。"""
    if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        arr = np.asarray(im).astype(np.float32)
        top = float(arr.max()) if arr.size else 255.0
        arr = arr * (255.0 / top) if top > 255 else arr
        im = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "L")
    if im.mode == "P":
        im = im.convert("RGBA") if "transparency" in im.info else im.convert("RGB")
    if im.mode in ("RGBA", "LA"):
        base = Image.new("RGB", im.size, (255, 255, 255))
        rgba = im.convert("RGBA")
        base.paste(rgba, mask=rgba.split()[-1])
        return base
    if im.mode == "L":
        return im.convert("RGB")
    if im.mode != "RGB":
        im = im.convert("RGB")
    return im


def read_exif_time(path: str | Path):
    """拍摄时间（按 EXIF）；读不到返回 None。仅用于默认排序。"""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            for tag in (36867, 36868, 306):  # DateTimeOriginal / Digitized / DateTime
                raw = exif.get(tag)
                if isinstance(raw, bytes):
                    raw = raw.decode("ascii", "ignore")
                if isinstance(raw, str) and len(raw) >= 19:
                    from datetime import datetime

                    return datetime.strptime(raw[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None
    return None


# ---------------------------------------------------------------- 找纸 / 拉正


def order_quad(pts: np.ndarray) -> np.ndarray:
    """把四个角点排成 左上 / 右上 / 右下 / 左下（不依赖输入顺序）。"""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(d)]
    bl = pts[np.argmax(d)]
    return np.stack([tl, tr, br, bl])


def _quad_ok(quad: np.ndarray) -> bool:
    """四角点必须有限、互异、有面积。

    退化四边形（两点重合、四点共线）喂给 getPerspectiveTransform 会直接抛
    cv2.error，而手机照片里恰好可能出现这种轮廓。宁可放弃这次矫正，
    也不能让整张图判成「处理失败」。
    """
    q = np.asarray(quad, dtype=np.float64).reshape(4, 2)
    if not np.isfinite(q).all():
        return False
    if min(np.linalg.norm(q[i] - q[(i + 1) % 4]) for i in range(4)) < 2.0:
        return False
    # 手写二维叉积：numpy 2.x 起 np.cross 拒绝 2 维向量，这里别踩版本差异
    a, b = q[1] - q[0], q[3] - q[0]
    area = abs(float(a[0] * b[1] - a[1] * b[0]))
    return area > 64.0


def detect_paper(bgr: np.ndarray):
    """找画面里最像「一张纸」的四边形。

    返回 (四角点, 面积占比) 或 (None, 0)。先把图缩到 1200px 再找边，
    4000px 的原图直接在原尺寸跑 Canny 会白等两秒。
    """
    cv2 = cv()
    h, w = bgr.shape[:2]
    if h < 16 or w < 16:
        return None, 0.0
    scale = min(1.0, 1200.0 / max(h, w))
    small = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else bgr

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    area_total = float(small.shape[0] * small.shape[1])
    best, best_area = None, 0.0
    for c in contours:
        peri = cv2.arcLength(c, True)
        if peri < 0.15 * (small.shape[0] + small.shape[1]):
            continue  # 太小，不可能是那张纸
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        area = cv2.contourArea(approx)
        if area > best_area:
            best, best_area = approx.reshape(4, 2), area

    if best is None:
        return None, 0.0
    quad = order_quad(best / scale)
    if not _quad_ok(quad):
        return None, 0.0
    return quad, best_area / area_total


def _ransac_line(pts: np.ndarray, vertical: bool = False,
                 thr: float = 3.0, iters: int = 600,
                 span_frac: float = 0.15) -> tuple[int, float, float]:
    """拟合一条直线，返回 (内点数, a, b)。

    vertical=False 拟 y = a·x + b；True 拟 x = a·y + b（近垂直的边框）。
    两样本跨度必须超过 span_frac·图幅，否则这对样本不足为凭，跳过——
    从相邻两列取两个点拟合出来的线方向是纯随机噪声。
    """
    span = span_frac * (pts[:, 1].max() - pts[:, 1].min() if vertical else
                        pts[:, 0].max() - pts[:, 0].min())
    best = (0, 0.0, 0.0)
    rs = np.random.default_rng(11)   # 固定种子：同图同结果，verify 才能断言
    n = len(pts)
    if n < 8:
        return best
    for _ in range(iters):
        i, j = rs.integers(0, n, 2)
        p1, p2 = pts[i], pts[j]
        c1, c2 = (p1[1], p2[1]) if vertical else (p1[0], p2[0])
        if abs(c2 - c1) < span:
            continue
        if vertical:
            a = (p2[0] - p1[0]) / (p2[1] - p1[1])
            b = p1[0] - a * p1[1]
            d = np.abs(pts[:, 0] - (a * pts[:, 1] + b))
        else:
            a = (p2[1] - p1[1]) / (p2[0] - p1[0])
            b = p1[1] - a * p1[0]
            d = np.abs(pts[:, 1] - (a * pts[:, 0] + b))
        cnt = int((d < thr).sum())
        if cnt > best[0]:
            best = (cnt, float(a), float(b))
    return best


def detect_paper_edges(bgr: np.ndarray, dark_thr: float | None = None):
    """按「四条边框直线」找证件 / 证书这类带深色花边的纸张四角。

    detect_paper 靠 Canny 轮廓 + 凸四边形，营业执照、不动产权证这类
    **花纹边框**会吃大亏：花纹让轮廓坑坑洼洼，approxPolyDP 凑不齐四点，
    或者凑齐了不是凸的——实测一张 1920px 执照照它只找回一个碎块
    （面积占比 0.003，判为没纸）。这里换一条路：

    1. 从四个方向逐行/列扫「最先碰到的目标像素」得到每条边的候选点，
       深色扫法（撞边框外沿）与亮色扫法（撞边框内沿，适合深色背景）
       各出一套候选；
    2. 每条边对两套点集分别做 RANSAC 直线拟合（弯曲的背景阴影内点率低，
       被淘汰；点全贴扫描窗口起点的候选直接扔——外侧没有可分辨结构），
       按内点残差把线推到边框带的外沿，再用「线内侧必须是亮纸面」打分，
       取通过验证里内点最多的一条；
    3. 四线两两相交得四角，收 0.2% 毛边后按 _quad_ok / 面积 / 凸性 /
       边长比校验——四条边不是一张纸的边（比如拍的是半张桌子）就放弃。

    返回 (四角点, 面积占比) 或 (None, 0.0)，口径与 detect_paper 一致。
    """
    cv2 = cv()
    h, w = bgr.shape[:2]
    if h < 60 or w < 60:
        return None, 0.0
    scale = min(1.0, 1200.0 / max(h, w))
    small = (cv2.resize(bgr, None, fx=scale, fy=scale,
                        interpolation=cv2.INTER_AREA) if scale < 1 else bgr)
    sh, sw = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    if dark_thr is None:
        # 阈值跟着**纸面**亮度走（p95 只要有 >5% 的亮纸就能取到），取其
        # 0.65 倍、夹在 70–170。p50 会掉进深色背景里：纸不满画幅的照片
        # 中位是桌面亮度，阈值跟着变 70，连花纹带都分不出来。
        dark_thr = float(np.clip(float(np.percentile(gray, 95)) * 0.65,
                                 70.0, 170.0))
    dark = gray < dark_thr

    # 先锁定纸的范围：纸面是画面里的大亮块。背景若是深色的，它同样
    # 低于阈值——不先把纸圈出来，每列「第一个深色像素」永远先撞背景
    # 外边缘，RANSAC 拟合到的是画布边而不是边框。
    # 用行/列亮占比定范围而不是最大连通域：纸上的标题条、色块会把亮区
    # 切成好几块，最大块只是纸的一部分，包围盒反而偏小。
    bright = (gray >= max(150.0, float(np.percentile(gray, 95)) * 0.72))
    rowf = bright.mean(axis=1)
    colf = bright.mean(axis=0)
    rows = np.nonzero(rowf > 0.10)[0]
    cols = np.nonzero(colf > 0.10)[0]
    if len(rows) < sh * 0.35 or len(cols) < sw * 0.35:
        return None, 0.0          # 没有一块像样的亮纸
    by, y1t = int(rows[0]), int(rows[-1])
    bx, x1t = int(cols[0]), int(cols[-1])
    if (y1t - by) * (x1t - bx) < 0.15 * sw * sh:
        return None, 0.0
    # 范围外扩 6% 再圈定扫描：花纹带可能贴着亮区边缘，不外扩就把边框
    # 裁掉了；外扩混进来的窄条背景由「线内侧必须是亮纸面」验证兜底。
    pad = max(8, int(0.06 * max(y1t - by, x1t - bx)))
    y0b, y1b = max(0, by - pad), min(sh, y1t + pad)
    x0b, x1b = max(0, bx - pad), min(sw, x1t + pad)
    dark[:y0b, :] = False
    dark[y1b:, :] = False
    dark[:, :x0b] = False
    dark[:, x1b:] = False

    # 瘦暗结构提取：背景的大块阴影/深色桌面是「胖」暗区，边框是「瘦」暗
    # 带。腐蚀把比核瘦的暗结构抹掉、再膨胀还原「胖区」的覆盖范围，dark
    # 里剩下的就是瘦暗结构（边框、文字、细线）——对它扫「第一个深色」，
    # 阴影的顶边就再也抢不到边框前面了（真实执照照实测：阴影顶边以 50%
    # 内点率赢下 RANSAC，把线顶到画幅外）。
    ek = max(5, (min(sh, sw) // 130) | 1)
    eroded = cv2.erode(dark.astype(np.uint8),
                       cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ek, ek)))
    fat = cv2.dilate(eroded, cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (ek * 2 + 1, ek * 2 + 1))).astype(bool)
    thin_dark = dark & ~fat

    xs = np.arange(int(sw * 0.04), int(sw * 0.96))
    ys = np.arange(int(sh * 0.04), int(sh * 0.96))
    y_t0, y_t1 = int(sh * .02), int(sh * .30)
    y_b0 = int(sh * .70)
    x_l0, x_l1 = int(sw * .02), int(sw * .30)
    x_r0 = int(sw * .70)
    bright_m = gray >= max(150.0, float(np.percentile(gray, 95)) * 0.72)

    # 「线内侧必须摸得到亮纸面」验证：背景的外边缘同样是一条强直线，
    # RANSAC 分不出谁是谁。实现是自适应深走而非固定条带——固定 5~30px
    # 会被**厚花纹边框**整个吃掉（真实执照边框带 30~40px，条带全落在带
    # 里，p80 永远过不了）；固定深条带又把「隔一段暗区才有纸」的假边放
    # 进来。折中：从线向内逐像素找第一段连续亮纸（≥run_need），步长超过
    # walk 上限算失败；亮纸阈值跟着纸面 p95 走，偏暗的照片（纸面只有
    # 140+）也不错过。6 成采样列摸得到纸才算通过。
    def bright_inside(hline=None, vline=None, inward: int = 1) -> bool:
        paper_thr = max(120.0, float(np.percentile(gray, 95)) * 0.55)
        walk = int(min(sh, sw) * 0.05)
        run_need = max(8, int(min(sh, sw) * 0.012))
        paper = gray >= paper_thr
        found = total = 0
        if hline is not None:
            a, b = hline
            for x in range(int(sw * 0.15), int(sw * 0.85), 8):
                if not 0 <= x < sw:
                    continue
                y = int(a * x + b) + inward * 3
                run = steps = 0
                while 0 <= y < sh and steps < walk:
                    run = run + 1 if paper[y, x] else 0
                    if run >= run_need:
                        found += 1
                        break
                    y += inward
                    steps += 1
                total += 1
        else:
            a, b = vline
            for y in range(int(sh * 0.15), int(sh * 0.85), 8):
                if not 0 <= y < sh:
                    continue
                x = int(a * y + b) + inward * 3
                run = steps = 0
                while 0 <= x < sw and steps < walk:
                    run = run + 1 if paper[y, x] else 0
                    if run >= run_need:
                        found += 1
                        break
                    x += inward
                    steps += 1
                total += 1
        return total > 0 and found >= total * 0.6

    def edge_points(name: str, m: np.ndarray) -> np.ndarray:
        """在一个 0/1 mask 上，从画幅外侧往里扫每列/行的「第一个命中像素」。

        深色扫法（dark）撞到的是边框/纸的外沿，适合浅色桌面；亮色扫法
        （bright_m）撞到的是边框内沿——纸的开始，适合深色桌面：深色背景
        配深色扫法永远先撞背景，一行有效点都没有。
        """
        if name == "top":
            return np.array([[x, int(np.argmax(m[y_t0:y_t1, x])) + y_t0]
                             for x in xs if m[y_t0:y_t1, x].any()], float)
        if name == "bot":
            return np.array(
                [[x, sh - 1 - int(np.argmax(m[sh - 1:y_b0:-1, x]))]
                 for x in xs if m[y_b0:, x].any()], float)
        if name == "left":
            return np.array(
                [[int(np.argmax(m[y, x_l0:x_l1])) + x_l0, y]
                 for y in ys if m[y, x_l0:x_l1].any()], float)
        return np.array(
            [[sw - 1 - int(np.argmax(m[y, sw - 1:x_r0:-1])), y]
             for y in ys if m[y, x_r0:].any()], float)

    def glued(name: str, pts: np.ndarray) -> bool:
        """两类一眼假的点集：

        - 贴扫描窗口起点：mask 从窗口第一行/列就命中——外侧没有可分辨
          结构（深色背景配深色扫法、亮背景配亮扫法都这样），拟出来的必
          然是窗口自身的边；
        - 贴清零边界：bbox 外扩线外的 dark 被清成 False，若点正好压在
          外扩线上，说明线外全是背景（纸不在这一侧），同样是假边。
        """
        med = (float(np.median(pts[:, 1])) if name in ("top", "bot")
               else float(np.median(pts[:, 0])))
        if name == "top":
            return med < y_t0 + 5 or abs(med - y0b) <= 2
        if name == "bot":
            return med > sh - 6 or abs(med - (y1b - 1)) <= 2
        if name == "left":
            return med < x_l0 + 5 or abs(med - x0b) <= 2
        return med > sw - 6 or abs(med - (x1b - 1)) <= 2

    # 每条边两个候选（深色扫法 / 亮色扫法），各自 RANSAC 后用「内侧是亮
    # 纸面」打分，取通过验证里内点最多的。两种背景各有一边会胜出：深色
    # 桌面上深色扫法拟到的是背景暗带（内侧一片黑，被否决），浅色桌面上
    # 亮色扫法拟到的是画幅边缘——都不用猜，验证说话。
    hlines, vlines = {}, {}
    for name, vertical, need in (("top", False, len(xs)),
                                 ("bot", False, len(xs)),
                                 ("left", True, len(ys)),
                                 ("right", True, len(ys))):
        best = (-1, None)
        for m in (thin_dark, bright_m):
            pts = edge_points(name, m)
            if len(pts) < need * 0.35 or glued(name, pts):
                continue
            cnt, a, b = (_ransac_line(pts) if not vertical
                         else _ransac_line(pts, vertical=True))
            if cnt < len(pts) * 0.25:
                continue    # 内点率太低：这条「边」是散点，不是直线
            if vertical:
                res = pts[:, 0] - (a * pts[:, 1] + b)
            else:
                res = pts[:, 1] - (a * pts[:, 0] + b)
            # 外沿校正只看内点的残差：点集混着背景结构时，全点百分位会把
            # 线推出天际（真实执照照实测 b 从 129 被推到 -17）
            inl = np.abs(res) < 3.0
            line = (a, b + float(np.percentile(
                res[inl], 3.0 if name in ("top", "left") else 97.0)))
            inward = 1 if name in ("top", "left") else -1
            ok = (bright_inside(hline=line, inward=inward) if not vertical
                  else bright_inside(vline=line, inward=inward))
            if ok and cnt > best[0]:
                best = (cnt, line)
        if best[1] is None:
            return None, 0.0    # 这条边怎么拟都不是「内侧有纸」的边框
        (vlines if vertical else hlines)[name] = best[1]

    def inter(hl: tuple[float, float], vl: tuple[float, float]):
        ah, bh = hl
        av, bv = vl
        x = (av * bh + bv) / (1 - av * ah)     # y = ah·x + bh 与 x = av·y + bv
        return x, ah * x + bh

    quad = np.array([inter(hlines["top"], vlines["left"]),
                     inter(hlines["top"], vlines["right"]),
                     inter(hlines["bot"], vlines["right"]),
                     inter(hlines["bot"], vlines["left"])], np.float32)
    # 向形心收 0.2%：外沿百分位偶尔越出边框一两个像素，收一点锯齿
    cen = quad.mean(axis=0)
    quad = cen + (quad - cen) * 0.998
    quad = order_quad(quad / scale)
    if not _quad_ok(quad):
        return None, 0.0
    area = float(cv2.contourArea(quad.astype(np.float32)))
    if area < 0.15 * h * w:
        return None, 0.0                       # 太小：拟合到的不是那张纸
    a = quad.astype(np.float64)
    sides = [np.linalg.norm(a[i] - a[(i + 1) % 4]) for i in range(4)]
    if max(sides) / max(1.0, min(sides)) > 3.5:
        return None, 0.0                       # 边长悬殊：不是矩形的纸
    # 凸性：四角按序的叉积必须同号
    signs = []
    for i in range(4):
        p, q = a[i], a[(i + 1) % 4]
        nxt = a[(i + 2) % 4]
        signs.append((q[0] - p[0]) * (nxt[1] - p[1])
                     - (q[1] - p[1]) * (nxt[0] - p[0]))
    if len({s > 0 for s in signs}) > 1:
        return None, 0.0
    return quad, area / (h * w)


def warp_quad(bgr: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """按四角点做透视变换。

    输出尺寸按检测到的边长取，**不强行拉成 A4 比例**——把长条小票拉成 A4
    会把字横向撑胖，反而不像扫描件。比例交给后面的版式环节去适配。
    """
    cv2 = cv()
    if not _quad_ok(quad):
        return bgr
    tl, tr, br, bl = quad
    w = max(float(np.linalg.norm(br - bl)), float(np.linalg.norm(tr - tl)))
    h = max(float(np.linalg.norm(tr - br)), float(np.linalg.norm(tl - bl)))
    if w < 32 or h < 32:
        return bgr
    scale = min(1.0, MAX_WARP_SIDE / max(w, h))
    ow, oh = max(32, int(round(w * scale))), max(32, int(round(h * scale)))
    dst = np.float32([[0, 0], [ow - 1, 0], [ow - 1, oh - 1], [0, oh - 1]])
    try:
        m = cv2.getPerspectiveTransform(quad.astype(np.float32), dst)
        return cv2.warpPerspective(bgr, m, (ow, oh), flags=cv2.INTER_CUBIC,
                                   borderMode=cv2.BORDER_CONSTANT,
                                   borderValue=(255, 255, 255))
    except Exception:  # noqa: BLE001 — 奇异矩阵等退化输入，放弃矫正比丢一张图强
        return bgr


def content_crop(bgr: np.ndarray, pad: float = 0.012):
    """按内容包围盒裁掉四周空白，返回 (图, 是否裁过)。

    只在「背景比内容亮」时动手；整张都是暗调照片（没有白纸可依）时原样返回，
    否则会把照片裁成中间一小块。
    """
    cv2 = cv()
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    if h < 8 or w < 8:
        return bgr, False
    border = np.concatenate([
        gray[:2, :].ravel(), gray[-2:, :].ravel(),
        gray[:, :2].ravel(), gray[:, -2:].ravel(),
    ])
    bg = float(np.median(border))
    if bg < 90:
        return bgr, False
    mask = gray.astype(np.int16) < (bg - 18)
    ratio = float(mask.mean())
    if ratio < 0.004 or ratio > 0.985:
        return bgr, False
    ys, xs = np.nonzero(mask)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    if (x1 - x0) < w * 0.25 or (y1 - y0) < h * 0.25:
        return bgr, False
    px, py = int(w * pad), int(h * pad)
    box = (max(0, x0 - px), max(0, y0 - py), min(w, x1 + px + 1), min(h, y1 + py + 1))
    if box[2] - box[0] < 32 or box[3] - box[1] < 32:
        return bgr, False
    return bgr[box[1]:box[3], box[0]:box[2]], True


def measure_skew(bgr: np.ndarray, max_angle: float = 15.0,
                 strict: bool = False) -> float:
    """估残差倾角（度）。正负约定与 rotate() 一致：把这个角度喂给 rotate 就摆正。

    v1.3.1 起不再「有直线就转」。旧版对一张风景照也会投票：屋顶、塔沿、
    栏杆都是长直线，加权中位数能把它们拧成 8–10° 的「倾斜」，于是用户
    什么都没改，预览里的图却是歪的。

    判据改成「这一组直线像不像纸的边缘」：
      - 直线数量与总长要够（零星几条斜线不算数）；
      - 角度要聚成一堆（标准差小），说明它们真的平行；
      - 这一堆在总长里要占大头。
    不满足就返回 0.0（= 判定这张没歪），宁可不转，也不能把正图转歪。

    strict=True 用于「没检测到纸张」的图：自然照片里 4° 以上的主导斜线
    几乎一定是场景本身（山坡、屋檐、透视），不是手抖，所以门槛更紧、
    角度上限压到 4°。判定不过就老实不动。
    """
    cv2 = cv()
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    scale = min(1.0, 1600.0 / max(h, w))
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    min_len = max(30, int(min(gray.shape) * 0.25))
    lines = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=50,
                            minLineLength=min_len, maxLineGap=10)
    if lines is None:
        return 0.0
    vals, weights = [], []
    # OpenCV 4 返回 (N,1,4)，OpenCV 5 返回 (N,4)：统一摊平成 N×4 再用
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
        if x2 == x1:
            continue
        ang = math.degrees(math.atan2(float(y2 - y1), float(x2 - x1)))
        norm = ((ang + 45.0) % 90.0) - 45.0  # 折到 ±45°，竖线也算进来
        if abs(norm) <= max_angle:
            vals.append(norm)
            weights.append(math.hypot(float(x2 - x1), float(y2 - y1)))
    if len(vals) < (10 if strict else 6):
        return 0.0

    v = np.asarray(vals)
    wt = np.asarray(weights)
    total = float(wt.sum())
    diag = math.hypot(h, w)
    if total < diag * (1.0 if strict else 0.6):
        return 0.0                    # 证据太稀：几根孤立短线撑不起一次旋转

    order = np.argsort(v)
    cw = np.cumsum(wt[order])
    angle = float(v[order][int(np.searchsorted(cw, cw[-1] / 2.0))])

    near = np.abs(v - angle) <= (1.0 if strict else 1.5)
    share = float(wt[near].sum()) / total
    spread = float(np.sqrt(np.average((v[near] - angle) ** 2, weights=wt[near])))
    if share < (0.7 if strict else 0.5) or spread > (0.8 if strict else 1.2):
        return 0.0                    # 方向不聚拢 = 场景里本来就有斜线，不是拍歪
    if strict and abs(angle) > 4.0:
        return 0.0
    return angle


def measure_border_skew(bgr: np.ndarray, max_angle: float = 15.0,
                        band: float = 0.14) -> float:
    """透视矫正后的残差倾角：**只看图四周的纸边，不看内部文字线**。

    为什么不能用 measure_skew：warp 已把「检测到的四边形」拉成正矩形，
    这时图内部的文字线不是对齐基准——纸张有折痕时会整半页翘起来
    （实测执照左半页文字比纸边斜 3°），按文字线纠偏会把本来摆正的
    纸边重新转歪。对齐基准只能是纸边本身。

    做法：把画面中心挖掉，只留四周 band 比例的边框带做 Canny+Hough，
    折到 ±45° 后取长度加权中位数，并要求这堆线确实聚拢（占比、离散度
    双门槛），否则返回 0.0——纸边残差是唯一有资格触发旋转的证据。

    正负约定与 measure_skew / rotate() 一致：返回值直接喂 rotate 就摆正。
    """
    cv2 = cv()
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    scale = min(1.0, 1600.0 / max(h, w))
    if scale < 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    # 挖掉中心，只留边框带：内部文字 / 表格线没有对齐资格
    bw, bh = int(edges.shape[1] * band), int(edges.shape[0] * band)
    edges[bh:edges.shape[0] - bh, bw:edges.shape[1] - bw] = 0
    min_len = max(30, int(min(gray.shape) * 0.25))
    lines = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=50,
                            minLineLength=min_len, maxLineGap=10)
    if lines is None:
        return 0.0
    vals, weights = [], []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
        if x2 == x1:
            continue
        ang = math.degrees(math.atan2(float(y2 - y1), float(x2 - x1)))
        norm = ((ang + 45.0) % 90.0) - 45.0  # 折到 ±45°，竖直的纸边也算进来
        if abs(norm) <= max_angle:
            vals.append(norm)
            weights.append(math.hypot(float(x2 - x1), float(y2 - y1)))
    if len(vals) < 4:
        return 0.0
    v = np.asarray(vals)
    wt = np.asarray(weights)
    total = float(wt.sum())
    if total < math.hypot(*gray.shape) * 0.3:
        return 0.0                    # 边框带里几乎没有长直线：纸边不可见，不动
    order = np.argsort(v)
    cw = np.cumsum(wt[order])
    angle = float(v[order][int(np.searchsorted(cw, cw[-1] / 2.0))])
    near = np.abs(v - angle) <= 1.5
    share = float(wt[near].sum()) / total
    spread = float(np.sqrt(np.average((v[near] - angle) ** 2, weights=wt[near])))
    if share < 0.6 or spread > 1.2:
        return 0.0                    # 边框带的线不聚拢 = 证据不可信，不动
    return angle


def resize_stretch(bgr: np.ndarray, sx: float, sy: float) -> np.ndarray:
    """非等比缩放（引擎内部兼容参数；编辑窗口的自由变换已取消）。

    缩小用 INTER_AREA（避免摩尔纹），放大用 INTER_CUBIC；比例几乎没变时
    原样返回，不白白重采样一遍。
    """
    if abs(sx - 1.0) < 0.005 and abs(sy - 1.0) < 0.005:
        return bgr
    cv2 = cv()
    h, w = bgr.shape[:2]
    nw = max(1, int(round(w * sx)))
    nh = max(1, int(round(h * sy)))
    interp = cv2.INTER_AREA if (nw < w or nh < h) else cv2.INTER_CUBIC
    return cv2.resize(bgr, (nw, nh), interpolation=interp)


def rotate(bgr: np.ndarray, angle: float) -> np.ndarray:
    """绕中心旋转并扩大画布（空白填白），角度小于 0.05° 直接返回原图。

    只供「引擎兼容参数」和角度测量使用；**纠偏不要用它**——扩大画布会
    让图片变尺寸，还会转出一圈白色三角（见 rotate_in_place）。
    """
    if abs(angle) < 0.05:
        return bgr
    cv2 = cv()
    h, w = bgr.shape[:2]
    cx, cy = w / 2.0 - 0.5, h / 2.0 - 0.5
    m = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2.0 - cx
    m[1, 2] += nh / 2.0 - cy
    return cv2.warpAffine(bgr, m, (nw, nh), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))


def rotate_in_place(bgr: np.ndarray, angle: float) -> np.ndarray:
    """纠偏专用：输出画布**严格等于输入画布**，既不外扩也不留白色三角。

    直接按同尺寸 warpAffine 转，四角会露白；把画布扩大到外接矩形再把白角
    裁掉，图又会变大（旧行为：1920×1440 的照片纠 1.9° 变成 1966×1502，
    用户看到「纠偏把图片弄大了，纠偏也就没用了」）。

    这里一次仿射就把两件事做完：旋转的同时按最小倍率 s 略微放大，让
    「输出矩形的四个角」反投影回去都还落在输入矩形内——于是输出的每一个
    像素都来自原图内容，没有需要填充的区域，也没有超出原图尺寸的区域。
    由四角约束解出 s = cos|θ| + max(w/h, h/w)·sin|θ|；θ=0 时 s=1，原样
    返回，不白白重采样。代价是画面边缘被均匀吃掉约 (s-1)/2（小角度 <3%），
    这是「既要摆正、又要尺寸不变、又不要白边」三者唯一能同时满足的做法。
    """
    # 归一化到 ±90°：|θ|>90° 时 cos 变负，放大倍率会 <1，反而缩出白角。
    # 纠偏路径的角度恒 ≤30°，这层是公开函数的输入契约。
    angle = ((float(angle) + 90.0) % 180.0) - 90.0
    if abs(angle) < 0.05:
        return bgr
    cv2 = cv()
    h, w = bgr.shape[:2]
    if h < 2 or w < 2:
        return bgr
    cx, cy = w / 2.0 - 0.5, h / 2.0 - 0.5
    rad = abs(math.radians(angle))
    s = math.cos(rad) + max(w / h, h / w) * math.sin(rad)
    m = cv2.getRotationMatrix2D((cx, cy), angle, s)
    # 灰度 / BGRA 输入也给出正确长度的填充值，不再硬编码 3 通道 BGR
    fill = 255 if bgr.ndim == 2 else (255,) * bgr.shape[2]
    return cv2.warpAffine(bgr, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=fill)


# ---------------------------------------------------------------- 去阴影


SHADOW_KERNEL = {"light": 21, "standard": 35, "strong": 55}


def remove_shadow(bgr: np.ndarray, strength: str = "standard") -> np.ndarray:
    """把「一半亮一半暗」的照明不均拉平，同时不把深色内容洗白。

    照明场用**大核取最大值**（膨胀）估计：纸面是亮的，窗口只要比最大的深色内容
    还大，窗口里的最大值就总是「附近的纸」，于是阴影区也能取到本地的纸面亮度。
    再把纸面拉到整页的高亮水平（p90），深色内容按同一增益放大，仍然是深色。

    走过的两条弯路（都在实测里翻车，记在这里免得下次再试）：
    - 形态学闭运算当背景直接除：整条深色标题会被当成阴影洗成「白底黑边」。
    - 「亮像素当种子 + 大核加权模糊」：种子只出现在亮侧，模糊后照明场退化成常数，
      阴影区压根没被校正。
    """
    if strength not in SHADOW_KERNEL:
        return bgr
    cv2 = cv()
    h, w = bgr.shape[:2]
    scale = min(1.0, 900.0 / max(h, w))
    small = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else bgr
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    short = min(small.shape[:2])
    # 窗口越小，照明场越贴近本地纸面，去阴影越狠；窗口越大越保守。
    # 这一点直觉容易搞反：大窗口会把整片明暗梯度一起平均掉，反而没校正多少。
    frac = {"light": 0.30, "standard": 0.18, "strong": 0.12}.get(strength, 0.18)
    k = max(21, int(short * frac) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    field = cv2.dilate(gray, kernel)          # 纸面亮度包络：窗口内最亮的那个值
    field = cv2.GaussianBlur(field, (0, 0), max(1.0, k / 4.0))  # 抹掉膨胀的方块感与噪点

    target = float(np.percentile(field, 95))  # 整页纸面拉到这个亮度
    gain = target / np.maximum(field.astype(np.float32), 1.0)
    gain = np.clip(gain, 0.5, 4.0)

    if scale < 1:
        gain = cv2.resize(gain, (w, h), interpolation=cv2.INTER_LINEAR)
    out = bgr.astype(np.float32) * gain[:, :, None]
    return np.clip(out, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- 扫描件滤镜


SCAN_MODES = ("color", "scan", "enhance", "gray", "bw")


def scan_whiten(bgr: np.ndarray, *, sharpen: float = 0.5) -> np.ndarray:
    """拍照文档 → 彩色扫描件（doc_filter 的 "scan" 档）。

    三步，每一步都是实测踩出来的（过程记录在各自的注释里）：

    1. **亮度白化**：在 LAB 的 L 通道上估计「纸面背景」，原亮度 ÷ 背景 ×
       255——光照不均、拍照阴影这类大尺度明暗会被除掉，纸面归白。背景
       估计两层：小核 close 填掉折痕暗谷与笔画（窄暗结构），大 σ 高斯
       吸收照明场。divide 有个原理边界，实测记下来：bg 里被 close 填平
       的**窄**折痕它反而「看不见」，会当内容保留——试过「定向替换」
       （把折痕像素提到背景水平），但深度/宽度/平滑度判据分不开「折痕」
       和「证件上的深色装饰边框」（后者更窄更浅，L 空间深度只有十几级），
       上线必漂白内容，弃用。现实里拍照的折痕多是宽缓的明暗带，divide
       足以压平；锐利的窄折痕会留下残迹，属已知边界。
       两个此前的教训，记下来免得再踩：
       - **大核**形态学当背景（核盖住国徽那种大暗斑）：erode 步会把邻近
         花纹边框的暗值传播过去（暗结构间距小于核），divide 一来国徽被
         「归一化到自己的暗底」，整块漂成粉白——所以 close 只用**小核**，
         只负责窄暗结构，大暗斑交给第 2 步的彩色保护兜底；
       - **纯高斯** σ 取短边/8：对又窄又深的折痕压不干净（合成样张实测
         残差 25/43），close 补上这一刀。
    2. **彩色保护**：白化只作用于低饱和度像素（纸面、黑字、水印底纹）；
       高饱和度区域（红章、金徽、金字）的亮度原样保留——亮度拉高而色度
       不动会让深色印刷看起来「褪成粉白」，这里从机制上杜绝。软过渡按
       HSV 饱和度线性混合，避免章边缘出现白圈。
    3. **轻锐化**：白化后笔画边缘发软，加一点 unsharp 恢复利落。

    防呆：整张图都偏暗（边框四边中位亮度 < 60 或亮部 p90 < 130）说明不是
    白纸文档，直接退回「增强」档——风景照被整页拉白比不去阴影严重得多。
    """
    cv2 = cv()
    h, w = bgr.shape[:2]
    if h < 32 or w < 32:
        return bgr
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    border = np.concatenate([gray[:2].ravel(), gray[-2:].ravel(),
                             gray[:, :2].ravel(), gray[:, -2:].ravel()])
    if float(np.median(border)) < 60.0 or float(np.percentile(gray, 90)) < 130.0:
        return doc_filter(bgr, "enhance", sharpen=sharpen)

    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    # 背景估计两层（1/3 尺度算再放大，低频背景缩尺结果几乎一致，快 9 倍）：
    # - close(核≈短边/12)：把窄暗结构（折痕暗谷、文字行）填平到纸面水平。
    #   bg 偏高永远安全——divide 后暗内容保持暗；怕的只是 bg 偏低（内容
    #   被漂白），所以核可以大胆给，填不掉的大暗斑由下面的双保险兜底；
    # - 大 σ 高斯（短边/6）：吸收照明场等大尺度明暗。
    small = cv2.resize(l, (max(2, round(w / 3.0)), max(2, round(h / 3.0))),
                       interpolation=cv2.INTER_AREA)
    k = max(31, (min(small.shape[:2]) // 12) | 1)
    small = cv2.morphologyEx(small, cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(
                                 cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.GaussianBlur(small, (0, 0), max(4.0, min(small.shape[:2]) / 6.0))
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_CUBIC)
    l2 = cv2.divide(l, bg, scale=255)
    # 双保险：白化结果不超过「背景 + 55」。背景被大暗斑拉低的地方上限
    # 自动变低——比核更大的低饱和深色块（黑白插图、贴在文档上的照片）
    # 就算 divide 算出天亮也会被按住，不会整块漂白。
    l2 = np.minimum(l2, bg.astype(np.int16) + 55).astype(np.uint8)

    s = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)[..., 1].astype(np.float32)
    w_color = np.clip((s - 30.0) / 40.0, 0.0, 1.0)          # 0=白化 1=保色
    l3 = (l2.astype(np.float32) * (1.0 - w_color)
          + l.astype(np.float32) * w_color)
    out = cv2.cvtColor(cv2.merge((l3.astype(np.uint8), a, b)),
                       cv2.COLOR_LAB2BGR)
    if sharpen > 0:
        out = cv2.addWeighted(out, 1.0 + sharpen,
                              cv2.GaussianBlur(out, (0, 0), 1.0), -sharpen, 0)
    return out


def doc_filter(bgr: np.ndarray, mode: str = "color", *,
               block: int | None = None, c: float = 12.0,
               sharpen: float = 0.6) -> np.ndarray:
    """把拉正后的照片洗成扫描件的五种观感，输入/输出都是 BGR uint8。

    手机扫描 App 的「原图 / 拍照修复 / 增强 / 灰度 / 黑白」五档，本质就是这里几行：
    - color   : 原样返回
    - scan    : 拍照→彩色扫描件（见下）
    - gray    : 只去色，保留连续层次
    - enhance : CLAHE 局部对比度 + unsharp 提笔画，白底深字但还有层次
    - bw      : 局部自适应阈值，纯白底黑字

    两个不能颠倒的点：
    1. 必须先（可选）去阴影拉平光照，再做二值化，否则半明半暗的纸会把暗的
       半边整片判成黑；局部自适应阈值本身就相当于逐块的背景估计，所以用它
       而不是全局 Otsu。
    2. 二值化要在**全分辨率**上做。若先 fit_within 缩小再阈值，细笔画会在
       LANCZOS 插值里变成灰阶、再被二次阈值吃掉，字就断了。

    阈值窗口随图幅缩放，夹在 25~81px 的奇数区间。下限很关键：窗口若和笔画
    差不多大（例如 600px 图用 15px 窗口、笔画 10px），高斯加权均值会被笔画
    自己拉低，阈值反落到笔画亮度以下，粗标题/暗半边的字会被整片洗成白。
    实测同一张半明半暗纸：窗口 15 时暗侧笔画变白，25 起两侧笔画都保住。
    上限防止 4000px 照片上窗口过大、跟不上纸面的局部明暗变化。
    """
    if mode not in SCAN_MODES or mode == "color":
        return bgr
    cv2 = cv()
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if mode == "scan":
        return scan_whiten(bgr, sharpen=sharpen)
    if mode == "gray":
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    if mode == "bw":
        short = min(gray.shape[:2])
        if block:
            win = max(3, int(block) | 1)
        else:
            win = max(25, min(81, int(round(short / 24.0)) | 1))
        win = max(3, min(win | 1, max(3, short | 1)))  # 奇数、>1、不比图大太多
        out = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, win, c)
        return cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)
    # enhance：先双边滤波压掉传感器噪点，再 CLAHE 拉局部对比度，最后轻锐化。
    # 顺序不能省掉第一步：CLAHE 和 unsharp 都会放大噪点，直接锐化出来的
    # 扫描件既发脏又让 JPEG 体积翻倍（实测 154KB → 330KB）。双边滤波保边，
    # 不会把 2~3px 的笔画糊掉。
    g = cv2.bilateralFilter(gray, 5, 30, 30)
    g = cv2.createCLAHE(clipLimit=1.6, tileGridSize=(8, 8)).apply(g)
    if sharpen > 0:
        g = cv2.addWeighted(g, 1.0 + sharpen,
                            cv2.GaussianBlur(g, (0, 0), 1.0), -sharpen, 0)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


# ---------------------------------------------------------------- 增强主流程


@dataclass
class EnhanceResult:
    image: Image.Image
    perspective: bool = False   # 是否真的做了透视拉正
    cropped: bool = False       # 是否裁过边
    angle: float = 0.0          # 纠偏角度（度），0 = 没转
    shadow: str = "off"
    filter: str = "color"       # 实际生效的扫描件滤镜，给报告/调试看
    notice: str = ""            # 给报告用的一句人话（如「未检测到纸张」）
    steps: list[str] = field(default_factory=list)


def enhance(im: Image.Image, *, geometry: bool = True, perspective: bool = True,
            shadow: str = "standard", crop: bool = True, deskew: bool = True,
            max_angle: float = 15.0, color_mode: str = "color",
            extra_rotate: float = 0.0,
            extra_scale_x: float = 1.0, extra_scale_y: float = 1.0) -> EnhanceResult:
    """几何矫正 + 去阴影 + 扫描件滤镜，输入输出都是 PIL RGB。

    v1.2.0：裁边 / 纠偏 / 透视拉正拆成三个独立开关，用户可以只开其中一项。
    geometry 仍是总开关（False = 三项全关），保留给命令行与旧调用。

    滤镜放在这里而不是编码阶段，是因为预览与出片共用 render_page：
    用户点「预览这张」看到的就是最终观感，不会「看着是彩照、出来是黑白」。
    """
    res = EnhanceResult(image=im, shadow="off", filter=color_mode)
    # 旋转 / 拉伸是引擎内部兼容参数（界面已取消自由变换），不属于「几何矫正」
    # 总开关：就算三项全关也能用。放在自动矫正之后、去阴影之前——拉伸走
    # resize_stretch，旋转转出来的空白区填白（rotate 已是白底），都不再裁边：
    # 裁了会把白衬边裁掉、纸角切齐旋转边，反而不像「转过的纸」。
    extra_rotate = round(max(-180.0, min(180.0, float(extra_rotate))), 1)
    extra_scale_x = max(0.2, min(5.0, float(extra_scale_x)))
    extra_scale_y = max(0.2, min(5.0, float(extra_scale_y)))
    do_rotate = abs(extra_rotate) >= 0.05
    do_scale = abs(extra_scale_x - 1.0) >= 0.005 or abs(extra_scale_y - 1.0) >= 0.005
    need_geom = bool(geometry) and bool(perspective or crop or deskew)
    if not need_geom:
        arr = np.asarray(im.convert("RGB"))[:, :, ::-1].copy()
        if do_scale:
            arr = resize_stretch(arr, extra_scale_x, extra_scale_y)
            res.steps.append(f"拉伸 {extra_scale_x * 100:.0f}%×{extra_scale_y * 100:.0f}%")
        if do_rotate:
            arr = rotate(arr, extra_rotate)
            res.steps.append(f"旋转 {extra_rotate:+.1f}°")
        if shadow != "off":
            arr = remove_shadow(arr, shadow)
            res.shadow = shadow
        arr = doc_filter(arr, color_mode)
        res.image = Image.fromarray(np.ascontiguousarray(arr[:, :, ::-1]))
        return res

    cv2 = cv()
    arr = np.ascontiguousarray(np.array(im)[:, :, ::-1])

    if perspective:
        quad, ratio = detect_paper(arr)
        if quad is None or ratio < MIN_PAPER_AREA:
            # 轮廓法失手了（花纹边框 / 印章把轮廓搅碎）。证件、证书这类
            # 带深色花边的纸还有第二条路：四条边框直线各自拟合再相交。
            quad2, ratio2 = detect_paper_edges(arr)
            if quad2 is not None and ratio2 >= MIN_PAPER_AREA:
                quad, ratio = quad2, ratio2
        if quad is not None and ratio >= MIN_PAPER_AREA:
            warped = warp_quad(arr, quad)
            # warp_quad 遇到退化四边形会原样返回，这时不能谎称做过矫正
            if warped is not arr:
                arr = warped
                res.perspective = True
                res.steps.append("透视矫正")
            else:
                res.notice = "纸张四角不可靠，跳过透视矫正"
        else:
            res.notice = "未检测到纸张，按内容裁边"

    if crop:
        arr, cropped = content_crop(arr, pad=0.008)
        if cropped:
            res.cropped = True
            res.steps.append("裁边")

    if deskew:
        if res.perspective:
            # 透视矫正已把检测到的纸边拉正，此时的对齐基准只能是纸边残差。
            # 内部文字线不能当基准：折痕会让半页纸翘起来（实测执照左半页
            # 文字比纸边斜 3°），按文字纠偏会把正图转歪。
            skew = measure_border_skew(arr, max_angle=max_angle)
        else:
            # 没检测到纸张时收紧判据：风景 / 建筑照片里的斜线是内容，不是拍歪
            skew = measure_skew(arr, max_angle=max_angle, strict=True)
        if abs(skew) >= 0.15:
            # 纠偏只把内容摆正，绝不能改图片尺寸：输出画布严格等于纠偏前，
            # 转出原图矩形的部分直接不显示，也不留白色三角。旧版用扩大画布
            # 的 rotate()，1920×1440 纠 1.9° 会变成 1966×1502，等于纠偏
            # 反而把图撑大了；后面那次 content_crop 也一并删掉——它照样会
            # 偷偷改尺寸，而且这里已经没有白角可裁。
            arr = rotate_in_place(arr, skew)
            res.angle = round(skew, 2)
            res.steps.append(f"纠偏 {skew:+.1f}°")

    if do_scale:
        arr = resize_stretch(arr, extra_scale_x, extra_scale_y)
        res.steps.append(f"拉伸 {extra_scale_x * 100:.0f}%×{extra_scale_y * 100:.0f}%")

    if do_rotate:
        arr = rotate(arr, extra_rotate)
        res.steps.append(f"旋转 {extra_rotate:+.1f}°")

    if shadow != "off":
        arr = remove_shadow(arr, shadow)
        res.shadow = shadow
        level = {"light": "轻", "standard": "标准", "strong": "强"}.get(shadow, shadow)
        res.steps.append(f"去阴影（{level}）")

    if arr.shape[2] == 4:  # 理论上不会走到，防御性一行
        arr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
    arr = doc_filter(arr, color_mode)
    res.image = Image.fromarray(np.ascontiguousarray(arr[:, :, ::-1]))
    return res


# ---------------------------------------------------------------- 重采样与编码


def fit_within(im: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """等比缩到盒子里（不放大）。已经比盒子小的原样返回。"""
    w, h = im.size
    box_w, box_h = max(1, int(box_w)), max(1, int(box_h))
    if w <= box_w and h <= box_h:
        return im
    scale = min(box_w / w, box_h / h)
    return im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def save_page_image(im: Image.Image, path: Path, *, color_mode: str = "color",
                    quality: int = 82) -> Path:
    """按画面效果落盘：彩色 / 增强 / 灰度走 JPEG，黑白走 1 位 PNG。

    doc_filter 已经在全分辨率完成了二值化；这里不能再自适应阈值一次，
    否则 LANCZOS 缩放后的细笔画会被二次窗口吃掉。只需用固定阈值把插值
    产生的灰阶压回 0/255 即可（此刻光照已拉平，全局阈值是安全的）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if color_mode == "bw":
        gray = np.asarray(im.convert("L"))
        bw = np.where(gray >= 160, 255, 0).astype(np.uint8)
        Image.fromarray(bw, mode="L").convert("1").save(path, "PNG", optimize=True)
    else:
        out = im.convert("L") if color_mode in ("gray", "enhance") else im.convert("RGB")
        out.save(path, "JPEG", quality=int(quality), optimize=True, progressive=False,
                 subsampling=0 if quality >= 88 else 2)
    return path
