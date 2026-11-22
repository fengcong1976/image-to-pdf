"""生成文档用截图（docs/screenshots/*.png）。

跑法：python tools/make_docs_shots.py

注意 1：这里**不能**用 offscreen 平台——offscreen 下没有系统字体，中文会全变成方框。
所以脚本会真的开一次窗口、抓完就关。
注意 2：v1.3.0 起界面是「左侧缩略图 + 右侧页面排版 + 常驻成品预览」，
        图像修复与输出集中在「编辑选中」一个窗口里，勾上「同时用于其它图片」
        就是整批生效。
每张截图都要等异步预览 / 成品排版算完再抓，避免拍到「正在处理…」的占位画面。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import main as m  # noqa: E402

SAMPLES = ROOT / "_docs_samples"
OUT = ROOT / "docs" / "screenshots"


def _shadow(im: Image.Image, left=0.60, right=1.0) -> Image.Image:
    arr = np.array(im).astype(np.float32)
    grad = np.linspace(left, right, arr.shape[1], dtype=np.float32)[None, :, None]
    return Image.fromarray(np.clip(arr * grad, 0, 255).astype(np.uint8))


def make_invoice(path: Path, seed: int) -> None:
    im = Image.new("RGB", (1000, 1320), (252, 251, 246))
    d = ImageDraw.Draw(im)
    d.rectangle((40, 40, 960, 150), fill=(30, 86, 156))
    d.text((70, 78), "电子发票（普通发票）", fill=(255, 255, 255))
    rows = [("购买方", "深圳某某科技有限公司"), ("项目", "办公用品一批"),
            ("金额", "¥ 1,280.00"), ("税额", "¥ 166.40"), ("价税合计", "¥ 1,446.40")]
    y = 210
    for k, v in rows:
        d.text((70, y), k, fill=(60, 60, 60))
        d.text((300, y), v, fill=(30, 30, 30))
        d.line((60, y + 34, 940, y + 34), fill=(205, 205, 200))
        y += 70
    rng = np.random.default_rng(seed)
    for i in range(9):
        w = int(rng.uniform(180, 620))
        d.rectangle((70, 620 + i * 62, 70 + w, 650 + i * 62), fill=(96, 96, 96))
    d.ellipse((700, 1020, 900, 1220), outline=(206, 60, 60), width=7)
    d.text((760, 1100), "发票专用章", fill=(206, 60, 60))
    _shadow(im).save(path, "JPEG", quality=93)


def make_card(path: Path, label: str) -> None:
    im = Image.new("RGB", (1000, 630), (248, 250, 252))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, 999, 629), radius=28, outline=(210, 214, 220), width=6)
    d.text((60, 60), label, fill=(120, 120, 120))
    d.rectangle((60, 140, 560, 200), fill=(70, 70, 70))
    d.rectangle((60, 250, 460, 292), fill=(110, 110, 110))
    d.rectangle((60, 340, 520, 382), fill=(110, 110, 110))
    d.rectangle((640, 140, 900, 470), fill=(206, 220, 236))
    d.text((700, 290), "照片", fill=(120, 140, 165))
    _shadow(im, 0.72, 1.0).save(path, "JPEG", quality=93)


def make_spread(path: Path) -> None:
    """一本摊开的书：宽横图，用来展示「自适应」纸张不会被压进 A4。"""
    im = Image.new("RGB", (1800, 1010), (250, 247, 238))
    d = ImageDraw.Draw(im)
    d.line((900, 30, 900, 980), fill=(196, 188, 172), width=5)
    d.text((120, 60), "第四章  图像与文档", fill=(40, 40, 40))
    d.text((980, 60), "4.2  版面与留白", fill=(40, 40, 40))
    rng = np.random.default_rng(21)
    for page_x in (120, 980):
        for i in range(20):
            y = 140 + i * 39
            w = int(rng.uniform(420, 720))
            d.rectangle((page_x, y, page_x + w, y + 17), fill=(92, 92, 92))
    d.rectangle((1080, 640, 1660, 900), outline=(150, 150, 150), width=4)
    d.text((1230, 760), "插图 4-2", fill=(120, 120, 120))
    _shadow(im, 0.75, 1.0).save(path, "JPEG", quality=93)


def build_samples() -> None:
    # 只搬走不删除：旧样张留着便于回查，也避开受限环境的批量删除限制
    if SAMPLES.exists():
        SAMPLES.rename(ROOT / f"_prev_docs_samples_{time.time_ns()}")
    SAMPLES.mkdir(parents=True)
    for i in range(3):
        make_invoice(SAMPLES / f"发票{i + 1}.jpg", seed=i)
    (SAMPLES / "证件").mkdir()
    make_card(SAMPLES / "证件" / "身份证-正面.jpg", "姓名  某某某    公民身份号码 4403**********1234")
    make_card(SAMPLES / "证件" / "身份证-反面.jpg", "签发机关  深圳市公安局    有效期限  2020.01-2040.01")
    (SAMPLES / "书页").mkdir()
    make_spread(SAMPLES / "书页" / "摊开的书.jpg")


def reset_shots() -> None:
    if OUT.exists() and any(OUT.iterdir()):
        OUT.rename(ROOT / f"_prev_docs_shots_{time.time_ns()}")
    OUT.mkdir(parents=True, exist_ok=True)


def shoot(app, widget, name: str) -> None:
    app.processEvents()
    widget.grab().save(str(OUT / name))
    print(f"  已保存 {name}")


def wait_view(app, view, timeout: float = 35.0) -> bool:
    """等一组预览面板真的出图（防抖 + 后台线程）再抓。"""
    end = time.time() + 0.4
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        # 单画布后 LivePreview 只有 pane，没有 left/right 两栏
        if (view.status.text().startswith("按当前参数实时算出")
                or view.status.text() == "预览失败") and not view._workers:
            return True
        time.sleep(0.02)
    print("  [警告] 预览没算完就抓图了")
    return False


def run_convert(app, win, timeout: float = 180.0) -> None:
    win.start()
    end = time.time() + timeout
    while win._running and time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def clear_files(win) -> None:
    win.files.clear()
    win.tunes.clear()
    win.results.clear()
    win.rebuild_list()


def wait_sheet(app, win, timeout: float = 35.0) -> bool:
    """等右侧成品预览真的排完再抓图。"""
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if win.sheet.canvas._bg is not None:
            return True
        time.sleep(0.02)
    print("  [警告] 成品预览没排完就抓图了")
    return False


def main() -> int:
    reset_shots()
    build_samples()
    from PySide6.QtGui import QDesktopServices

    QMessageBox.information = staticmethod(lambda *a, **k: None)  # type: ignore[assignment]
    QMessageBox.warning = staticmethod(lambda *a, **k: None)  # type: ignore[assignment]
    QMessageBox.critical = staticmethod(lambda *a, **k: None)  # type: ignore[assignment]
    QDesktopServices.openUrl = staticmethod(lambda *a, **k: True)  # type: ignore[assignment]

    app = QApplication(sys.argv)
    m.configure_app(app)
    win = m.MainWindow()
    win.show()
    app.processEvents()

    invoices = [str(SAMPLES / f"发票{i + 1}.jpg") for i in range(3)]

    # 1) 主界面：左侧缩略图 + 右侧页面排版 + 常驻成品预览
    win.add_files(invoices)
    app.processEvents()
    wait_sheet(app, win)
    shoot(app, win, "01-主界面.png")

    # 2) 编辑选中：单画布即时预览，参数一改自动重算
    win.thumb_list.setCurrentRow(0)
    app.processEvents()
    ed = win.make_edit_dialog()
    ed.show()
    app.processEvents()
    ed.shadow_box.setCurrentIndex(ed.shadow_box.findData("standard"))
    ed._show_effect()
    wait_view(app, ed.view)
    shoot(app, ed, "02-编辑选中-即时预览.png")

    # 3) 同一窗口勾上「同时用于其它图片」：这组参数就是整批的
    ed.apply_all.setChecked(True)
    ed.color_box.setCurrentIndex(ed.color_box.findData("enhance"))
    ed._show_effect()
    wait_view(app, ed.view)
    shoot(app, ed, "03-编辑选中-整批.png")
    ed._save_images()
    win._apply_edit(ed)
    app.processEvents()
    ed.close()

    # 4) 缩略列表：改过的那张带「单独修复 / 已存 out」的痕迹
    win.rebuild_list()
    win.thumb_list.setCurrentRow(0)
    app.processEvents()
    shoot(app, win, "09-缩略图列表与选中.png")

    # 5) 参数调整：页面排版在主窗体，成品预览跟着重排
    win.dpi_spin.setValue(300)
    win.layout_box.setCurrentIndex(win.layout_box.findText("一页两张（上下）"))
    app.processEvents()
    wait_sheet(app, win)
    shoot(app, win, "04-参数调整.png")

    # 6) 恢复常规参数后生成，缩略图带「✓ 已处理」
    win.dpi_spin.setValue(150)
    win.layout_box.setCurrentIndex(0)
    win.out_edit.setText(str(SAMPLES / "out"))
    run_convert(app, win)
    shoot(app, win, "05-生成完成.png")
    pdf_receipt = win.report.pdf_path if win.report else None

    # 7) 书页：纸张「自适应」，成品预览里是跟着比例的横版长页
    clear_files(win)
    win.paper_box.setCurrentText("A4")
    win.add_files([str(SAMPLES / "书页" / "摊开的书.jpg")])
    win.paper_box.setCurrentText("自适应")
    app.processEvents()
    wait_sheet(app, win)
    shoot(app, win, "06-自适应纸张-书页.png")

    # 8) 成品 PDF 首页：证明真的按参数落纸了
    import pymupdf

    if pdf_receipt and Path(pdf_receipt).exists():
        doc = pymupdf.open(pdf_receipt)
        doc[0].get_pixmap(dpi=100).save(str(OUT / "07-成品PDF-票据.png"))
        doc.close()
        print("  已保存 07-成品PDF-票据.png")

    # 9) 证件左右并排，生成完成
    clear_files(win)
    win.paper_box.setCurrentText("A4")
    win.layout_box.setCurrentIndex(win.layout_box.findText("一页两张（左右）"))
    win.dpi_spin.setValue(200)
    win.add_files([str(SAMPLES / "证件" / "身份证-正面.jpg"),
                   str(SAMPLES / "证件" / "身份证-反面.jpg")])
    win.out_edit.setText(str(SAMPLES / "out"))
    run_convert(app, win)
    shoot(app, win, "08-证件并排-完成.png")

    win.close()
    print(f"截图目录：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
