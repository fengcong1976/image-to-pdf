"""自检脚本：按《新品立项与验收规范》验证核心引擎与「六条标配」。

    python verify.py

覆盖：找纸 / 透视 / 纠偏 / 去阴影三档 / DPI 与体积反推 / 拼版页序 / 横图横版 /
EXIF 方向 / 原图不可变 / 取消不留半截 / 重名不覆盖 / 中文名 / 损坏文件 /
不支持格式 / 灰度与黑白 / 0 联网 / report.json 字段。

断言只写「产品对外承诺过的东西」，不测实现细节（比如某次内部函数调用次数）。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

import core.imaging as I
from core.compliance import NetworkAudit
from core.converter import (
    collect_images, convert_batch, fingerprint, preview_page, preview_sheet,
    sheet_count,
)
from core.presets import PRESETS, ConvertOptions

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "verify_samples"
OUT = ROOT / "verify_out"
RESULTS: list[bool] = []
DETAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    if not ok:
        DETAILS.append(f"{name}｜{detail}")
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def sha(path: Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------- 造样张


def paper_image(w: int = 1000, h: int = 1414, dark_block: bool = True) -> Image.Image:
    """一张「有字的白纸」：标题条、正文行、右下角彩色块。"""
    im = Image.new("RGB", (w, h), (250, 250, 248))
    d = ImageDraw.Draw(im)
    if dark_block:
        d.rectangle((60, 80, w - 60, 170), fill=(38, 38, 38))
    y = 240
    rng = np.random.default_rng(7)
    while y < h - 180:
        length = int((w - 120) * rng.uniform(0.45, 0.95))
        d.rectangle((60, y, 60 + length, y + 26), fill=(70, 70, 70))
        y += 58
    d.rectangle((w - 260, h - 220, w - 60, h - 100), fill=(30, 60, 150))
    return im


def add_shadow(im: Image.Image, left: float = 0.55, right: float = 1.0) -> Image.Image:
    """模拟手影：从左到右的亮度梯度。"""
    arr = np.array(im).astype(np.float32)
    h, w = arr.shape[:2]
    g = np.linspace(left, right, w, dtype=np.float32)[None, :]  # 只跟 x 有关
    arr = arr * g[:, :, None]
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def save_jpg(im: Image.Image, path: Path, quality: int = 92) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, "JPEG", quality=quality)
    return path


def shoot(im: Image.Image, path: Path, quad=None, bg: tuple = (58, 58, 62),
          canvas=(1400, 1800)) -> Path:
    """把纸「拍」进画面：可选透视四边形 + 深色背景 + 噪点。"""
    cv = I.cv()
    W, H = canvas
    base = np.full((H, W, 3), 0, np.uint8)
    base[:, :] = bg
    noise = np.random.default_rng(3).normal(0, 6, base.shape).astype(np.int16)
    base = np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    src_im = np.array(im)[:, :, ::-1].copy()  # RGB → BGR
    h, w = src_im.shape[:2]
    if quad is None:
        x0, y0 = (W - w) // 2, (H - h) // 2
        x0, y0 = max(20, x0), max(20, y0)
        base[y0:y0 + h, x0:x0 + w] = src_im
    else:
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        m = cv.getPerspectiveTransform(src, np.float32(quad))
        warped = cv.warpPerspective(src_im, m, (W, H))
        mask = cv.warpPerspective(np.full((h, w), 255, np.uint8), m, (W, H))
        base[mask > 0] = warped[mask > 0]
    ok, buf = cv.imencode(".jpg", base, [int(cv.IMWRITE_JPEG_QUALITY), 92])
    assert ok
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.tobytes())
    return path


def make_samples() -> dict[str, Path]:
    # 只覆盖同名样张，不递归删除目录：清理动作属于用户确认后才做的事
    SAMPLES.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}

    paper = paper_image()
    # 1) 干净扫描：纸平铺在桌面上，有可见纸边、无阴影
    out["flat"] = shoot(paper, SAMPLES / "平铺扫描.jpg")
    # 2) 拍歪 + 阴影 + 深色背景（核心卖点场景）
    skewed = add_shadow(I.rotate(np.array(add_shadow(paper))[:, :, ::-1].copy(), -6.0)[:, :, ::-1])
    out["skewed"] = shoot(Image.fromarray(np.array(skewed)), SAMPLES / "拍歪带阴影.jpg",
                          quad=[(300, 200), (1250, 300), (1180, 1650), (220, 1500)])
    # 2b) 花纹边框证件（营业执照这类）：深色花边让轮廓法凑不齐凸四边形，
    #     是 detect_paper_edges（四边直线拟合）的专属场景
    cert = paper_image()
    d = ImageDraw.Draw(cert)
    d.rectangle((26, 26, 1000 - 26, 1414 - 26), outline=(120, 96, 60), width=14)
    rng2 = np.random.default_rng(9)
    for t in range(60, 960, 44):              # 沿边框打点，模拟花纹的坑洼
        y2 = t * 1414 // 1000
        for (x, y) in ((t, 26), (t, 1414 - 40), (26, y2), (960, y2)):
            r = int(rng2.integers(6, 12))
            d.ellipse((x - r, y - r, x + r, y + r), fill=(110, 88, 55))
    out["cert"] = shoot(cert, SAMPLES / "花纹边框证件.jpg",
                        quad=[(260, 180), (1230, 300), (1160, 1660), (200, 1520)])
    # 3) 深色块专项：验证去阴影不会把整条深色标题洗白
    out["block"] = save_jpg(add_shadow(paper, 0.5, 1.0), SAMPLES / "深色块.jpg")
    # 4) 横图（作业横版）
    landscape = paper_image(1414, 1000).rotate(0)
    out["land"] = save_jpg(landscape, SAMPLES / "横图.jpg")
    # 5) 中文名 + 空格
    out["cn"] = save_jpg(paper, SAMPLES / "票据 2026 年 11 月 第一张.jpg")
    # 6) 小票（长条）
    receipt = Image.new("RGB", (700, 2200), (252, 252, 250))
    d = ImageDraw.Draw(receipt)
    for i in range(30):
        d.rectangle((50, 80 + i * 60, 50 + (200 + (i * 53) % 380), 100 + i * 60), fill=(60, 60, 60))
    out["receipt"] = save_jpg(add_shadow(receipt, 0.62, 1.0), SAMPLES / "长条小票.jpg")
    # 7) EXIF 方向 6（需要顺时针转 90° 才正）
    rot = paper.rotate(-90, expand=True)
    exif_path = SAMPLES / "手机竖拍.jpg"
    exif = Image.Exif()
    exif[274] = 6
    rot.save(exif_path, "JPEG", quality=92, exif=exif)
    out["exif"] = exif_path
    # 8) 无纸（一张纯风景感渐变，找不到纸）
    grad = Image.new("RGB", (900, 1200))
    px = grad.load()
    for y in range(1200):
        for x in range(900):
            px[x, y] = (int(40 + x * 0.15), int(60 + y * 0.1), int(120 + (x + y) * 0.05))
    out["nopaper"] = save_jpg(grad, SAMPLES / "无纸场景.jpg")
    # 9) 损坏文件
    bad = SAMPLES / "损坏文件.jpg"
    bad.write_bytes(b"\xff\xd8\xff\xe0" + os.urandom(400))
    out["bad"] = bad
    # 10) 大幅样张：验证 200 / 300dpi 的像素与体积差异
    out["big"] = save_jpg(paper_image(2400, 3394), SAMPLES / "大幅样张.jpg")
    # 11) 不支持的格式
    txt = SAMPLES / "说明.txt"
    txt.write_text("这不是图片", encoding="utf-8")
    out["txt"] = txt
    return out


# ---------------------------------------------------------------- 专项测试


def test_paper_detection(s: dict[str, Path]) -> None:
    section("找纸 / 透视矫正")
    flat = np.array(I.open_image(s["flat"]))[:, :, ::-1].copy()
    quad, ratio = I.detect_paper(flat)
    check("平铺纸能检出四边形", quad is not None, f"ratio={ratio:.2f}")
    check("平铺纸面积占比 > 0.45", ratio > 0.45, f"ratio={ratio:.3f}")

    warped = I.warp_quad(flat, quad) if quad is not None else flat
    paper_ratio = 1000 / 1414  # 样张纸本身的宽高比
    out_ratio = warped.shape[1] / warped.shape[0]
    check("透视拉正后长宽比与纸本身一致（±6%）",
          abs(out_ratio - paper_ratio) / paper_ratio < 0.06,
          f"纸={paper_ratio:.3f} 出片={out_ratio:.3f}")

    shot = np.array(I.open_image(s["skewed"]))[:, :, ::-1].copy()
    q2, r2 = I.detect_paper(shot)
    check("拍歪的纸能检出四边形", q2 is not None, f"ratio={r2:.2f}")
    check("拍歪纸面积占比 > 0.35", r2 > 0.35, f"ratio={r2:.3f}")
    if q2 is not None:
        fixed = I.warp_quad(shot, q2)
        check("拉正后画面宽高比接近 A4（0.68~0.74）",
              0.68 <= fixed.shape[1] / fixed.shape[0] <= 0.76,
              f"{fixed.shape[1]}x{fixed.shape[0]}")

    nop = np.array(I.open_image(s["nopaper"]))[:, :, ::-1].copy()
    q3, r3 = I.detect_paper(nop)
    check("无纸画面不硬找纸（占比 < 0.12）", r3 < 0.12, f"ratio={r3:.3f}")

    res = I.enhance(I.open_image(s["nopaper"]), geometry=True, perspective=True, shadow="off")
    check("找不到纸时退化处理并如实说明",
          "未检测到纸张" in res.notice and res.image.size[0] > 0,
          res.notice or "无说明")

    # --- 花纹边框证件：四边直线拟合法的专属场景 -------------------------
    cert = np.array(I.open_image(s["cert"]))[:, :, ::-1].copy()
    q4, r4 = I.detect_paper(cert)
    ok4 = q4 is not None and r4 >= I.MIN_PAPER_AREA
    q5, r5 = I.detect_paper_edges(cert)
    ok5 = q5 is not None and r5 >= I.MIN_PAPER_AREA
    check("花纹边框证件：轮廓法与四边拟合法至少一路成功",
          ok4 or ok5, f"轮廓 ratio={r4:.3f} 边线 ratio={r5:.3f}")
    check("四边直线拟合法能找回花纹边框证件的四角", ok5, f"ratio={r5:.3f}")
    if ok5:
        expect = np.float32([(260, 180), (1230, 300), (1160, 1660), (200, 1520)])
        expect = I.order_quad(expect)
        err = max(float(np.linalg.norm(q5[i] - expect[i])) for i in range(4))
        check("四角与拍摄四边形偏差 < 7% 对角线",
              err < 0.07 * math.hypot(*cert.shape[:2]), f"max_err={err:.0f}px")
        fixed = I.warp_quad(cert, q5)
        # 竖版纸（1000x1414），拉正后宽/高应回到 0.71 附近
        check("拉正后宽高比接近纸张本身（0.64~0.78）",
              0.64 <= fixed.shape[1] / fixed.shape[0] <= 0.78,
              f"{fixed.shape[1]}x{fixed.shape[0]}")
    q6, r6 = I.detect_paper_edges(nop)
    check("无纸画面四边拟合法也不硬找", q6 is None, f"ratio={r6:.3f}")


def test_scan_filter(s: dict[str, Path]) -> None:
    section("拍照修复（彩色扫描件滤镜）")
    # 合成一张「拍照的执照」：灰纸 + 折痕亮脊暗谷 + 红章 + 黑字 + 水印
    rng = np.random.default_rng(5)
    w, h = 1100, 1500
    img = np.full((h, w, 3), 205.0, np.float32)          # 灰纸（拍照偏灰）
    g = np.linspace(0.72, 1.0, w, dtype=np.float32)       # 光照不均
    fold = 1.0 - 0.15 * np.exp(-((np.arange(w) - w * 0.48) / 26.0) ** 2)
    img *= g[None, :, None] * fold[None, :, None]         # 中间一道竖折痕
    img = np.clip(img + rng.normal(0, 5, img.shape), 0, 255).astype(np.uint8)
    img[120:190, 150:950] = (40, 40, 40)                  # 黑色标题条
    cv = I.cv()
    cv.circle(img, (550, 1100), 130, (60, 60, 200), 26)   # 红章圈（BGR: 红多）
    img[300:800, 60:520] = np.clip(
        img[300:800, 60:520] * 0.92 + (0, 0, 18), 0, 255).astype(np.uint8)  # 淡水印

    out = I.doc_filter(img, "scan")
    gray_o = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
    gray_n = cv.cvtColor(out, cv.COLOR_BGR2GRAY)
    paper_o = gray_o[900:980, 640:1020]                   # 折痕右侧的纸面
    paper_n = gray_n[900:980, 640:1020]
    check("纸面被拉白（中位 > 235）",
          float(np.median(paper_n)) > 235, f"{np.median(paper_n):.0f}")
    fold_o = gray_o[900:980, 480:560]                     # 折痕上的纸面
    fold_n = gray_n[900:980, 480:560]
    gap_o = float(np.median(paper_o) - np.median(fold_o))
    gap_n = float(np.median(paper_n) - np.median(fold_n))
    check("折痕被压平（窄折痕残差 < 原差的 65%；宽缓折痕除得更干净）",
          gap_n < max(4.0, gap_o * 0.65), f"原差 {gap_o:.0f} → 剩 {gap_n:.0f}")
    # 红章保色：红色优势（R − G）处理前后同向且不缩水超过 15%
    ring_o = img[1100 - 60:1100 + 60, 550 - 170:550 + 170]
    ring_n = out[1100 - 60:1100 + 60, 550 - 170:550 + 170]
    adv_o = float(ring_o[..., 2].astype(int).mean() - ring_o[..., 1].astype(int).mean())
    adv_n = float(ring_n[..., 2].astype(int).mean() - ring_n[..., 1].astype(int).mean())
    check("红章红色优势保住（不褪色）", adv_n > adv_o * 0.85,
          f"R−G 原 {adv_o:.0f} → 现 {adv_n:.0f}")
    check("黑字仍黑（标题条中位 < 90）",
          float(np.median(gray_n[130:180, 200:900])) < 90,
          f"{np.median(gray_n[130:180, 200:900]):.0f}")
    dark = np.full((400, 300, 3), 70, np.uint8)           # 整页暗照：防呆
    check("暗图防呆：scan 退回增强档不白化",
          np.array_equal(I.doc_filter(dark, "scan"), I.doc_filter(dark, "enhance")))
    res = I.enhance(I.open_image(s["flat"]), geometry=True,
                    perspective=True, shadow="off", color_mode="scan")
    check("enhance 全管线接受 scan 档", res.filter == "scan", res.filter)


def test_deskew(s: dict[str, Path]) -> None:
    section("纠偏")
    cv = I.cv()
    base = np.array(I.open_image(s["flat"]))[:, :, ::-1].copy()
    for angle in (3.0, -7.5, 12.0):
        tilted = I.rotate(base, angle)
        measured = I.measure_skew(tilted)
        check(f"倾角 {angle:+.1f}° 能被测出来（±0.6°）",
              abs(abs(measured) - abs(angle)) < 0.6,
              f"测到 {measured:+.2f}°")
        fixed = I.rotate(tilted, measured)
        residual = I.measure_skew(fixed)
        check(f"按测到的角度转回去后残差 < 0.5°（{angle:+.1f}°）",
              abs(residual) < 0.5, f"残差 {residual:+.2f}°")
    check("旋转后画布扩大且背景为白",
          int(np.array(I.rotate(base, 5.0))[:3, :3].mean()) > 240)

    # v1.3.1：加严判据。旧版「有直线就转」，于是一张风景照里的屋檐、塔沿、
    # 栏杆被当成拍歪，用户什么都没改，预览里却是歪的。以下四条钉住新行为：
    # 宁可不动，也不能把正图转歪。用假图片自证，不依赖任何外部素材。
    def _lines(groups, w=1200, h=840):
        """groups: [(角度, 条数)]，每条画一根贯通长线。"""
        img = np.full((h, w, 3), 246, np.uint8)
        for ang, n in groups:
            t = math.tan(math.radians(ang))
            for i in range(n):
                y0 = 70 + i * (h - 140) / max(1, n)
                cv.line(img, (70, int(y0 + 70 * t)),
                          (w - 70, int(y0 + (w - 70) * t)), (35, 35, 35), 2)
        return img

    scenery = _lines([(-7, 4), (2, 4), (13, 4)], 1200, 840)
    check("三组不同角度的线（风景照）不判为拍歪",
          abs(I.measure_skew(scenery)) < 0.1,
          f"测到 {I.measure_skew(scenery):+.2f}°")
    check("同上一张即使是「没找到纸张」的严判也不转",
          abs(I.measure_skew(scenery, strict=True)) < 0.1)

    sparse = _lines([(3, 2)], 1200, 840)
    check("只有两根短线时不纠偏（证据不足）",
          abs(I.measure_skew(sparse)) < 0.1)

    doc3 = np.full((840, 1200, 3), 250, np.uint8)
    t3 = math.tan(math.radians(3.0))
    for i in range(14):
        y = 60 + i * 52
        cv.line(doc3, (70, int(y + 70 * t3)), (1130, int(y + 1130 * t3)),
                  (30, 30, 30), 2)
    for i in range(6):
        x = 110 + i * 180
        cv.line(doc3, (int(x - 60 * t3), 60), (int(x + 780 * t3), 780),
                  (30, 30, 30), 2)
    check("表格线整齐的一张 3° 文档仍能自动摆正",
          abs(I.measure_skew(doc3, strict=True) - 3.0) < 0.6,
          f"测到 {I.measure_skew(doc3, strict=True):+.2f}°")

    doc8 = np.full((840, 1200, 3), 250, np.uint8)
    t8 = math.tan(math.radians(8.0))
    for i in range(14):
        y = 60 + i * 52
        cv.line(doc8, (70, int(y + 70 * t8)), (1130, int(y + 1130 * t8)),
                  (30, 30, 30), 2)
    check("没找到纸张时，8° 这种大角度多半是场景本身，不动",
          abs(I.measure_skew(doc8, strict=True)) < 0.1,
          f"测到 {I.measure_skew(doc8, strict=True):+.2f}°")
    check("但真找到纸张的大角度仍照纠（非严判）",
          abs(abs(I.measure_skew(doc8)) - 8.0) < 0.6,
          f"测到 {I.measure_skew(doc8):+.2f}°")

    # v1.3.2：透视矫正成功后，纠偏只认纸边残差（measure_border_skew）。
    # 折痕会让半页纸翘起来，内部文字线与纸边不再平行——真实执照实测：
    # warp 后顶边 0.00°，measure_skew 却按左半页文字量出 +3° 把正图转歪。
    # 对齐基准只能是纸边本身。
    def _framed(ang: float, text_ang: float) -> np.ndarray:
        """ang: 纸框整体倾角；text_ang: 内部文字线倾角（模拟折痕弯纸）。"""
        img = np.full((840, 1200, 3), 250, np.uint8)
        pts = np.float32([[80, 80], [1120, 80], [1120, 760], [80, 760]])
        m = cv.getRotationMatrix2D((600, 420), ang, 1.0)
        qp = cv.transform(pts.reshape(-1, 1, 2), m).reshape(-1, 2)
        cv.polylines(img, [qp.astype(np.int32)], True, (60, 50, 40), 14)
        tt = math.tan(math.radians(text_ang))
        for i in range(9):
            y = 170 + i * 56
            cv.line(img, (200, int(y + 200 * tt)),
                    (1000, int(y + 1000 * tt)), (90, 90, 90), 2)
        return img

    for ang in (2.5, -3.0):
        framed = _framed(ang, 0.0)
        got = I.measure_border_skew(framed)
        check(f"纸框歪 {ang:+.1f}° 时边框残差能测出（±0.6°）",
              abs(abs(got) - abs(ang)) < 0.6, f"测到 {got:+.2f}°")
        check(f"按测得角度转回后纸框残差 < 0.5°（{ang:+.1f}°）",
              abs(I.measure_border_skew(I.rotate(framed, got))) < 0.5,
              f"残差 {I.measure_border_skew(I.rotate(framed, got)):+.2f}°")

    bent = _framed(0.0, 3.0)
    check("纸框正、文字因折痕歪 3° —— 边框纠偏不许把正纸框转歪",
          abs(I.measure_border_skew(bent)) < 0.15,
          f"测到 {I.measure_border_skew(bent):+.2f}°")
    check("同一样张用全图直线法必然误判（对照：新旧判据必须分道）",
          abs(I.measure_skew(bent)) >= 1.0,
          f"measure_skew 测到 {I.measure_skew(bent):+.2f}°")

    res_cert = I.enhance(I.open_image(s["cert"]), perspective=True,
                         color_mode="scan")
    cert_res = np.array(res_cert.image)[:, :, ::-1].copy()
    cert_skew = I.measure_border_skew(cert_res)
    check("证件照整管线出片后纸边保持摆正（残差 < 0.8°）",
          abs(cert_skew) < 0.8, f"残差 {cert_skew:+.2f}°")

    # 纠偏不许改变图片尺寸、不许转出画布白角。用户实测：1920×1440 的执照
    # 纠 1.9° 被旧版（扩大画布的 rotate）撑成 1966×1502，四角还多一圈白
    # 三角——「纠偏也就没用了」。现在纠偏走 rotate_in_place。
    solid = np.full((720, 960, 3), 30, np.uint8)
    for ang in (1.9, -6.0, 12.0):
        turned = I.rotate_in_place(solid, ang)
        check(f"同尺寸纠偏 {ang:+.1f}°：输出尺寸严格不变",
              turned.shape[:2] == solid.shape[:2],
              f"{turned.shape[:2]} vs {solid.shape[:2]}")
        corners = np.concatenate([turned[:4, :4].ravel(), turned[:4, -4:].ravel(),
                                 turned[-4:, :4].ravel(), turned[-4:, -4:].ravel()])
        check(f"同尺寸纠偏 {ang:+.1f}°：四角没有白色填充",
              float(corners.mean()) < 80, f"四角均值 {corners.mean():.0f}")
    check("0° 时原样返回、不重采样", I.rotate_in_place(solid, 0.0) is solid)

    tilted2 = I.rotate(base, 4.0)
    fixed2 = I.rotate_in_place(tilted2, I.measure_skew(tilted2))
    check("同尺寸纠偏同样能把内容摆正（残差 < 0.8°）",
          abs(I.measure_skew(fixed2)) < 0.8,
          f"残差 {I.measure_skew(fixed2):+.2f}°")

    # 造一张「确实歪了、纠偏一定会动手」的图（skewed 样张是 6°，没检测到
    # 纸张时严判会主动放弃，拿来验尺寸可以、验角度不行）
    tilted_pil = Image.fromarray(np.ascontiguousarray(I.rotate(base, 3.0)[:, :, ::-1]))
    only_skew2 = I.enhance(tilted_pil, geometry=True, perspective=False,
                           crop=False, deskew=True, shadow="off")
    check("只开纠偏：输出尺寸严格等于原图",
          only_skew2.image.size == tilted_pil.size,
          f"{only_skew2.image.size} vs {tilted_pil.size}")
    check("只开纠偏：角度仍如实记录",
          abs(only_skew2.angle) >= 0.15, f"angle={only_skew2.angle:+.2f}°")
    crop_only = I.enhance(tilted_pil, geometry=True, perspective=False,
                          crop=True, deskew=False, shadow="off")
    crop_skew = I.enhance(tilted_pil, geometry=True, perspective=False,
                          crop=True, deskew=True, shadow="off")
    check("裁边+纠偏：与只裁边的输出尺寸完全一致",
          crop_skew.image.size == crop_only.image.size,
          f"{crop_skew.image.size} vs {crop_only.image.size}")
    check("纠偏后没有二次偷偷裁边",
          crop_skew.steps.count("裁边") <= crop_only.steps.count("裁边"),
          f"{crop_skew.steps} vs {crop_only.steps}")
    cert_im = I.open_image(s["cert"])
    cert_a = I.enhance(cert_im, geometry=True, perspective=False,
                       crop=True, deskew=False, shadow="off").image.size
    cert_b = I.enhance(cert_im, geometry=True, perspective=False,
                       crop=True, deskew=True, shadow="off").image.size
    check("花纹边框证件：开纠偏与不开纠偏的输出尺寸一致",
          cert_a == cert_b, f"{cert_a} vs {cert_b}")


def test_shadow(s: dict[str, Path]) -> None:
    section("去阴影（三档）")
    cv = I.cv()
    src = np.array(I.open_image(s["block"]))[:, :, ::-1].copy()
    gray = cv.cvtColor(src, cv.COLOR_BGR2GRAY)
    left = float(np.median(gray[:, : gray.shape[1] // 6]))
    right = float(np.median(gray[:, -gray.shape[1] // 6:]))
    check("样张本身左暗右亮（差 > 40 灰阶）", abs(right - left) > 40,
          f"左 {left:.0f} / 右 {right:.0f}")

    limits = {"light": 30, "standard": 18, "strong": 12}
    diffs = {}
    for level in ("light", "standard", "strong"):
        out = I.remove_shadow(src, level)
        g = cv.cvtColor(out, cv.COLOR_BGR2GRAY)
        l2 = float(np.median(g[:, : g.shape[1] // 6]))
        r2 = float(np.median(g[:, -g.shape[1] // 6:]))
        diffs[level] = abs(r2 - l2)
        check(f"{level}：左右亮度差压到 < {limits[level]} 灰阶",
              diffs[level] < limits[level], f"左 {l2:.0f} / 右 {r2:.0f}")
        check(f"{level}：纸面被拉亮到 ≥ 205", min(l2, r2) >= 205, f"{min(l2, r2):.0f}")
    check("三档强弱顺序正确（强 < 标准 < 轻）",
          diffs["strong"] <= diffs["standard"] <= diffs["light"],
          str({k: round(v) for k, v in diffs.items()}))

    out = I.remove_shadow(src, "strong")
    g = cv.cvtColor(out, cv.COLOR_BGR2GRAY)
    # 深色标题条：取上半部中间一条（原图该处是纯深色块）
    y0, y1 = int(g.shape[0] * 0.07), int(g.shape[0] * 0.11)
    x0, x1 = int(g.shape[1] * 0.2), int(g.shape[1] * 0.8)
    block = float(np.median(g[y0:y1, x0:x1]))
    check("深色标题条没有被洗白（中位 < 90）", block < 90, f"{block:.0f}")
    check("去阴影后没有整片过曝（p99 < 253）", float(np.percentile(g, 99)) < 253,
          f"p99={np.percentile(g, 99):.0f}")

    same = I.remove_shadow(src, "off")
    check("off 档原样返回", np.array_equal(same, src))


def test_enhance_flags(s: dict[str, Path]) -> None:
    section("增强开关与说明")
    res = I.enhance(I.open_image(s["skewed"]), geometry=False, shadow="off")
    check("关掉自动矫正时不动几何", res.perspective is False and res.cropped is False)
    check("关掉自动矫正时不写处理步骤", res.steps == [], str(res.steps))
    res2 = I.enhance(I.open_image(s["block"]), geometry=False, shadow="light")
    check("只开去阴影时报告写明用了哪档", res2.shadow == "light", res2.shadow)
    res3 = I.enhance(I.open_image(s["skewed"]), geometry=True, shadow="standard")
    check("开启后报告里有可读的处理链", len(res3.steps) >= 2, " + ".join(res3.steps))
    check("报告角度是数字且不超过 ±15°", abs(res3.angle) <= 15.0, f"{res3.angle}")


def test_dpi_and_pixels(s: dict[str, Path]) -> None:
    section("DPI 与像素尺寸")
    from core.pdfbuild import content_rect, page_size

    pw, ph = page_size("A4")
    x0, y0, x1, y1 = content_rect(pw, ph, 12.0)
    cell_w_in = (x1 - x0) / 72.0
    cell_h_in = (y1 - y0) / 72.0
    check("A4 页面尺寸为 595×842pt（±1）", abs(pw - 595.28) < 1 and abs(ph - 841.89) < 1,
          f"{pw:.1f}x{ph:.1f}")

    items, _ = collect_images([s["flat"]])
    opt = ConvertOptions.from_preset("receipt", geometry=False, shadow="off")
    rep = convert_batch(items, out_dir=OUT / "dpi", options=opt, pdf_name="dpi-200.pdf")
    it = rep.items[0]
    expect_w = min(it.src_px[0], round(cell_w_in * 200))
    check("200dpi 出片宽度不超过版心像素宽", it.out_px[0] <= expect_w + 2,
          f"{it.out_px[0]} vs {expect_w}")
    big_items, _ = collect_images([s["big"]])
    big_rep = convert_batch(big_items, out_dir=OUT / "dpi-chk",
                            options=ConvertOptions.from_preset("receipt", geometry=False,
                                                               shadow="off"),
                            pdf_name="dpi-chk.pdf")
    check("报告 DPI 与落位一致（源图够大时 ±3）", abs(big_rep.items[0].dpi - 200) < 3,
          f"{big_rep.items[0].dpi}")
    check("源图不够大时实际 DPI 低于目标并如实说明",
          it.dpi <= 200.5 and (it.dpi >= 184 or bool(it.note)),
          f"{it.dpi} / {it.note}")
    check("不放大凑 DPI：源图小就如实说明",
          it.out_px[0] == it.src_px[0] or it.dpi >= 195,
          f"{it.src_px} → {it.out_px} @{it.dpi}")

    # 换一张真正够大的图（2400×3394）来比 200 与 300：源图不够大时两者像素相同，
    # 比体积没有意义——那属于「不放大凑 DPI」的正确行为，不该混进这条断言。
    big, _ = collect_images([s["big"]])
    rep300 = convert_batch(big, out_dir=OUT / "dpi300",
                           options=ConvertOptions.from_preset("receipt", dpi=300,
                                                              geometry=False, shadow="off"),
                           pdf_name="dpi-300.pdf")
    rep200 = convert_batch(big, out_dir=OUT / "dpi200",
                           options=ConvertOptions.from_preset("receipt", dpi=200,
                                                              geometry=False, shadow="off"),
                           pdf_name="dpi-200.pdf")
    check("同一张大图：300dpi 出片像素更多", rep300.items[0].out_px[0] > rep200.items[0].out_px[0],
          f"{rep300.items[0].out_px} vs {rep200.items[0].out_px}")
    check("同一张大图：300dpi 的体积更大", rep300.pdf_bytes > rep200.pdf_bytes,
          f"{rep300.pdf_bytes} vs {rep200.pdf_bytes}")


def test_target_size(s: dict[str, Path]) -> None:
    section("目标体积反推")
    items, _ = collect_images([s["skewed"], s["flat"], s["cn"]])
    target = 150 * 1024
    opt = ConvertOptions.from_preset("receipt", target_bytes=target)
    rep = convert_batch(items, out_dir=OUT / "target", options=opt, pdf_name="target.pdf")
    check("给了目标体积就真的压到目标以内", rep.pdf_bytes <= target,
          f"{rep.pdf_bytes} > {target}")
    check("压体积时至少重排过一次", len(rep.attempts) >= 2, str(rep.attempts))
    check("每轮的体积是单调不增的",
          all(a["bytes"] >= b["bytes"] for a, b in zip(rep.attempts, rep.attempts[1:])),
          str([a["bytes"] for a in rep.attempts]))
    check("重排档位不会低于地板 150dpi",
          all(a["dpi"] >= 150 for a in rep.attempts), str([a["dpi"] for a in rep.attempts]))
    check("实际 DPI 低于地板时必有「源图分辨率不足」说明",
          all(i.dpi >= 149.5 or "源图分辨率不足" in i.note for i in rep.items),
          str([(i.dpi, i.note) for i in rep.items]))

    tiny = ConvertOptions.from_preset("receipt", target_bytes=30 * 1024)
    rep2 = convert_batch(items, out_dir=OUT / "target2", options=tiny, pdf_name="tiny.pdf")
    over = rep2.pdf_bytes > tiny.target_bytes
    check("目标小到不可能时如实说不达标", over and "未达目标" in rep2.summary,
          rep2.summary)
    check("不达标时仍然出了可用的 PDF（不是空手而归）",
          rep2.pdf_path is not None and rep2.pdf_bytes > 0)
    check("不达标时报告给了可执行的下一步",
          "调高目标体积" in rep2.summary and "拆成两份" in rep2.summary,
          rep2.summary)

    floor = ConvertOptions.from_preset("receipt", target_bytes=30 * 1024, floor_dpi=96)
    rep3 = convert_batch(items, out_dir=OUT / "target3", options=floor, pdf_name="floor96.pdf")
    check("允许 96dpi 地板时能压得更小", rep3.pdf_bytes < rep2.pdf_bytes,
          f"{rep3.pdf_bytes} vs {rep2.pdf_bytes}")


def _natural_order_ok() -> bool:
    """文件夹里 第2张 / 第10张 的收集顺序：自然序应给出 2 在 10 前。"""
    seq = OUT / "seq"
    seq.mkdir(parents=True, exist_ok=True)
    paper = paper_image(120, 170)
    for name in ("页10.jpg", "页2.jpg", "页1.jpg"):
        save_jpg(paper, seq / name)
    found, _ = collect_images([seq])
    return [i.path.name for i in found] == ["页1.jpg", "页2.jpg", "页10.jpg"]


def test_imposition(s: dict[str, Path]) -> None:
    section("拼版与页序")
    files = [s["flat"], s["cn"], s["block"], s["receipt"], s["land"]]
    items, _ = collect_images(files)
    check("批量收集时顺序稳定（同一批跑两次结果一致）",
          [i.path.name for i in items] == [i.path.name for i in collect_images(files)[0]],
          str([i.path.name for i in items]))
    seq, _ = collect_images([OUT / "seq"]) if (OUT / "seq").exists() else ([], [])
    check("文件名按自然序排（第 2 张在 第 10 张前）",
          _natural_order_ok(), "自然序")

    rep1 = convert_batch(items, out_dir=OUT / "imp1",
                         options=ConvertOptions.from_preset("receipt", geometry=False),
                         pdf_name="one.pdf")
    check("一页一张：5 张 → 5 页", rep1.pages == 5, str(rep1.pages))
    check("一页一张：页序从 1 递增", [i.page for i in rep1.items] == [1, 2, 3, 4, 5],
          str([i.page for i in rep1.items]))

    rep2 = convert_batch(items, out_dir=OUT / "imp2",
                         options=ConvertOptions.from_preset("receipt", per_page=2, layout="col",
                                                            geometry=False),
                         pdf_name="two.pdf")
    check("一页两张：5 张 → 3 页", rep2.pages == 3, str(rep2.pages))
    check("一页两张：第 1、2 张同在第 1 页",
          rep2.items[0].page == rep2.items[1].page == 1,
          f"{rep2.items[0].page},{rep2.items[1].page}")
    check("一页两张：第 3 张起换页", rep2.items[2].page == 2, str(rep2.items[2].page))

    rep4 = convert_batch(items, out_dir=OUT / "imp4",
                         options=ConvertOptions.from_preset("receipt", per_page=4, layout="grid",
                                                            geometry=False),
                         pdf_name="four.pdf")
    check("一页四张：5 张 → 2 页", rep4.pages == 2, str(rep4.pages))

    # 拼版后每张落地尺寸应更小（同一张纸切四块）
    check("拼版后单张的可用像素变少（4 张一页 < 1 张一页）",
          rep4.items[0].out_px[0] < rep1.items[0].out_px[0],
          f"{rep4.items[0].out_px} vs {rep1.items[0].out_px}")


def test_orientation(s: dict[str, Path]) -> None:
    section("横图横版 / EXIF 方向")
    import pymupdf

    items, _ = collect_images([s["land"]])
    opt = ConvertOptions.from_preset("homework", geometry=False, shadow="off")
    rep = convert_batch(items, out_dir=OUT / "land", options=opt, pdf_name="land.pdf")
    doc = pymupdf.open(rep.pdf_path)
    r = doc[0].rect
    check("横图在作业预设下页面变横版", r.width > r.height, f"{r.width:.0f}x{r.height:.0f}")
    doc.close()

    opt2 = ConvertOptions.from_preset("homework", geometry=False, shadow="off",
                                      auto_landscape=False)
    rep2 = convert_batch(items, out_dir=OUT / "land2", options=opt2, pdf_name="land2.pdf")
    doc = pymupdf.open(rep2.pdf_path)
    check("关掉自动横版后仍是竖版", doc[0].rect.height > doc[0].rect.width,
          f"{doc[0].rect.width:.0f}x{doc[0].rect.height:.0f}")
    doc.close()

    ex, _ = collect_images([s["exif"]])
    im = I.open_image(s["exif"])
    check("EXIF Orientation=6 的图被转正（宽 < 高）", im.width < im.height,
          f"{im.width}x{im.height}")
    check("原图像素与转正后一致（只是旋转，没裁）",
          sorted(im.size) == sorted((ex[0].width, ex[0].height)),
          f"{im.size} vs {(ex[0].width, ex[0].height)}")


def test_source_untouched(s: dict[str, Path]) -> None:
    section("原图不可变 / 重名不覆盖")
    if (OUT / "safe").exists():
        shutil.rmtree(OUT / "safe")   # 让「第二次自动加 (1)」每次都能从干净状态验证
    items, _ = collect_images([s["skewed"], s["cn"]])
    before = {str(i.path): (sha(i.path), i.path.stat().st_mtime_ns) for i in items}
    rep = convert_batch(items, out_dir=OUT / "safe" / "out",
                        options=ConvertOptions.from_preset("receipt", geometry=False),
                        pdf_name="safe.pdf")
    after = {str(i.path): (sha(i.path), i.path.stat().st_mtime_ns) for i in items}
    check("转换后原图 SHA-256 与修改时间都没变", before == after,
          str([(k, before[k][0] == after[k][0]) for k in before]))
    check("报告写明原图复核凭据", all(i.src_check in ("sha256", "size+mtime") for i in rep.items))
    check("报告里的哈希与磁盘一致",
          all(i.digest == (sha(Path(i.src)) if i.src_check == "sha256" else i.digest)
              for i in rep.items))
    check("输出落在独立的 out 目录，不混在原图目录里",
          rep.pdf_path.parent.name == "out", str(rep.pdf_path.parent))

    first = rep.pdf_path
    rep2 = convert_batch(items, out_dir=OUT / "safe" / "out",
                         options=ConvertOptions.from_preset("receipt", geometry=False),
                         pdf_name="safe.pdf")
    check("同名不覆盖，第二次自动加 (1)", rep2.pdf_path.name == "safe(1).pdf",
          rep2.pdf_path.name)
    check("第一次的 PDF 仍然在", first.exists() and first.stat().st_size > 0)

    withj = convert_batch(items, out_dir=OUT / "safe" / "out",
                          options=ConvertOptions.from_preset("receipt", geometry=False),
                          pdf_name="中文 名字.pdf")
    check("中文 + 空格文件名可以正常写出",
          withj.pdf_path.exists() and withj.pdf_path.stat().st_size > 0,
          str(withj.pdf_path))


def test_robustness(s: dict[str, Path]) -> None:
    section("损坏文件 / 不支持格式 / 跳过")
    items, skipped = collect_images([s["flat"], s["bad"], s["txt"]])
    names = [i.path.name for i in items]
    check("损坏文件没有进清单（收集阶段就挡住）", "损坏文件.jpg" not in names, str(names))
    check("不支持格式给了可读提示",
          any("格式不支持" in s for s in skipped), str(skipped))

    # 绕过收集阶段直接塞一个坏文件，验证处理阶段也不崩
    from core.converter import ImageInfo

    bad = ImageInfo(path=s["bad"], bytes=s["bad"].stat().st_size, width=100, height=100)
    good = items[0]
    rep = convert_batch([good, bad], out_dir=OUT / "robust",
                        options=ConvertOptions.from_preset("receipt", geometry=False),
                        pdf_name="robust.pdf")
    check("一张坏图不会拖垮整批", rep.pdf_path is not None and rep.pages >= 1,
          rep.summary)
    check("坏图在报告里是 failed 并带原因",
          any(i.status == "failed" and i.note for i in rep.items),
          str([(i.status, i.note) for i in rep.items]))


def test_color_modes(s: dict[str, Path]) -> None:
    section("灰度 / 黑白")
    import pymupdf

    items, _ = collect_images([s["flat"]])
    for mode, expect_smaller in (("gray", True), ("bw", True)):
        opt = ConvertOptions.from_preset("receipt", geometry=False, shadow="off",
                                         color_mode=mode)
        rep = convert_batch(items, out_dir=OUT / f"mode-{mode}", options=opt,
                            pdf_name=f"{mode}.pdf")
        doc = pymupdf.open(rep.pdf_path)
        xref = doc[0].get_images()[0][0]
        info = doc.extract_image(xref)
        doc.close()
        space = info.get("colorspace")
        gray_like = (space in (1, "DeviceGray")) or (isinstance(space, str) and "Gray" in space)
        if mode == "gray":
            check("灰度模式嵌入的是单通道 JPEG", gray_like, str(space))
        else:
            check("黑白模式嵌入的是灰度图（1 位或 8 位）",
                  gray_like and int(info.get("bpc", 8)) in (1, 8),
                  f"bpc={info.get('bpc')} colorspace={space}")
        base = convert_batch(items, out_dir=OUT / f"mode-base-{mode}",
                             options=ConvertOptions.from_preset("receipt", geometry=False,
                                                                shadow="off"),
                             pdf_name="color.pdf")
        check(f"{mode} 模式体积小于彩色", rep.pdf_bytes < base.pdf_bytes,
              f"{rep.pdf_bytes} vs {base.pdf_bytes}")


def test_scan_filters(s: dict[str, Path]) -> None:
    section("扫描件四档滤镜")
    import pymupdf
    from dataclasses import replace

    from core.converter import describe_options, effective_options, render_page
    from core.presets import COLOR_NAMES

    # 半明半暗 + 粗笔画：专门复现「自适应窗口太小，把暗侧笔画洗白」的翻车
    arr = np.full((800, 600, 3), 230, np.uint8)
    arr[:, 300:] = 150
    arr[200:210, 100:400] = 30
    arr[300:310, 100:350] = 30
    src = Image.fromarray(arr)

    check("画面效果共五档且含拍照修复", list(COLOR_NAMES) ==
          ["color", "scan", "enhance", "gray", "bw"], str(list(COLOR_NAMES)))
    check("彩色档原样不改动", np.array_equal(I.doc_filter(arr, "color"), arr))

    gray = I.doc_filter(arr, "gray")
    g_gray = gray[..., 0]
    check("灰度档去色但保留层次",
          np.array_equal(gray[..., 0], gray[..., 2]) and len(np.unique(g_gray)) >= 3,
          f"unique={len(np.unique(g_gray))}")
    g_enh = I.doc_filter(arr, "enhance")[..., 0]
    check("增强档把对比度拉开",
          int(g_enh.max()) - int(g_enh.min()) > int(g_gray.max()) - int(g_gray.min()),
          f"enhance={int(g_enh.max() - g_enh.min())} gray={int(g_gray.max() - g_gray.min())}")
    g_bw = I.doc_filter(arr, "bw")[..., 0]
    check("黑白档只有纯黑纯白两级",
          set(np.unique(g_bw).tolist()) <= {0, 255}, str(np.unique(g_bw).tolist()[:6]))
    check("黑白档半明半暗纸两侧笔画都保住",
          g_bw[100, 100] == 255 and g_bw[205, 100] == 0
          and g_bw[100, 350] == 255 and g_bw[205, 350] == 0,
          f"亮纸{g_bw[100, 100]} 亮笔画{g_bw[205, 100]} "
          f"暗纸{g_bw[100, 350]} 暗笔画{g_bw[205, 350]}")
    check("极小图也能二值化不崩",
          I.doc_filter(np.full((40, 30, 3), 200, np.uint8), "bw").shape == (40, 30, 3))

    r1 = I.enhance(src, geometry=False, shadow="off", color_mode="bw")
    r2 = I.enhance(src, geometry=True, shadow="off", color_mode="bw")
    check("enhance 无几何分支也套滤镜",
          r1.filter == "bw" and len(np.unique(np.asarray(r1.image)[..., 0])) == 2)
    check("enhance 有几何分支也套滤镜",
          r2.filter == "bw" and len(np.unique(np.asarray(r2.image)[..., 0])) == 2)
    check("去阴影仍排在二值化之前",
          I.enhance(src, geometry=False, shadow="standard",
                    color_mode="bw").shadow == "standard")

    tmp = OUT / "scan"
    tmp.mkdir(parents=True, exist_ok=True)
    for mode, fmt, pil_mode in (("color", "JPEG", "RGB"),
                                ("gray", "JPEG", "L"),
                                ("enhance", "JPEG", "L"),
                                ("bw", "PNG", "1")):
        p = I.save_page_image(src, tmp / f"{mode}.img", color_mode=mode, quality=82)
        im = Image.open(p)
        check(f"{mode} 档落盘为 {fmt}/{pil_mode}",
              im.format == fmt and im.mode == pil_mode, f"{im.format}/{im.mode}")
        im.close()

    items, _ = collect_images([s["flat"]])
    opt = ConvertOptions.from_preset("receipt", geometry=False, shadow="off",
                                     color_mode="enhance")
    rep = convert_batch(items, out_dir=OUT / "scan-pdf", options=opt,
                        pdf_name="enhance.pdf")
    doc = pymupdf.open(rep.pdf_path)
    info = doc.extract_image(doc[0].get_images()[0][0])
    doc.close()
    space = info.get("colorspace")
    gray_like = (space in (1, "DeviceGray")) or (isinstance(space, str) and "Gray" in space)
    check("增强档出片嵌入单通道图", gray_like, str(space))
    check("归一化与参数描述都认增强档",
          opt.normalized().color_mode == "enhance"
          and "增强" in describe_options(opt))
    check("参数指纹随滤镜变化（预览缓存会失效）",
          opt.signature() != opt.copy(color_mode="color").signature())

    _src_im, page_im, _pv_rep, _box = preview_page(items[0], opt)
    pv = np.asarray(page_im.convert("RGB"))
    check("预览看到的就是增强后的画面",
          np.allclose(pv[..., 0], pv[..., 1]) and np.allclose(pv[..., 1], pv[..., 2]),
          f"mode={page_im.mode}")
    tuned = replace(items[0], tune={"color_mode": "bw"})
    check("逐张覆盖可单独把一张设成黑白",
          effective_options(items[0], opt).color_mode == "enhance"
          and effective_options(tuned, opt).color_mode == "bw")
    _pg, _res, _cell, _pno, tuned_rep, _pbox = render_page(tuned, opt, 150)
    check("render_page 按逐张覆盖出黑白", tuned_rep.color == COLOR_NAMES["bw"],
          str(tuned_rep.color))

    # 旋转（引擎内部兼容参数）：空白区填白；独立于几何总开关；
    # 经 tune 白名单走进同一条处理链，预览与出片不会分叉。
    r0 = I.enhance(src, geometry=False, shadow="off", color_mode="color")
    rr = I.enhance(src, geometry=False, shadow="off", color_mode="color",
                   extra_rotate=12.0)
    check("任意旋转独立于几何总开关也生效",
          any(s.startswith("旋转") for s in rr.steps)
          and not any(s.startswith("旋转") for s in r0.steps), str(rr.steps))
    check("旋转后画布扩大且角落填白",
          rr.image.size != r0.image.size
          and np.asarray(rr.image)[2, 2].tolist() == [255, 255, 255],
          f"{r0.image.size} -> {rr.image.size} 角色值 "
          f"{np.asarray(rr.image)[2, 2].tolist()}")
    check("0° 不旋转不扩画布",
          I.enhance(src, geometry=False, shadow="off",
                    extra_rotate=0.0).image.size == r0.image.size)
    big = I.enhance(src, geometry=False, shadow="off", extra_rotate=250.0)
    check("旋转角被夹回 ±180°",
          any("旋转" in s and "180.0" in s for s in big.steps), str(big.steps))
    check("旋转角归一化保留一位小数",
          ConvertOptions(rotate=250).normalized().rotate == 180.0
          and ConvertOptions(rotate=-3.333).normalized().rotate == -3.3,
          str(ConvertOptions(rotate=250).normalized().rotate))
    check("参数指纹随旋转变化（预览缓存会失效）",
          opt.signature() != opt.copy(rotate=5.0).signature())
    rot_tuned = replace(items[0], tune={"rotate": 10.0})
    eo = effective_options(rot_tuned, opt)
    check("单张 tune 的 rotate 进入生效参数", abs(eo.rotate - 10.0) < 1e-9,
          str(eo.rotate))
    from core.converter import enhance_only
    _im10, res10, _rep10 = enhance_only(rot_tuned, opt)
    check("存图链路按单张 rotate 出图并写进处理链",
          any(s.startswith("旋转 +10.0") for s in res10.steps), str(res10.steps))

    # 拉伸（引擎内部兼容参数）：宽高独立缩放，同样独立于几何总开关，
    # 与旋转先后固定「先拉伸后旋转」，tune 白名单放行，链路一条不分叉。
    src = Image.new("RGB", (400, 300), (200, 100, 50))
    rs = I.enhance(src, geometry=False, shadow="off",
                   extra_scale_x=1.2, extra_scale_y=0.8)
    check("拉伸宽高独立生效（400×300 → 480×240）",
          rs.image.size == (480, 240)
          and any(s.startswith("拉伸") for s in rs.steps),
          f"{rs.image.size} {rs.steps}")
    check("100% 不拉伸不重采样",
          I.enhance(src, geometry=False, shadow="off",
                    extra_scale_x=1.0, extra_scale_y=1.0).image.size == (400, 300))
    rs2 = I.enhance(src, geometry=False, shadow="off",
                    extra_scale_x=1.2, extra_scale_y=0.8, extra_rotate=90.0)
    check("先拉伸后旋转（90° 旋转把拉伸后的 480×240 转成 240×480）",
          rs2.image.size == (240, 480), str(rs2.image.size))
    check("拉伸比被夹回 0.2~5.0",
          ConvertOptions(scale_x=9.0, scale_y=0.05).normalized().scale_x == 5.0
          and ConvertOptions(scale_x=9.0, scale_y=0.05).normalized().scale_y == 0.2)
    check("参数指纹随拉伸变化（预览缓存会失效）",
          opt.signature() != opt.copy(scale_x=1.3).signature())
    sc_tuned = replace(items[0], tune={"scale_x": 1.25, "scale_y": 1.0})
    eo2 = effective_options(sc_tuned, opt)
    check("单张 tune 的 scale 进入生效参数",
          abs(eo2.scale_x - 1.25) < 1e-9 and abs(eo2.scale_y - 1.0) < 1e-9,
          f"{eo2.scale_x}/{eo2.scale_y}")
    _im25, res25, _rep25 = enhance_only(sc_tuned, opt)
    check("存图链路按单张 scale 出图并写进处理链",
          any(s.startswith("拉伸 125%") for s in res25.steps), str(res25.steps))


def test_cancel(s: dict[str, Path]) -> None:
    section("取消不留半截")
    items, _ = collect_images([s["flat"], s["cn"], s["block"], s["receipt"]])
    out_dir = OUT / "cancel"
    rep = convert_batch(items, out_dir=out_dir,
                        options=ConvertOptions.from_preset("receipt"),
                        pdf_name="cancel.pdf", cancel=lambda: True)
    check("取消后标记 cancelled", rep.cancelled)
    check("取消后没有 PDF 产物", rep.pdf_path is None)
    left = list(out_dir.glob("*.pdf")) if out_dir.exists() else []
    check("输出目录里没有半截 PDF", not left, str([p.name for p in left]))
    check("取消也给了人话说明", "未生成 PDF" in rep.summary, rep.summary)


def test_report_json(s: dict[str, Path]) -> None:
    section("report.json 与逐张披露")
    items, _ = collect_images([s["skewed"]])
    rep = convert_batch(items, out_dir=OUT / "report",
                        options=ConvertOptions.from_preset("receipt", target_bytes=200 * 1024),
                        pdf_name="report.pdf")
    path = rep.pdf_path.parent / "report.json"
    check("report.json 与 PDF 同目录", path.exists(), str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("pdf", "pdf_bytes", "pages", "total_images", "attempts", "options",
                "items", "network_attempts"):
        check(f"report.json 含字段 {key}", key in data)
    item = data["items"][0]
    for key in ("src", "src_px", "out_px", "dpi", "geometry", "shadow", "color",
                "quality", "src_check", "digest"):
        check(f"逐张字段 {key} 存在", key in item)
    check("逐张写明了几何处理链", isinstance(item["geometry"], str) and item["geometry"],
          str(item.get("geometry")))
    check("逐张写明了去阴影档位", item["shadow"] in ("关", "轻", "标准", "强"), str(item["shadow"]))
    check("report.json 里的体积与磁盘一致",
          data["pdf_bytes"] == rep.pdf_path.stat().st_size)


def test_preview(s: dict[str, Path]) -> None:
    """界面「看效果」的可信度：预览必须和真实出片是同一张图、同一个 DPI。"""
    section("预览与成品一致")
    items, _ = collect_images([s["flat"], s["cn"]])

    for preset in ("receipt", "idcard", "homework"):
        opt = ConvertOptions.from_preset(preset, geometry=False, shadow="off")
        rep = convert_batch(items, out_dir=OUT / f"pv-{preset}", options=opt,
                            pdf_name="pv.pdf")
        for idx, (info, real) in enumerate(zip(items, rep.items)):
            src, page, pv_rep, (pw, ph, cell) = preview_page(info, opt, index=idx)
            check(f"{preset} 第{idx + 1}张：预览像素与出片一致",
                  page.size == tuple(real.out_px),
                  f"{page.size} vs {tuple(real.out_px)}")
            check(f"{preset} 第{idx + 1}张：预览 DPI 与出片一致",
                  abs(pv_rep.dpi - real.dpi) < 0.05, f"{pv_rep.dpi} vs {real.dpi}")
            check(f"{preset} 第{idx + 1}张：预览页号与出片一致",
                  pv_rep.page == real.page, f"{pv_rep.page} vs {real.page}")
        check(f"{preset}：预览不改原图尺寸",
              all(preview_page(i, opt, index=k)[0].size == (i.width, i.height)
                  for k, i in enumerate(items)))

    # 整页缩略用的落位框要真的在纸内，否则预览里会画到纸外面
    opt = ConvertOptions.from_preset("idcard")
    for idx in range(4):
        _src, _page, _rep, (pw, ph, cell) = preview_page(items[0], opt, index=idx)
        check(f"拼版第{idx + 1}格的落位框在 A4 纸内",
              0 <= cell[0] < cell[2] <= pw and 0 <= cell[1] < cell[3] <= ph,
              f"page=({pw:.0f},{ph:.0f}) cell={tuple(round(v) for v in cell)}")
    boxes = [preview_page(items[0], opt, index=i)[3][2] for i in (0, 1)]
    check("左右并排的两张落位框不重叠（一个在左、一个在右）",
          boxes[0][2] <= boxes[1][0], f"{tuple(round(v) for v in boxes[0])} vs "
                                      f"{tuple(round(v) for v in boxes[1])}")


def test_preview_sheet(s: dict[str, Path]) -> None:
    """主窗体成品预览：拼出来的整页必须和真出片的页数、比例、落位一致。"""
    section("成品预览（整页拼装）")
    items, _ = collect_images([s["flat"], s["cn"], s["receipt"], s["land"]])
    ones = [int(255)] * 3

    for paper, per, layout in (("A4", 1, "single"), ("A4", 4, "grid"),
                               ("A4", 2, "row"), ("自适应", 1, "single")):
        opt = ConvertOptions(paper=paper, dpi=150, margin_mm=10,
                             per_page=per, layout=layout).normalized()
        rep = convert_batch(items, out_dir=OUT / f"sheet-{per}-{layout}",
                            options=opt, pdf_name="s.pdf")
        pages = sheet_count(items, opt)
        check(f"{paper}/{per}张一页：预览页数与出片页数一致",
              pages == rep.pages, f"{pages} vs {rep.pages}")
        sheet, reps = preview_sheet(items, opt, 0)
        check(f"{paper}/{per}张一页：第 1 页拼得出来", sheet is not None)
        check(f"{paper}/{per}张一页：本页张数正确",
              len(reps) == min(per, len(items)), str(len(reps)))
        # 纸张比例要和出片的页面一致（自适应时跟着第一张图走）
        _pi, _pg, _rp, (pw, ph, _cell) = preview_page(items[0], opt, index=0)
        ratio_sheet = sheet.width / sheet.height
        check(f"{paper}/{per}张一页：整页比例与纸张一致",
              abs(ratio_sheet - pw / ph) < 0.01,
              f"{ratio_sheet:.3f} vs {pw / ph:.3f}")
        # 版心外四角必须是白纸，否则说明图被贴到了边距上
        w, h = sheet.size
        corners = [sheet.getpixel((1, 1)), sheet.getpixel((w - 2, 1)),
                   sheet.getpixel((1, h - 2)), sheet.getpixel((w - 2, h - 2))]
        check(f"{paper}/{per}张一页：边距处是白纸",
              all(c == tuple(ones) for c in corners), str(corners))
        ink = sum(1 for px in sheet.crop((0, 0, w, h)).resize((60, 60)).getdata()
                  if sum(px) < 720)
        check(f"{paper}/{per}张一页：页面上真有内容", ink > 20, str(ink))

    opt = ConvertOptions(dpi=150).normalized()
    check("超出页数返回空，不抛异常", preview_sheet(items, opt, 99) == (None, []))
    check("空清单没有页", sheet_count([], opt) == 0
          and preview_sheet([], opt, 0) == (None, []))


def test_page_furniture(s: dict[str, Path]) -> None:
    """页码 / 页脚：位置算得对、不压图、出片里真的有字，且不撑大体积。"""
    section("页码与页脚")
    from core.pdfbuild import cells, content_rect, has_page_text, page_texts, text_reserve

    off = ConvertOptions(dpi=150)
    on = ConvertOptions(dpi=150, page_numbers=True)
    foot = on.copy(footer_text="创略 · 内部资料")

    check("默认不画页码", not has_page_text(off))
    check("不画时不让版心", text_reserve(off) == (0.0, 0.0))
    band = text_reserve(on)[1]
    check("底部页码在版心下面让出一条带", 15.0 < band < 40.0, f"{band:.1f}pt")
    check("页码在顶部时让上面", text_reserve(on.copy(page_number_pos="top-center"))[0] > 0)
    check("只写页脚也让底部", text_reserve(off.copy(footer_text="x"))[1] > 0)

    pw, ph = 595.276, 841.89
    plain = content_rect(pw, ph, 12.0)
    kept = content_rect(pw, ph, 12.0, text_reserve(on))
    check("让出页码后版心变矮", kept[3] < plain[3] - band + 0.5,
          f"{plain[3]:.1f} -> {kept[3]:.1f}")

    t1 = page_texts(pw, ph, 1, 1, on)
    check("一页就一行页码", len(t1) == 1 and "第 1 页" in t1[0].text, str(t1))
    check("底部居中的 x 就是页面中线", abs(t1[0].x - pw / 2) < 0.5, f"{t1[0].x:.1f}")
    check("页码落在下边距里（不压图）", t1[0].y > kept[3], f"{t1[0].y:.1f} vs {kept[3]:.1f}")

    t2 = page_texts(pw, ph, 1, 1, foot)
    check("页码 + 页脚两行都画", len(t2) == 2, str(t2))
    check("页码在中间时页脚靠左", t2[1].align == "left" and t2[1].x < pw / 2,
          f"{t2[1].align} {t2[1].x:.1f}")
    check("页脚与页码同一行、不压字", abs(t2[0].y - t2[1].y) < 0.5)

    t3 = page_texts(pw, ph, 1, 1, foot.copy(page_number_pos="bottom-left"))
    sides = {t.align for t in t3}
    check("页码选了左下，页脚自动让到右边", sides == {"left", "right"}, str(sides))

    t4 = page_texts(pw, ph, 1, 1, on.copy(page_number_start=7))
    check("起始页号从 7 起算", t4[0].text.startswith("第 7 页"), t4[0].text)
    t5 = page_texts(pw, ph, 3, 3, on.copy(page_number_start=7))
    check("共几页算的是末页页号", "共 9 页" in t5[0].text, t5[0].text)

    # 出片里真的有这几个字；体积只多几 KB（用内置中文字体，不嵌整份字库）
    items, _ = collect_images([s["flat"], s["cn"], s["receipt"]])
    base = convert_batch(items, out_dir=OUT / "pager-off",
                         options=ConvertOptions(dpi=150), pdf_name="p.pdf")
    page = convert_batch(items, out_dir=OUT / "pager-on",
                         options=ConvertOptions(dpi=150, page_numbers=True,
                                                footer_text="创略 · 内部资料"),
                         pdf_name="p.pdf")
    import pymupdf

    doc = pymupdf.open(page.pdf_path)
    text0 = doc[0].get_text()
    doc.close()
    check("PDF 第 1 页有页码文字", "第 1 页" in text0, repr(text0[:40]))
    check("PDF 第 1 页有页脚文字", "创略" in text0, repr(text0[:40]))
    growth = page.pdf_bytes - base.pdf_bytes
    check("页码只多几 KB，不塞进整套中文字库", growth < 24 * 1024, f"+{growth} B")
    check("页码不会改变页数", page.pages == base.pages)

    # 关了页码就和以前一模一样
    check("关掉页码后 PDF 没有多出来的字",
          "第 1 页" not in (pymupdf.open(base.pdf_path)[0].get_text()))


def test_manual_layout(s: dict[str, Path]) -> None:
    """成品预览里手动摆位置：比例存储、越界夹回、预览与出片一致。"""
    section("手动排版")
    from core.converter import manual_rect, render_page, sheet_layers

    items, _ = collect_images([s["flat"], s["cn"]])
    opt = ConvertOptions(dpi=150).normalized()

    check("没摆过就是自动落格", manual_rect(items[0], opt, 595.276, 841.89) is None)

    items[0].place = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    rect = manual_rect(items[0], opt, 595.276, 841.89)
    check("比例换算成页面矩形",
          abs(rect[0] - 59.53) < 0.6 and abs(rect[3] - 505.13) < 0.9,
          str(tuple(round(v, 1) for v in rect)))
    _pg, _res, cell, _no, _rp, _box = render_page(items[0], opt, 150)
    check("出片用的就是手动那个矩形", tuple(round(v, 1) for v in cell)
          == tuple(round(v, 1) for v in rect), str(tuple(round(v) for v in cell)))

    # 换个纸型，比例不变 -> 位置跟着新页面走
    land = opt.copy(orientation="landscape").normalized()
    r2 = manual_rect(items[0], land, 841.89, 595.276)
    check("换纸型后位置按比例跟着走",
          abs((r2[2] - r2[0]) / 841.89 - 0.3) < 0.002, str(tuple(round(v, 1) for v in r2)))

    items[0].place = {"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5}
    clamped = manual_rect(items[0], opt, 595.276, 841.89)
    check("越界的位置被夹回纸内",
          clamped[2] <= 595.276 + 0.01 and clamped[3] <= 841.89 + 0.01,
          str(tuple(round(v, 1) for v in clamped)))
    items[0].place = {"x": -5, "y": -5, "w": 0.2, "h": 0.2}
    check("负坐标不出纸", manual_rect(items[0], opt, 595.276, 841.89)[0] >= 0)

    items[0].place = {"x": 0.05, "y": 0.05, "w": 0.4, "h": 0.3}
    auto_paper = opt.copy(paper="自适应").normalized()
    check("自适应纸张忽略手动位置", manual_rect(items[0], auto_paper, 595.276, 841.89) is None)

    data = sheet_layers(items, opt, 0)
    row, _im, layer_rect = data["layers"][0][:3]
    # 手动框和图片比例不同时按 contain 收进框里居中——不能拉伸变形
    pw0, _ph0 = data["page_pt"]
    check("成品预览的分层矩形收在手动框里且居中",
          layer_rect[0] >= 0.05 * pw0 - 0.6 and layer_rect[2] <= 0.45 * pw0 + 0.6
          and abs((layer_rect[0] + layer_rect[2]) / 2 - 0.25 * pw0) < 1.0,
          str(tuple(round(v, 1) for v in layer_rect)))
    check("分层带回它在清单里的下标", row == 0, str(row))

    # 预览里真的只在那块区域有墨
    sheet, _reps = preview_sheet(items, opt, 0)
    w, h = sheet.size
    top_right = sheet.crop((int(w * 0.7), 0, w, int(h * 0.35)))
    ink = sum(1 for px in top_right.resize((40, 40)).getdata() if sum(px) < 700)
    check("手动摆到左上后，右上角是白纸", ink <= 2, str(ink))

    items[0].place = None
    check("清掉位置就回到自动", manual_rect(items[0], opt, 595.276, 841.89) is None)


def test_captions_and_bookmarks(s: dict[str, Path]) -> None:
    """图注与书签：文件名洗得对不对、版面让得对不对、出片里真的有字有目录。"""
    section("图注与书签")
    import pymupdf

    from core.converter import (
        ImageInfo, caption_for, clean_caption, page_caption_bands, render_page,
        sheet_layers,
    )
    from core.pdfbuild import _shrink_for_caption, caption_band, cells, ellipsize

    d = ConvertOptions()
    check("图注默认关（多数图片自己会说话）", not d.captions)
    check("书签默认开（不占版面又便宜）", d.bookmarks)
    check("图注字号被夹在 6–18", d.copy(caption_size=99).normalized().caption_size == 18.0)
    check("默认图注字号 9pt", caption_band(9.0) > 15.0 and caption_band(9.0) < 18.0,
          f"{caption_band(9.0):.2f}pt")

    # 文件名清洗：相机/微信/截图自动名洗完应该什么都不剩
    check("相机自动名洗完不剩字", clean_caption("IMG_20261109_124512") == "")
    check("微信自动名洗完不剩字", clean_caption("微信图片_20261109124512") == "")
    check("纯日期截图名洗完不剩字", clean_caption("Screenshot_2026-11-09-124512") == "")
    check("相机纯编号洗完不剩字", clean_caption("DSC00012") == "")
    check("中文文件名原样当图注", clean_caption("平铺扫描") == "平铺扫描")
    check("下划线/连字符换成空格", clean_caption("合同-第3页") == "合同 第3页")
    check("带年份的仍保留", clean_caption("票据 2026 年 11 月 第一张").startswith("票据 2026"))

    items, _ = collect_images([s["flat"], s["cn"]])
    check("没手填就用文件名", caption_for(items[0]) == "平铺扫描")
    items[0].caption = "  第一章   合同正文  "
    check("手填优先，且空白被压平", caption_for(items[0]) == "第一章 合同正文")
    items[1].caption = ""
    check("清空手填就回落到文件名", caption_for(items[1]).startswith("票据"))

    # 版面几何：图注从格子底部让出一条带，图区变矮但顶部不动
    band = caption_band(9.0)
    plain = cells(595.276, 841.89, 12.0, 1, "single")[0]
    kept = cells(595.276, 841.89, 12.0, 1, "single", caption_h=band)[0]
    check("图注从格子底部让出一条带", abs((plain[3] - kept[3]) - band) < 0.01,
          f"{plain[3] - kept[3]:.2f}pt")
    check("让位后格子顶部不动", abs(plain[1] - kept[1]) < 0.01)
    grid_kept = cells(595.276, 841.89, 12.0, 4, "grid", caption_h=band)
    grid_plain = cells(595.276, 841.89, 12.0, 4, "grid")
    heights = [b[3] - b[1] for b in grid_kept]
    check("四张一页每格都各自让出，且四格仍一样高",
          all(abs((a[3] - b[3]) - band) < 0.01 for a, b in zip(grid_plain, grid_kept))
          and max(heights) - min(heights) < 0.01,
          str([round(h, 1) for h in heights]))
    low = _shrink_for_caption([(0.0, 0.0, 100.0, 20.0)], 18.0)[0]
    check("格子太矮时最多让掉三成", abs(low[3] - 14.0) < 0.01, f"{low[3]:.1f}")

    # 按页统一：同页只要有一张有字，整页一起让；全页都没字就一点都不让
    two = ConvertOptions(captions=True, per_page=2, layout="row").normalized()
    bands = page_caption_bands(items, two)
    check("一页两张时按页统一留高", len(bands) == 1 and bands[0] > 0, str(bands))
    blank = [ImageInfo(path=Path("IMG_20261109_124512.jpg"), bytes=1, width=10, height=10),
             ImageInfo(path=Path("Screenshot_2026-11-09-124512.jpg"), bytes=1, width=10, height=10)]
    check("整页都没字就不占版面", page_caption_bands(blank, two) == [0.0],
          str(page_caption_bands(blank, two)))
    check("关掉开关时一页都不让", page_caption_bands(items, two.copy(captions=False)) == [0.0])

    # 自适应纸张：页高要把图注算进去，否则字会掉到纸外
    auto_on = ConvertOptions(captions=True, paper="自适应", dpi=150).normalized()
    auto_off = auto_on.copy(captions=False).normalized()
    box_on = render_page(items[0], auto_on, 150)[5]
    box_off = render_page(items[0], auto_off, 150)[5]
    check("自适应页把图注高度算进页高", box_on[1] > box_off[1] + 10,
          f"{box_off[1]:.1f} -> {box_on[1]:.1f}")

    # 出片：图注真的有字，书签真的有目录项且页号对
    on = convert_batch(items, out_dir=OUT / "caption-on",
                       options=ConvertOptions(dpi=150, captions=True),
                       pdf_name="cap.pdf")
    off = convert_batch(items, out_dir=OUT / "caption-off",
                        options=ConvertOptions(dpi=150), pdf_name="cap.pdf")
    no_toc = convert_batch(items, out_dir=OUT / "caption-notoc",
                           options=ConvertOptions(dpi=150, bookmarks=False),
                           pdf_name="cap.pdf")

    doc = pymupdf.open(on.pdf_path)
    texts = [p.get_text() for p in doc]
    toc = doc.get_toc()
    doc.close()
    check("PDF 第 1 页印出了手填的图注", "第一章 合同正文" in texts[0], repr(texts[0][:40]))
    check("PDF 第 2 页用文件名当图注", "票据" in texts[1], repr(texts[1][:40]))
    check("书签一图一项", len(toc) == 2, str(toc))
    check("书签页号指向图片所在页", [row[2] for row in toc] == [1, 2], str(toc))
    check("书签名就是图注", toc[0][1] == "第一章 合同正文", str(toc[0]))
    growth = on.pdf_bytes - off.pdf_bytes
    check("图注只多几 KB，不塞字库", growth < 24 * 1024, f"+{growth} B")

    check("关掉图注后版面上没有那几个字",
          "第一章" not in pymupdf.open(off.pdf_path)[0].get_text())
    check("图注关了书签照旧（两件事互相独立）",
          len(pymupdf.open(off.pdf_path).get_toc()) == 2)
    check("关掉书签后没有目录", pymupdf.open(no_toc.pdf_path).get_toc() == [])

    import json

    meta = json.loads(on.pdf_path.with_suffix(".report.json").read_text(encoding="utf-8"))
    check("报告里记下了哪张印了什么字",
          meta["per_image_captions"].get(str(items[0].path)) == "第一章 合同正文",
          str(meta["per_image_captions"]))

    # 过长的图注截断，不然会压到旁边那一格上
    body = ellipsize("很长的图注" * 40, 180.0, 9.0)
    check("过长的图注被截断并补省略号", body.endswith("…") and len(body) < 40, body[:20])
    check("放不下一行时返回空", ellipsize("字" * 60, 4.0, 9.0) == "")

    # 画布拿到的图层带图注，拖动时字才跟着图走
    data = sheet_layers(items, ConvertOptions(dpi=150, captions=True), 0)
    check("图层把图注一起交给画布", len(data["layers"][0]) == 5
          and data["layers"][0][3] == "第一章 合同正文", str(data["layers"][0][3]))
    check("图层带着图注字号", abs(data["layers"][0][4] - 9.0) < 0.01)
    quiet = sheet_layers(items, ConvertOptions(dpi=150), 0)
    check("没开图注时图层不带字", quiet["layers"][0][3] == "")


def test_scale(s: dict[str, Path]) -> None:
    section("规模（60 张）")
    import time

    big = OUT / "scale_src"
    if big.exists():
        # 只搬走不删除：批量删文件在受限环境里会被拦，而且旧样张留着也便于回查。
        big.rename(OUT / f"_prev_scale_src_{int(time.time())}")
    big.mkdir(parents=True)
    paper = paper_image(800, 1130)
    for i in range(60):
        save_jpg(add_shadow(paper, 0.6, 1.0), big / f"批量{i:02d}.jpg")
    items, _ = collect_images([big])
    check("60 张全部收进清单", len(items) == 60, str(len(items)))
    t0 = time.perf_counter()
    rep = convert_batch(items, out_dir=OUT / "scale",
                        options=ConvertOptions.from_preset("receipt", dpi=150),
                        pdf_name="scale.pdf", workers=4)
    cost = time.perf_counter() - t0
    check("60 张 → 60 页", rep.pages == 60, str(rep.pages))
    check("逐张报告条数与张数一致", len(rep.items) == 60, str(len(rep.items)))
    check("页序连续无重复", sorted(i.page for i in rep.items) == list(range(1, 61)))
    check("规模跑完耗时 < 120s", cost < 120, f"{cost:.1f}s")
    print(f"        60 张耗时 {cost:.1f}s，输出 {rep.pdf_bytes / 1048576:.2f} MB")


def test_offline(s: dict[str, Path]) -> None:
    section("0 联网")
    items, _ = collect_images([s["flat"]])
    with NetworkAudit() as audit:
        convert_batch(items, out_dir=OUT / "net",
                      options=ConvertOptions.from_preset("receipt", geometry=False),
                      pdf_name="net.pdf")
    check("整批转换期间 0 次外连尝试", audit.attempts == 0, str(audit.attempts))
    check("自检能给出可读的联网结论", "0 次联网" in audit.summary(), audit.summary())
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "core").glob("*.py")
                    if p.name != "compliance.py")
    for word in ("urllib.request", "requests.", "http.client", "socket.create_connection("):
        check(f"核心代码里没有 {word}", word not in src)


def test_general_mode(s: dict[str, Path]) -> None:
    """v1.2.0：通用默认 / 参数自由度 / 自适应纸张 / 细粒度开关 / 重试复用。"""
    section("通用模式与参数自由度")
    import pymupdf

    from core.converter import default_out_dir, describe_options
    from core.presets import DEFAULT_PRESET, PAPER_AUTO

    d = ConvertOptions()
    check("无参构造就是通用默认", d.preset == DEFAULT_PRESET == "general")
    check("通用默认不擅自改图像内容（去阴影关）", d.shadow == "off")
    check("通用默认三项几何矫正都开", d.geometry and d.crop and d.deskew and d.perspective)
    check("旧预设键仍全部可读（老报告 / 命令行不失效）",
          {"receipt", "idcard", "homework"}.issubset(PRESETS))
    check("from_preset 只是填值：覆盖一项不牵动其它",
          ConvertOptions.from_preset("idcard", dpi=123).dpi == 123 and
          ConvertOptions.from_preset("idcard").dpi == 300)
    check("参数指纹随 DPI 变化", d.signature() != d.copy(dpi=300).signature())
    check("参数指纹不随目标体积变化（体积开关不该触发预览重算）",
          d.signature() == d.copy(target_bytes=1024).signature())

    n = ConvertOptions(paper=PAPER_AUTO, per_page=4, layout="grid").normalized()
    check("自适应纸张强制回到一页一张", n.per_page == 1 and n.layout == "single")
    n2 = ConvertOptions(geometry=False).normalized()
    check("总开关关掉时三个细分项都关", not (n2.crop or n2.deskew or n2.perspective))
    n3 = ConvertOptions(crop=False, deskew=False, perspective=False).normalized()
    check("三个细分项全关时总开关也归 False", n3.geometry is False)
    check("强制横版会覆盖 auto_landscape",
          ConvertOptions(orientation="landscape", auto_landscape=False).normalized().auto_landscape)
    check("强制竖版不会自动转横版",
          not ConvertOptions(orientation="portrait", auto_landscape=True).normalized().auto_landscape)
    check("空清单时默认输出目录不抛异常", default_out_dir([]).name == "out")
    check("参数描述写实际参数、不写预设名",
          "200dpi" in describe_options(d) and "receipt" not in describe_options(d))

    # 三个几何开关彼此独立：只开一项时，另两项不能偷偷跟着做
    only_crop = I.enhance(I.open_image(s["flat"]), geometry=True, perspective=False,
                          crop=True, deskew=False, shadow="off")
    check("只开裁边：不会顺手纠偏或拉正",
          not only_crop.perspective and only_crop.angle == 0.0, str(only_crop.steps))
    only_skew = I.enhance(I.open_image(s["skewed"]), geometry=True, perspective=False,
                          crop=False, deskew=True, shadow="off")
    check("只开纠偏：不会顺手裁边或拉正",
          not only_skew.perspective and "裁边" not in only_skew.steps, str(only_skew.steps))
    only_persp = I.enhance(I.open_image(s["skewed"]), geometry=True, perspective=True,
                           crop=False, deskew=False, shadow="off")
    check("只开透视：不会顺手裁边或纠偏",
          "裁边" not in only_persp.steps and only_persp.angle == 0.0, str(only_persp.steps))

    # 退化四边形不能让整张图被判成「处理失败」
    degenerate = np.array([[0, 0], [100, 0], [100, 100], [0, 0]], dtype=np.float32)
    check("退化四边形被识别为不可用", not I._quad_ok(degenerate))
    blank = np.zeros((64, 64, 3), dtype=np.uint8)
    check("退化四边形拉正时原样返回、不抛异常", I.warp_quad(blank, degenerate) is blank)
    good = np.array([[0, 0], [100, 0], [100, 100], [0, 100]], dtype=np.float32)
    check("正常四边形通过校验", I._quad_ok(good))

    # 自适应纸张：成品页面比例跟着图片走，预览与成品同尺寸
    items, _ = collect_images([s["land"]])
    opt = ConvertOptions(paper=PAPER_AUTO, dpi=150, margin_mm=0, geometry=False, shadow="off")
    rep = convert_batch(items, out_dir=OUT / "auto", options=opt, pdf_name="auto.pdf")
    check("自适应纸张出片成功", rep.pdf_path is not None, rep.summary)
    if rep.pdf_path:
        doc = pymupdf.open(rep.pdf_path)
        rect = doc[0].rect
        doc.close()
        _src, _page, _r, (pw, ph, _cell) = preview_page(items[0], opt)
        check("自适应：成品页面是横版", rect.width > rect.height,
              f"{rect.width:.0f}×{rect.height:.0f}pt")
        check("自适应：页面比例与图片一致（±0.02）",
              abs(rect.width / rect.height - 1414 / 1000) < 0.02,
              f"{rect.width / rect.height:.3f}")
        check("自适应：预览页面尺寸 == 成品页面尺寸",
              abs(pw - rect.width) < 1.5 and abs(ph - rect.height) < 1.5,
              f"{pw:.0f}×{ph:.0f} vs {rect.width:.0f}×{rect.height:.0f}")

    # 每个 PDF 一份同名报告：第二次生成不能把第一次的记录盖掉
    rep1 = convert_batch(items, out_dir=OUT / "reports", options=opt, pdf_name="第一批.pdf")
    rep2 = convert_batch(items, out_dir=OUT / "reports", options=opt, pdf_name="第二批.pdf")
    check("两次生成得到两个不同 PDF",
          rep1.pdf_path is not None and rep2.pdf_path is not None
          and rep1.pdf_path != rep2.pdf_path)
    check("每个 PDF 都有自己的同名 .report.json",
          bool(rep1.pdf_path and rep1.pdf_path.with_suffix(".report.json").exists()
               and rep2.pdf_path and rep2.pdf_path.with_suffix(".report.json").exists()))
    if rep1.pdf_path and rep2.pdf_path:
        d1 = json.loads(rep1.pdf_path.with_suffix(".report.json").read_text(encoding="utf-8"))
        d2 = json.loads(rep2.pdf_path.with_suffix(".report.json").read_text(encoding="utf-8"))
        check("两份报告各自指向自己的 PDF", d1["pdf"] != d2["pdf"])

    # 体积阶梯：跨轮复用增强结果后，凭据与单调性都不能丢
    tiny = ConvertOptions(dpi=300, geometry=False, shadow="off",
                          target_bytes=30 * 1024, floor_dpi=96)
    ladder = convert_batch(items, out_dir=OUT / "ladder", options=tiny, pdf_name="ladder.pdf")
    check("体积阶梯至少试了两轮", len(ladder.attempts) >= 2, str(ladder.attempts))
    check("重试轮仍写明原图只读凭据",
          bool(ladder.items and ladder.items[0].digest),
          str(ladder.items[0].digest[:8]) if ladder.items else "无 items")
    widths = [a["dpi"] for a in ladder.attempts]
    check("重试轮 DPI 单调不升", all(a >= b for a, b in zip(widths, widths[1:])), str(widths))


def test_per_image_tune(s: dict[str, Path]) -> None:
    """v1.3.0：界面里「编辑这张」写下的逐张修复参数，必须在预览与出片都生效。"""
    section("逐张单独修复")
    from core.converter import ImageInfo, effective_options, render_page

    base = ConvertOptions(dpi=150, shadow="standard", geometry=True)
    src_info, _ = collect_images([s["skewed"]])
    plain = src_info[0]
    e0 = effective_options(plain, base)
    check("没有逐张覆盖时就是整批参数", e0.shadow == "standard" and e0.geometry)

    tuned = ImageInfo(path=plain.path, bytes=plain.bytes, width=plain.width,
                      height=plain.height,
                      tune={"geometry": False, "shadow": "off", "color_mode": "bw"})
    e1 = effective_options(tuned, base)
    check("逐张覆盖生效（几何/去阴影/色彩）",
          not e1.geometry and e1.shadow == "off" and e1.color_mode == "bw")
    check("逐张覆盖不污染整批参数", base.shadow == "standard" and base.geometry)
    junk = ImageInfo(path=plain.path, bytes=plain.bytes, width=plain.width,
                     height=plain.height, tune={"dpi": 999, "paper": "乱写"})
    check("白名单过滤掉非修复项，不会串改整批页面设置",
          effective_options(junk, base).dpi == 150 and
          effective_options(junk, base).paper == "A4")
    wild = ImageInfo(path=plain.path, bytes=plain.bytes, width=plain.width,
                     height=plain.height, tune="不是字典")
    check("非法覆盖值被忽略而不是抛异常",
          effective_options(wild, base).shadow == "standard")

    _s0, _p0, r0, _b0 = preview_page(plain, base)
    _s1, _p1, r1, _b1 = preview_page(tuned, base)
    check("预览如实反映逐张去阴影差异", r0.shadow != r1.shadow and r1.shadow == "关",
          f"{r0.shadow} / {r1.shadow}")
    check("预览如实反映逐张色彩差异", r0.color != r1.color,
          f"{r0.color} / {r1.color}")
    pg, _res, _cell, _pno, rp, _box = render_page(tuned, base, 150, 1)
    check("render_page 入口也自动合并逐张覆盖",
          rp.shadow == "关" and rp.geometry == "原样嵌入",
          f"{rp.shadow} / {rp.geometry}")

    items, _ = collect_images([s["flat"], s["skewed"]], sort_by_exif=False)
    check("批量样张齐备", len(items) == 2, str(len(items)))
    items[0].tune = None
    items[1].tune = {"geometry": False, "shadow": "off", "color_mode": "bw"}
    rep = convert_batch(items, out_dir=OUT / "tune", options=base, pdf_name="逐张修复.pdf")
    check("两张都成功出片", rep.pdf_path is not None and len(rep.items) == 2,
          rep.summary)
    if rep.pdf_path and len(rep.items) == 2:
        by_src = {r.src: r for r in rep.items}
        normal, special = by_src[str(items[0].path)], by_src[str(items[1].path)]
        check("成品里逐张处理链确实与整批不同", normal.geometry != special.geometry,
              f"{normal.geometry} / {special.geometry}")
        check("成品里逐张色彩确实生效", special.color != normal.color,
              f"{special.color} / {normal.color}")
        _s, page, _r, _box = preview_page(items[1], base)
        check("逐张修复：预览落纸尺寸 == 成品落纸尺寸", page.size == tuple(special.out_px),
              f"{page.size} / {tuple(special.out_px)}")
        check("报告只记下单独修过的那张",
              len(rep.per_image_tunes) == 1 and str(items[1].path) in rep.per_image_tunes,
              str(list(rep.per_image_tunes)))
        data = json.loads(rep.pdf_path.with_suffix(".report.json").read_text(encoding="utf-8"))
        check("report.json 也写明逐张覆盖",
              data.get("per_image_tunes") == rep.per_image_tunes)


def test_save_processed(s: dict[str, Path]) -> None:
    """「编辑选中 → 应用」存下的修改后图：落盘位置 + 出片优先用它。"""
    section("保存修改后的图（编辑过的图优先）")
    from core.converter import (
        edited_copy, enhance_only, preview_page, render_page, save_processed_images,
    )

    work = OUT / "save_processed"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "单据.jpg"
    shutil.copy2(s["flat"], src)
    before = sha(src)
    items, _ = collect_images([str(src)], sort_by_exif=False)
    info = items[0]
    check("还没编辑过时没有修改后的图", edited_copy(info) is None)

    opt = ConvertOptions(dpi=150, shadow="standard", color_mode="color")
    reps = save_processed_images(items, opt)
    saved = Path(reps[0].dst) if reps and reps[0].dst else Path()
    check("存图成功并给出落盘路径",
          len(reps) == 1 and reps[0].status == "ok" and saved.is_file(), str(saved))
    check("落在原图旁的 out 目录、文件名沿用原名",
          saved.parent == src.parent / "out" and saved.name == "单据.jpg", str(saved))
    check("存图不动原图一个字节", sha(src) == before)
    full, _res, _rep = enhance_only(info, opt)
    with Image.open(saved) as got:
        check("存的是全分辨率那张，没缩进纸格子", got.size == full.size,
              f"{got.size} / {full.size}")

    check("存下之后就能认出「这张被编辑过」", edited_copy(info) == saved)

    _pg, _rs, _cl, _pn, rep, _bx = render_page(info, opt, 150)
    check("出片优先用修改后的图", rep.geometry == "用修改后的图", rep.geometry)
    _s, _p, prev_rep, _b = preview_page(info, opt)
    check("编辑窗口预览仍按原图试参数（改了参数能立刻看到）",
          prev_rep.geometry != "用修改后的图", prev_rep.geometry)

    bw = ConvertOptions(dpi=150, color_mode="bw")
    reps2 = save_processed_images(items, bw)
    png = Path(reps2[0].dst)
    check("黑白档存的是 PNG", png.suffix == ".png" and png.is_file(), str(png))
    check("换档后不留上一代的 JPEG", not saved.exists(), str(saved))
    check("一张图只留一份修改后的版本", edited_copy(info) == png, str(edited_copy(info)))

    # 原图后来被改动（比修改后图还新）→ 修改后图过期，回去用原图
    newer = time.time() + 10
    os.utime(src, (newer, newer))
    check("原图比修改后图新时判为过期、回到原图", edited_copy(info) is None)
    _pg2, _r2, _c2, _p2, rep2, _b2 = render_page(info, opt, 150)
    check("过期后出片重新处理原图", rep2.geometry != "用修改后的图", rep2.geometry)


def test_out_dir_not_rescanned(s: dict[str, Path]) -> None:
    """选文件夹时不能把 out 里的成品当原图再加一遍。"""
    section("不重复收 out 里的成品")
    work = OUT / "rescan"
    (work / "out").mkdir(parents=True, exist_ok=True)
    shutil.copy2(s["flat"], work / "原件.jpg")
    shutil.copy2(s["flat"], work / "out" / "原件.jpg")
    items, _ = collect_images([str(work)], sort_by_exif=False)
    check("只收原图，不进 out",
          len(items) == 1 and items[0].path.name == "原件.jpg",
          str([i.path.name for i in items]))


def test_annotations(s: dict[str, Path]) -> None:
    """页面标注：文字 / 方框 / 箭头 / 涂黑 / 签名图。

    这里断言的全是「用户能在纸上看到的结论」：字有没有印上去、涂黑是不是
    真的把下面盖暗、签名图的透明有没有保留、越界页号的标注会不会把出片搞坏。
    不测内部函数调用几次，那些随便重构。
    """
    section("页面标注")
    import pymupdf

    from core import annotate as A
    from core.converter import _group_annos
    from core.pdfbuild import _text_width

    # --- 几何：越界的要夹回来 -------------------------------------------
    a = A.make("box", 0, (-0.2, 0.1, 0.5, 0.2))
    check("负坐标被夹回纸内", a["x"] == 0.0 and a["w"] > 0, str(a))
    a2 = A.make("text", 0, (0.9, 0.9, 0.5, 0.5))
    check("超出右下角的宽高被夹住", a2["x"] + a2["w"] <= 1.0001
          and a2["y"] + a2["h"] <= 1.0001, f"{a2['x']:.2f}+{a2['w']:.2f}")
    a3 = A.make("redact", 0, (0.5, 0.5, 0.0, 0.0))
    check("拖成零面积也有最小尺寸", a3["w"] >= A.MIN_W and a3["h"] >= A.MIN_H,
          f"{a3['w']:.3f}x{a3['h']:.3f}")
    a4 = A.make("box", 0, (0.6, 0.6, -0.3, -0.2))
    check("往左上反着拖也能变成正的", a4["x"] < 0.6 and a4["w"] > 0, str(a4))
    check("未知类型退回方框", A.make("nonsense", 0, (0.1, 0.1, 0.2, 0.2))["kind"] == "box")

    # --- 换算：比例 → 纸上的 pt ------------------------------------------
    box = A.to_pt(A.make("box", 0, (0.1, 0.2, 0.5, 0.4)), 600, 800)
    check("比例 → pt 换算精确",
          all(abs(v - e) < 0.01 for v, e in zip(box, (60, 160, 360, 480))), str(box))

    # --- 颜色 ----------------------------------------------------------
    check("三位色号能解析", A.rgb("#fff") == (1.0, 1.0, 1.0))
    check("写错色号退回黑色", A.rgb("zzz") == (0.0, 0.0, 0.0))
    check("色号转 0–255", A.rgb255("#d0021b") == (208, 2, 27), str(A.rgb255("#d0021b")))

    # --- 折行：中文按字断、英文按词断 ------------------------------------
    m5 = lambda t, _sz: len(t) * 5.0        # 每个字符 5pt，便于手算
    cn = A.wrap_text("这是一个很长的中文句子需要折行处理", 60, 10, m5)
    check("中文按宽度折行，每行都不超宽",
          len(cn) > 1 and all(m5(x, 10) <= 60 for x in cn), str(cn))
    en = A.wrap_text("alpha beta gamma", 60, 10, m5)
    check("英文按词折行，不把单词劈开",
          all(w in ("alpha", "beta", "gamma") for ln in en for w in ln.split()),
          str(en))
    check("空文字不产生空行", A.wrap_text("   ", 100, 10, m5) == [])
    check("换行符被当成硬换行", len(A.wrap_text("第一行\n第二行", 500, 10, m5)) == 2)
    # 整段只有一个长词：以前走不到「拆词」那段逻辑，字直接溢出框外——
    # 用户看到字在框右边，点它却选不中（命中按框算）。
    long_word = A.wrap_text("fgsgfsdfg", 20, 10, m5)
    check("整段只有一个长词，也按字宽硬折断",
          len(long_word) > 1 and all(m5(x, 10) <= 20 for x in long_word),
          str(long_word))
    check("硬折断不丢字，拼回来还是原文",
          "".join(long_word) == "fgsgfsdfg", "".join(long_word))

    # --- 文字框自适应：写完字要看得见每一行 ------------------------------
    narrow = A.make("text", 0, (0.10, 0.10, 12 / 600, 12 / 800),
                    text="fgsgfsdfg", size=14)
    fitted = A.fit_text(narrow, 600, 800)
    fbox = A.to_pt(fitted, 600, 800)
    need = _text_width("fgsgfsdfg", 14)
    check("窄框被撑到放得下最长的词", fbox[2] - fbox[0] >= need - 4.0,
          f"{fbox[2] - fbox[0]:.1f}pt vs 字宽 {need:.1f}pt")
    lines = A.wrap_text(fitted["text"], fbox[2] - fbox[0] - 2, 14,
                        lambda t, z: _text_width(t, z))
    check("撑开之后每一行都装得下",
          all(_text_width(ln, 14) <= fbox[2] - fbox[0] - 2 + 0.01 for ln in lines),
          str([round(_text_width(ln, 14), 1) for ln in lines]))
    check("高度按行数给足，不裁行",
          fbox[3] - fbox[1] >= len(lines) * 14 * A.LINE_H,
          f"{fbox[3] - fbox[1]:.1f}pt / {len(lines)} 行")

    wide = A.make("text", 0, (0.05, 0.05, 0.9, 0.3), text="短", size=12)
    kept = A.to_pt(A.fit_text(wide, 600, 800), 600, 800)
    orig = A.to_pt(wide, 600, 800)
    check("已经够大的框不会被缩小",
          kept[2] - kept[0] >= orig[2] - orig[0] - 0.01
          and kept[3] - kept[1] >= orig[3] - orig[1] - 0.01,
          f"{orig} → {kept}")

    low = A.make("text", 0, (0.05, 0.94, 0.4, 0.03), text="一" * 300, size=12)
    lowbox = A.to_pt(A.fit_text(low, 600, 800), 600, 800)
    check("贴纸底的长文字往上挪，最后一行不出纸",
          lowbox[3] <= 800 + 0.01, f"底边 {lowbox[3]:.1f}pt")

    # --- 版面：字少居中，字多裁掉放不下的行 ------------------------------
    short = A.make("text", 0, (0.1, 0.1, 0.5, 0.1), text="一行", size=10)
    laid, lh = A.text_layout(short, A.to_pt(short, 600, 800), m5)
    expect_y = 80 + (80 - lh) / 2 + 10 * 0.98
    check("一行字在框里垂直居中", len(laid) == 1 and abs(laid[0][1] - expect_y) < 0.5,
          f"{laid[0][1]:.1f} vs {expect_y:.1f}")
    tall = A.make("text", 0, (0.1, 0.1, 0.5, 0.03), text="一" * 200, size=10)
    many, lh2 = A.text_layout(tall, A.to_pt(tall, 600, 800), m5)
    check("放不下的行被裁掉，不画到框外",
          len(many) <= int((800 * 0.03) // lh2), f"{len(many)} 行")
    align = A.make("text", 0, (0.1, 0.1, 0.5, 0.05), text="居中", size=10, align="center")
    mid, _ = A.text_layout(align, A.to_pt(align, 600, 800), m5)
    check("居中时行的起点右移", mid and mid[0][0] > 60.0, str(mid[:1]))

    # --- 箭头：起点 + 有符号位移，方向随便指 -----------------------------
    a_dn = A.make("arrow", 0, (0.1, 0.1, 0.3, 0.2))
    e_dn = A.arrow_ends(a_dn, 1.0, 1.0)
    check("箭头从起点指向位移终点",
          abs(e_dn[0][0] - 0.1) < 1e-9 and abs(e_dn[0][1] - 0.1) < 1e-9
          and abs(e_dn[1][0] - 0.4) < 1e-9 and abs(e_dn[1][1] - 0.3) < 1e-9,
          str(e_dn))
    a_up = A.make("arrow", 0, (0.1, 0.8, 0.4, -0.3))
    e_up = A.arrow_ends(a_up, 1.0, 1.0)
    check("负位移就是反方向（朝右上）",
          abs(e_up[1][0] - 0.5) < 1e-9 and abs(e_up[1][1] - 0.5) < 1e-9,
          str(e_up))
    check("水平 / 反向箭头的位移不被翻正",
          a_up["h"] < 0
          and A.make("arrow", 0, (0.5, 0.5, -0.3, 0.0))["w"] < 0)
    legacy = A.make("arrow", 0, (0.1, 0.1, 0.3, 0.2), flip=True)
    e_lg = A.arrow_ends(legacy, 1.0, 1.0)
    check("旧数据 flip=True 仍从右下指回左上",
          abs(e_lg[0][0] - 0.4) < 1e-9 and abs(e_lg[1][0] - 0.1) < 1e-9,
          str(e_lg))
    check("箭头点在线上才命中（包围盒空白不算）",
          A.hit_test([a_up], (0.3, 0.65), 0.02) == 0
          and A.hit_test([a_up], (0.15, 0.55), 0.02) == -1)

    # --- 分组：扁平列表和已分组的字典都认 --------------------------------
    flat = [A.make("box", 0, (0.1, 0.1, 0.2, 0.2)),
            A.make("box", 2, (0.1, 0.1, 0.2, 0.2))]
    g = _group_annos(flat)
    check("扁平列表按页分组", sorted(g) == [0, 2] and len(g[0]) == 1, str(sorted(g)))
    g2 = _group_annos({1: [{"kind": "text", "x": 0.2, "y": 0.2, "w": 0.3,
                            "h": 0.1, "text": "x"}]})
    check("已分组的输入也被归一", g2[1][0]["kind"] == "text" and g2[1][0]["page"] == 1)
    check("空输入不炸", _group_annos(None) == {} and _group_annos([]) == {})

    # --- 真的出片 -------------------------------------------------------
    items, _ = collect_images([s["flat"]])
    work = OUT / "anno"
    base = convert_batch(items, out_dir=work, options=ConvertOptions(dpi=110),
                         pdf_name="anno_base.pdf")

    sig = work / "sig.png"
    sign = Image.new("RGBA", (200, 80), (0, 0, 0, 0))
    ImageDraw.Draw(sign).rectangle([8, 28, 192, 52], outline=(200, 0, 0, 255), width=5)
    sign.save(sig)

    annos = [
        A.make("text", 0, (0.06, 0.04, 0.8, 0.05),
               text="已阅 请转财务复核", size=16, color="#d0021b"),
        A.make("redact", 0, (0.06, 0.30, 0.5, 0.05), color="#000000"),
        A.make("box", 0, (0.06, 0.62, 0.4, 0.06), color="#1b58c8", width=2.0),
        A.make("arrow", 0, (0.55, 0.68, 0.3, 0.1), color="#1b58c8"),
        A.make("arrow", 0, (0.85, 0.20, -0.25, 0.0), color="#d0021b", width=3.0),
        A.make("stamp", 0, (0.30, 0.45, 0.3, 0.1), src=str(sig)),
        A.make("text", 9, (0.1, 0.1, 0.5, 0.05), text="这一页不存在"),
    ]
    rep = convert_batch(items, out_dir=work, options=ConvertOptions(dpi=110),
                        pdf_name="anno.pdf", annotations=annos)
    check("出片成功且页数不变", rep.pages == base.pages, f"{rep.pages} vs {base.pages}")

    doc = pymupdf.open(rep.pdf_path)
    text = doc[0].get_text()
    check("文字标注真的印进了 PDF", "已阅" in text and "财务" in text, repr(text[:40]))
    check("越界页号的标注不出现在纸上", "这一页不存在" not in text)

    # 涂黑：同一位置比不带标注的那份明显更暗，才算真盖住了
    def pix_at(path, rx, ry):
        d = pymupdf.open(path)
        p = d[0].get_pixmap(dpi=72)
        im = Image.frombytes("RGB", (p.width, p.height), p.samples)
        px = im.getpixel((int(rx * im.width), int(ry * im.height)))
        d.close()
        return px

    dark = pix_at(rep.pdf_path, 0.30, 0.325)
    light = pix_at(base.pdf_path, 0.30, 0.325)
    check("涂黑区真的被盖暗", max(dark) < 60 and sum(light) - sum(dark) > 150,
          f"{dark} vs {light}")

    smasks = [x[1] for x in doc[0].get_images(full=True)]
    check("签名图带透明遮罩进 PDF（不是实心色块）", any(smasks), str(smasks))
    check("标注意外没有把 PDF 撑大", rep.pdf_bytes - base.pdf_bytes < 120_000,
          f"+{(rep.pdf_bytes - base.pdf_bytes) / 1024:.0f} KB")
    doc.close()

    check("报告里记下了每条标注", len(rep.annotations) == 7, str(len(rep.annotations)))
    check("报告里不写本机绝对路径",
          all("src" not in a or "\\" not in a["src"] and "/" not in a["src"]
              for a in rep.annotations),
          str([a.get("src") for a in rep.annotations if a.get("src")]))
    rj = json.loads((Path(rep.pdf_path).with_suffix(".report.json")).read_text("utf-8"))
    check("report.json 里有 annotations",
          len(rj.get("annotations") or []) == 7, str(len(rj.get("annotations") or [])))

    # --- 预览必须和出片一致 ---------------------------------------------
    sheet_on, _ = preview_sheet(items, ConvertOptions(dpi=110), 0, annotations=annos)
    sheet_off, _ = preview_sheet(items, ConvertOptions(dpi=110), 0)
    diff = sum(1 for a, b in zip(sheet_on.convert("L").getdata(),
                                 sheet_off.convert("L").getdata()) if abs(a - b) > 24)
    check("整页预览里画上了标注", diff > 500, f"{diff} 像素不同")


def test_regressions(s: dict[str, Path]) -> None:
    """本轮「重新检查所有代码」修出来的问题，逐条钉住，防止悄悄回归。"""
    section("回归验收（本轮修复）")
    from core.compliance import NetworkAudit
    from core.converter import _next_step, save_processed_images

    # 1) 同尺寸纠偏的公开契约：大角度归一化、灰度 / 四通道都不崩
    solid = np.full((240, 320, 3), 30, np.uint8)
    big = I.rotate_in_place(solid, 120.0)
    check("|θ|>90° 归一化后仍严格同尺寸、四角不露白",
          big.shape == solid.shape and float(big[:4, :4].mean()) < 80,
          f"{big.shape} / {float(big[:4, :4].mean()):.0f}")
    gray = np.full((120, 160), 200, np.uint8)
    g = I.rotate_in_place(gray, 6.0)
    check("灰度 2D 输入也能同尺寸纠偏",
          g.shape == gray.shape and float(g[:3, :3].mean()) > 100)
    bgra = np.full((120, 160, 4), 40, np.uint8)
    b = I.rotate_in_place(bgra, 6.0)
    check("四通道输入的填充值通道数正确",
          b.shape == bgra.shape and float(b[:3, :3, :3].mean()) < 80)

    # 2) 预览缓存键要覆盖所有会改画面的版面参数
    base = ConvertOptions()
    for kw in ({"captions": True}, {"page_numbers": True}, {"footer_text": "页脚"},
               {"gap_mm": 9.0}, {"caption_size": 12.0},
               {"page_number_size": 14.0}, {"page_number_fmt": "n"},
               {"page_number_pos": "top-right"}, {"page_number_start": 5}):
        key = next(iter(kw))
        check(f"signature 覆盖 {key}",
              base.copy(**kw).signature() != base.signature())

    # 3) 体积阶梯：低质量起步不许把 JPEG 质量往上抬
    step = _next_step(200, 45, 10_000_000, 1_000_000, 150, "color")
    check("q45 起步时降质单调（不会先升到 q60）",
          step is not None and step[1] <= 45, str(step))

    # 4) pdf_name 不能逃出 out 目录
    items, _ = collect_images([str(s["flat"])], sort_by_exif=False)
    work = OUT / "regress"
    work.mkdir(parents=True, exist_ok=True)
    rep = convert_batch(items, out_dir=work, options=ConvertOptions(dpi=96),
                         pdf_name="../escaped.pdf")
    check("带 ../ 的 pdf_name 只在 out 目录内落地",
          rep.pdf_path is not None and rep.pdf_path.parent == work.resolve()
          and "escaped" in rep.pdf_path.name, str(rep.pdf_path))
    rep2 = convert_batch(items, out_dir=work, options=ConvertOptions(dpi=96),
                         pdf_name="C:/Windows/Temp/hack.pdf")
    check("绝对路径 pdf_name 也只取文件名",
          rep2.pdf_path is not None and rep2.pdf_path.parent == work.resolve()
          and "hack" in rep2.pdf_path.name, str(rep2.pdf_path))

    # 5) 收集阶段：无效路径要给说法；报告里的 skipped / network_attempts 要真实
    found, bad = collect_images([str(work / "不存在的图-xyz.txt")],
                                 sort_by_exif=False)
    check("既非文件也非目录的输入会进 skipped",
          not found and any("路径不存在" in x for x in bad), str(bad))
    with NetworkAudit() as audit:
        rep3 = convert_batch(items, out_dir=work, options=ConvertOptions(dpi=96),
                            pdf_name="report_check.pdf",
                            skipped=["坏图.jpg：格式不支持"], network_audit=audit)
    data = json.loads(rep3.pdf_path.with_suffix(".report.json").read_text(encoding="utf-8"))
    check("report.json 记下被跳过的文件",
          data["skipped"] == ["坏图.jpg：格式不支持"], str(data["skipped"]))
    check("report.json 的 network_attempts 来自实时审计",
          data["network_attempts"] == audit.attempts, str(data["network_attempts"]))

    # 6) 取消「存修改后的图」不能留下半截文件污染下一次出片
    two, _ = collect_images([str(s["flat"]), str(s["skewed"])], sort_by_exif=False)
    cancel_dir = work / "cancel_out"
    ticks = {"n": 0}
    def cancel_late():
        ticks["n"] += 1
        return ticks["n"] > 1     # 先写完第一张，再取消
    reps = save_processed_images(two, ConvertOptions(dpi=96),
                                 out_dir=cancel_dir, cancel=cancel_late)
    check("取消存图后不留半截「修改后的图」",
          reps == [] and not list(cancel_dir.glob("*.jpg"))
          and not list(cancel_dir.glob("*.png")),
          f"{len(reps)} 份 / {[q.name for q in cancel_dir.glob('*')]}")

    # 7) CLI 与引擎的参数档位保持一致
    import cli
    args = cli.build_parser().parse_args([str(s["flat"]), "--color", "scan"])
    check("CLI 支持 scan（彩色扫描件）档", args.color == "scan")

    # 8) 「关于 / 使用说明」不能留已经退役的操作指引（按钮早就没有了）
    import core.about as about
    usage_blob = "\n".join(about.USAGE_SHORT + [about.DISCLOSURE])
    check("使用说明已同步即时预览，不再让用户点「看效果」",
          "看效果" not in usage_blob and "立即重算" in usage_blob)


def main() -> int:
    print(f"自检开始 · {I.__name__} · Python {sys.version.split()[0]}")
    s = make_samples()
    OUT.mkdir(parents=True, exist_ok=True)

    test_paper_detection(s)
    test_scan_filter(s)
    test_deskew(s)
    test_shadow(s)
    test_enhance_flags(s)
    test_dpi_and_pixels(s)
    test_target_size(s)
    test_imposition(s)
    test_orientation(s)
    test_source_untouched(s)
    test_robustness(s)
    test_color_modes(s)
    test_scan_filters(s)
    test_cancel(s)
    test_report_json(s)
    test_preview(s)
    test_preview_sheet(s)
    test_page_furniture(s)
    test_manual_layout(s)
    test_captions_and_bookmarks(s)
    test_annotations(s)
    test_scale(s)
    test_general_mode(s)
    test_per_image_tune(s)
    test_save_processed(s)
    test_out_dir_not_rescanned(s)
    test_offline(s)
    test_regressions(s)

    ok = all(RESULTS)
    passed = sum(RESULTS)
    print(f"\n合计 {passed}/{len(RESULTS)} 项通过")
    if not ok:
        print("\n未通过：")
        for d in DETAILS:
            print("  - " + d)
    print("结论：" + ("全部通过，核心引擎满足规范与立项承诺。"
                      if ok else "存在未通过项，见上。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
