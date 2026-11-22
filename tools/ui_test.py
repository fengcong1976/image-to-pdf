"""界面自检：把「拖进来 → 调参数 → 编辑选中 → 即时预览 → 存图 → 生成 → 取消 → 关窗口」
跑一遍。

与 verify.py 分工：verify.py 验引擎（不吃界面），这里验界面（真的建 QMainWindow、
真的点按钮、真的跑 QThread），两者都跑才算验收完整。

跑法：python tools/ui_test.py            （约 1 分钟，会生成 _ui_samples/ 与截图）
      QT_QPA_PLATFORM=offscreen python tools/ui_test.py   # 无桌面时

本脚本只覆盖写入自己造的样张，不递归删除任何目录。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer, QUrl  # noqa: E402
from PySide6.QtGui import QDesktopServices, QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QGroupBox, QMessageBox, QSplitter,
)

import main as m  # noqa: E402
from core import annotate  # noqa: E402
from core import pdfbuild  # noqa: E402

SAMPLES = ROOT / "_ui_samples"
OUTDIR = ROOT / "_ui_out"
SHOTS = ROOT / "_ui_shots"

_passed = 0
_failed = 0
OPENED: list[str] = []
BOXES: list[str] = []
TYPED_TEXT = "已阅 请转财务复核"
STAMP_SRC = ""


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}" + (f"（{detail}）" if detail else ""))
    else:
        _failed += 1
        print(f"  FAIL  {name}" + (f"（{detail}）" if detail else ""))
    return cond


def reset_samples() -> None:
    """整轮开始先把上次的样张搬走（不删除，避开受限环境的批量删除限制）。

    取消用例会往同一目录造到 24 张，不隔离的话下一轮开头「4 张图全部进列表」
    就会凭空多出 20 张，后面所有页序断言跟着全崩。
    """
    if SAMPLES.exists():
        SAMPLES.rename(ROOT / f"_prev_ui_samples_{time.time_ns()}")
    SAMPLES.mkdir(parents=True)


def build_samples(n: int = 4) -> None:
    """造几张带阴影的样张；n 决定批量大小（取消用例要够多才来得及掐）。"""
    SAMPLES.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        im = Image.new("RGB", (900, 1270), (250, 250, 248))
        d = ImageDraw.Draw(im)
        d.rectangle((50, 60, 850, 150), fill=(40, 40, 40))
        for r in range(14):
            d.rectangle((50, 220 + r * 55, 50 + 600 - r * 25, 245 + r * 55),
                        fill=(70, 70, 70))
        arr = np.array(im).astype(np.float32)
        grad = np.linspace(0.62, 1.0, arr.shape[1], dtype=np.float32)[None, :, None]
        im = Image.fromarray(np.clip(arr * grad, 0, 255).astype(np.uint8))
        im.save(SAMPLES / f"样张{i + 1}.jpg", "JPEG", quality=92)


def install_stubs() -> None:
    """把会弹模态框的东西换成记账版本，否则无头环境下测试会卡死。"""
    def note(*a, **k):
        BOXES.append(str(a[2]) if len(a) > 2 else "")

    QMessageBox.information = staticmethod(note)  # type: ignore[assignment]
    QMessageBox.warning = staticmethod(note)  # type: ignore[assignment]
    QMessageBox.critical = staticmethod(note)  # type: ignore[assignment]
    QMessageBox.question = staticmethod(  # type: ignore[assignment]
        lambda *a, **k: QMessageBox.StandardButton.No)
    QDesktopServices.openUrl = staticmethod(  # type: ignore[assignment]
        lambda url: OPENED.append(url.toString()) or True)
    # 文字标注和签名图都要弹框；换成直接给一句话 / 给个路径
    m.QInputDialog.getMultiLineText = staticmethod(  # type: ignore[assignment]
        lambda *a, **k: (TYPED_TEXT, True))
    m.QFileDialog.getOpenFileName = staticmethod(  # type: ignore[assignment]
        lambda *a, **k: (STAMP_SRC, "图片 (*.png)"))


def run_and_wait(win, timeout: float = 180.0) -> float:
    t0 = time.perf_counter()
    while win._running and time.perf_counter() - t0 < timeout:
        QApplication.processEvents()
        time.sleep(0.02)
    QApplication.processEvents()
    return time.perf_counter() - t0


def wait_view(view, timeout: float = 25.0) -> bool:
    """等预览面板算完：状态栏出现「按当前参数实时算出」或明确报错都算结束。

    单画布后画布上永远有图（先原图后效果），不能拿「有没有图」当完成标志。
    """
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if (view.status.text().startswith("按当前参数实时算出")
                or view.status.text() == "预览失败"):
            return True
        time.sleep(0.02)
    return False


def wait_until(pred, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return False


def item_center(win, row: int) -> QPointF:
    rect = win.thumb_list.visualItemRect(win.thumb_list.item(row))
    return QPointF(rect.center())


def main() -> int:
    reset_samples()
    build_samples()
    OUTDIR.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    install_stubs()

    app = QApplication(sys.argv)
    m.configure_app(app)
    win = m.MainWindow()
    win.show()
    QApplication.processEvents()

    # ---------------------------------------------------------------- 启动
    print("\n=== 启动与主窗体职责 ===")
    check("窗口标题带产品名", "图片转PDF" in win.windowTitle(), win.windowTitle())
    check("没有一键填值行（界面不再按场景分类）",
          not hasattr(win, "preset_buttons") and not hasattr(win, "apply_preset"))
    check("主窗体不再摆单张修复控件",
          not any(hasattr(win, n) for n in (
              "crop_check", "deskew_check", "persp_check", "shadow_box",
              "color_box")))
    check("整份 PDF 的输出控件在主窗体（不在编辑窗口）",
          all(hasattr(win, n) for n in (
              "quality_spin", "use_target", "target_mb", "floor_box",
              "out_edit", "btn_out")))
    check("主窗体不再堆说明文字（用法都收进 tooltip）",
          not any(hasattr(win, n) for n in ("list_hint", "right_hint", "guide")))
    check("默认 200dpi / 12mm",
          (win.dpi_spin.value(), win.margin_spin.value()) == (200, 12),
          f"{win.dpi_spin.value()} / {win.margin_spin.value()}")
    check("默认 A4 / 一页一张 / 方向自动",
          win.paper_box.currentText() == "A4" and win.layout_box.currentText() == "一页一张"
          and win.orient_box.currentData() == "auto")
    check("空列表时「开始生成」不可点", not win.btn_start.isEnabled())
    check("空列表时「编辑选中」不可点", not win.btn_edit.isEnabled())
    check("空列表时显示拖入提示", win.empty_hint.isVisible())
    check("空列表时缩略列表隐藏", not win.thumb_list.isVisible())
    check("空列表时选中说明写着还没有图片",
          "还没有图片" in win.sel_label.text(), win.sel_label.text())

    split = win.findChild(QSplitter)
    check("主界面是左右分栏", split is not None and split.count() == 2)
    check("缩略列表在左边",
          split is not None and split.widget(0).findChild(m.ThumbList) is win.thumb_list)
    right_groups = split.widget(1).findChildren(QGroupBox) if split is not None else []
    check("右栏四组，都属于整份 PDF（排版 + 页码页脚 + 图注书签 + 输出）",
          [g.title() for g in right_groups]
          == ["页面排版（整份 PDF）", "页码与页脚（整份 PDF）",
              "图注与书签（整份 PDF）", "PDF 输出（整份 PDF）"],
          str([g.title() for g in right_groups]))
    check("右下角那排按钮已经去掉",
          not any(hasattr(win, n) for n in ("btn_preview", "btn_batch")))
    left_panel = split.widget(0) if split is not None else None
    check("「编辑选中」在左上按钮行里（不在右下）",
          win.btn_edit.text() == "编辑选中"
          and win.btn_edit.parent() is left_panel
          and win.btn_edit.y() == win.btn_del.y(),
          f"parent={win.btn_edit.parent() is left_panel} y={win.btn_edit.y()}"
          f" / 同排移出列表 y={win.btn_del.y()}")
    check("右侧常驻成品预览", isinstance(win.sheet, m.SheetPreview))

    # ---------------------------------------------------------------- 加入清单
    print("\n=== 拖入清单 ===")
    win.add_files([str(SAMPLES)])
    QApplication.processEvents()
    check("4 张图全部进列表", len(win.files) == 4, str(len(win.files)))
    check("缩略图卡片数与清单一致", win.thumb_list.count() == 4, str(win.thumb_list.count()))
    check("卡片写了页序、文件名与尺寸",
          "1. 样张1.jpg" in win.thumb_list.item(0).text()
          and "900×1270" in win.thumb_list.item(0).text(),
          win.thumb_list.item(0).text().replace("\n", " | "))
    check("每张卡片都生成了缩略图图标",
          all(not win.thumb_list.item(i).icon().isNull() for i in range(4)))
    check("卡片用 UserRole 记住完整路径",
          str(win.thumb_list.item(0).data(Qt.ItemDataRole.UserRole)) == str(win.files[0]))
    check("加入后隐藏拖入提示、显示列表",
          not win.empty_hint.isVisible() and win.thumb_list.isVisible())
    check("加入后「开始生成」可点", win.btn_start.isEnabled())
    check("加入后自动选中第 1 张", win.selected_rows() == [0], str(win.selected_rows()))
    check("选中说明写明是第 1 张", "第 1 张" in win.sel_label.text(), win.sel_label.text())
    # 自由变换已取消：旧单张设置里残留的旋转 / 拉伸不能偷偷生效
    probe = str(win.files[0])
    win.tunes[probe] = {"crop": True, "rotate": 12.0, "scale_x": 1.4}
    items, _ = win.current_items()
    tune = next((i.tune for i in items if str(i.path) == probe), None)
    check("旧设置残留的旋转 / 拉伸不会偷偷生效",
          tune is not None and all(k not in tune
                                  for k in ("rotate", "scale_x", "scale_y"))
          and tune.get("crop") is True, str(tune))
    win.tunes.clear()
    win.grab().save(str(SHOTS / "01-主界面.png"))
    before = len(win.files)
    win.add_files([str(SAMPLES)])
    check("重复拖入同一批不重复添加", len(win.files) == before, str(len(win.files)))

    # ---------------------------------------------------------------- 编辑窗口
    print("\n=== 编辑选中窗口 ===")
    win.thumb_list.setCurrentRow(1)
    QApplication.processEvents()
    check("选中第 2 张后「编辑选中」可点", win.btn_edit.isEnabled())
    dlg = win.make_edit_dialog()
    dlg.show()
    QApplication.processEvents()
    groups = [g.title() for g in dlg.findChildren(QGroupBox)]
    check("编辑窗口只有单张的两组（修复 / 存图），没有 PDF 输出",
          groups == ["图像修复（只改图片）", "保存处理后的图"], str(groups))
    check("编辑窗口里没有任何整份 PDF 的控件",
          not any(hasattr(dlg, n) for n in (
              "quality_spin", "use_target", "target_mb", "floor_box",
              "out_edit", "btn_out", "paper_box", "dpi_spin")))
    labels = [w.text() for w in dlg.findChildren(m.QLabel)]
    check("修复组里不再写「纸张/清晰度…属于整份 PDF」那句提示",
          not any("属于整份 PDF" in t or "页面排版」和「PDF 输出" in t
                  for t in labels), str(labels))
    check("标题写明第 2 / 4 张", "第 2/4 张" in dlg.windowTitle(), dlg.windowTitle())
    check("一打开就显示图（原图先上，不用任何按钮）",
          dlg.view.pane._pix is not None, dlg.view.pane.canvas.text())
    ready = wait_view(dlg.view)
    check("打开后自动算出当前效果（无「看效果」按钮）",
          ready and dlg.view.pane._pix is not None, dlg.view.status.text())
    check("没有「看效果」按钮（已由即时预览取代）",
          getattr(dlg, "btn_effect", None) is None)
    check("裁边 / 纠偏 / 透视三个开关默认都不勾（自动修复不擅自动手）",
          not any(w.isChecked() for w in (dlg.crop_check, dlg.deskew_check, dlg.persp_check)))
    check("自由变换整套已取消：任意旋转 / 拉伸控件都不存在",
          not any(hasattr(dlg, n) for n in (
              "rotate_spin", "scale_w_spin", "scale_h_spin",
              "rotate_reset", "scale_reset", "_transform_done")))
    cv = dlg.view.pane.canvas
    own = type(cv).__dict__
    check("画布不再自绘变换框、不再有拖动手势",
          type(cv).__name__ == "ImageCanvas"
          and "paintEvent" not in own and "mousePressEvent" not in own
          and not hasattr(cv, "cb_done") and not hasattr(cv, "_frame"),
          f"{type(cv).__name__} / {sorted(k for k in own if not k.startswith('__'))}")
    check("预览图按画布等比缩放、不会被拉伸变形",
          cv.pixmap() is not None and dlg.view.pane._pix is not None
          and cv.pixmap().width() <= cv.width()
          and cv.pixmap().height() <= cv.height()
          and abs(cv.pixmap().width() / cv.pixmap().height()
                  - dlg.view.pane._pix.width() / dlg.view.pane._pix.height()) < 0.02,
          f"显示 {cv.pixmap().width()}×{cv.pixmap().height()} 源 "
          f"{dlg.view.pane._pix.width()}×{dlg.view.pane._pix.height()}")
    check("处理链不再含旋转 / 拉伸（旧设置残留也被强制归零）",
          "旋转" not in dlg.view.info.text() and "拉伸" not in dlg.view.info.text(),
          dlg.view.info.text())
    check("画面效果下拉共五档", dlg.color_box.count() == 5,
          str([dlg.color_box.itemData(i) for i in range(dlg.color_box.count())]))
    check("下拉里有「彩色扫描件（拍照修复）」", dlg.color_box.findData("scan") >= 0)
    check("下拉里有「增强（扫描件）」", dlg.color_box.findData("enhance") >= 0)
    check("默认不勾「同时用于其它图片」", not dlg.apply_all.isChecked())
    check("默认勾上「应用时保存处理后的图」", dlg.save_images.isChecked())

    # 预览串图回归：改参数 / 切图必须作废在途 worker，旧指纹的结果不许落缓存
    view = dlg.view
    t0 = view._token
    view.set_options(dlg._compose())
    check("改参数会作废在途预览（token 递增）", view._token > t0, str(view._token))
    t1 = view._token
    view.set_index(0)
    check("切图也会作废在途预览", view._token > t1, str(view._token))
    view._cache.clear()
    view._on_done(view._token, None, None, None, None, "", ("旧参数指纹",))
    check("旧参数的结果不会写进当前缓存（防串图）",
          not view._cache, str(list(view._cache)))

    dlg.shadow_box.setCurrentIndex(dlg.shadow_box.findData("standard"))
    ready = wait_view(dlg.view)
    check("改参数后自动重算（即时预览）",
          ready and dlg.view.pane._pix is not None, dlg.view.status.text())
    check("效果说明写明落纸尺寸与实际 dpi",
          "dpi" in dlg.view.info.text() and "处理后" in dlg.view.info.text(),
          dlg.view.info.text())
    dlg.color_box.setCurrentIndex(dlg.color_box.findData("scan"))
    ready = wait_view(dlg.view)
    check("「彩色扫描件」档自动算出处理后图",
          ready and dlg.view.pane._pix is not None, dlg.view.status.text())
    check("修复参数里不再带旋转 / 拉伸字段",
          not any(k in dlg.repair_values()
                  for k in ("rotate", "scale_x", "scale_y")),
          str(dlg.repair_values()))
    dlg.color_box.setCurrentIndex(dlg.color_box.findData("color"))
    dlg.shadow_box.setCurrentIndex(dlg.shadow_box.findData("off"))
    wait_view(dlg.view)
    dlg.grab().save(str(SHOTS / "10-编辑选中.png"))

    # 只改这一张：不勾整批，给这张单独勾上裁边（整批默认没勾）
    key1 = str(win.files[1])
    dlg.crop_check.setChecked(True)
    saved_ok = dlg._save_images()
    check("点应用前就能把这一张存下来", saved_ok and len(dlg.saved) == 1,
          f"{saved_ok} / {len(dlg.saved)} / {dlg.save_error}")
    out_dir = win.files[1].parent / "out"
    check("修改后的图落在原图旁的 out 目录",
          all(Path(r.dst).parent == out_dir for r in dlg.saved)
          and all(Path(r.dst).is_file() for r in dlg.saved),
          str([Path(r.dst).name for r in dlg.saved]))
    win._apply_edit(dlg)
    QApplication.processEvents()
    check("不勾整批时只记这一张",
          list(win.tunes) == [key1], str([Path(k).name for k in win.tunes]))
    check("单独设置里带着这次的修复值",
          win.tunes[key1]["color_mode"] == "color" and win.tunes[key1]["crop"],
          str(win.tunes[key1]))
    check("整批参数没被单张改动带跑",
          not win.batch_values["crop"] and win.batch_values["color_mode"] == "color",
          str({k: win.batch_values[k] for k in ("crop", "color_mode")}))
    check("缩略图标出「单独修复」",
          "★ 单独修复" in win.thumb_list.item(1).text(),
          win.thumb_list.item(1).text().replace("\n", " | "))
    check("状态栏写明存到哪个 out",
          "out" in win.statusBar().currentMessage(), win.statusBar().currentMessage())
    dlg.close()

    # 勾整批：一次改全部
    dlg2 = win.make_edit_dialog()
    dlg2.show()
    QApplication.processEvents()
    check("已有单独设置的那张，窗口里显示的就是它自己的值",
          dlg2.crop_check.isChecked(), str(dlg2.crop_check.isChecked()))
    dlg2.btn_follow.clicked.emit()
    check("「跟随整批」把修复项恢复成整批默认（默认全不勾）",
          not dlg2.crop_check.isChecked())
    dlg2.apply_all.setChecked(True)
    dlg2.color_box.setCurrentIndex(dlg2.color_box.findData("gray"))
    dlg2.shadow_box.setCurrentIndex(dlg2.shadow_box.findData("light"))
    win.quality_spin.setValue(78)
    win.out_edit.setText(str(OUTDIR))
    dlg2.save_images.setChecked(False)     # 这一轮只验参数，不再存图
    win._apply_edit(dlg2)
    QApplication.processEvents()
    check("勾整批后整批参数跟着变",
          win.batch_values["color_mode"] == "gray"
          and win.batch_values["shadow"] == "light",
          str({k: win.batch_values[k] for k in ("color_mode", "shadow")}))
    check("勾整批后单张覆盖被清掉", win.tunes == {}, str(win.tunes))
    check("整批应用不碰主窗体的输出设置（质量 / 目录仍是主窗体那份）",
          win.quality_spin.value() == 78 and win.out_edit.text() == str(OUTDIR),
          f"{win.quality_spin.value()} / {win.out_edit.text()}")
    check("options() 用的是这批新参数",
          win.options().color_mode == "gray" and win.options().quality == 78,
          f"{win.options().color_mode} / {win.options().quality}")
    check("缩略图上的「单独修复」标记随整批应用一起消失",
          "单独修复" not in win.thumb_list.item(1).text())
    dlg2.close()

    # ---------------------------------------------------------------- 页面参数
    print("\n=== 页面排版（主窗体） ===")
    win.dpi_spin.setValue(240)
    win.margin_spin.setValue(8)
    QApplication.processEvents()
    check("options() 反映主窗体的 DPI", win.options().dpi == 240, str(win.options().dpi))
    check("options() 反映主窗体的边距", win.options().margin_mm == 8, str(win.options().margin_mm))
    check("主窗体常驻页面排版控件",
          all(w.isVisible() for w in (win.paper_box, win.dpi_spin, win.margin_spin,
                                     win.layout_box, win.orient_box)))

    print("\n=== PDF 输出（主窗体） ===")
    check("默认不限制体积", not win.use_target.isChecked())
    check("没勾体积上限时 MB 与最低画质锁灰",
          not win.target_mb.isEnabled() and not win.floor_box.isEnabled(),
          f"{win.target_mb.isEnabled()} / {win.floor_box.isEnabled()}")
    win.use_target.setChecked(True)
    check("勾上体积上限后两项解锁",
          win.target_mb.isEnabled() and win.floor_box.isEnabled())
    check("体积上限进 options()",
          win.options().target_bytes == win.target_mb.value() * 1048576,
          str(win.options().target_bytes))
    win.use_target.setChecked(False)
    check("取消体积上限后 target_bytes 为空", win.options().target_bytes is None)
    win.orient_box.setCurrentIndex(win.orient_box.findData("landscape"))
    check("页面方向可选强制横版", win.options().orientation == "landscape")
    win.orient_box.setCurrentIndex(win.orient_box.findData("auto"))
    win.paper_box.setCurrentText("自适应")
    QApplication.processEvents()
    check("选自适应纸张会锁成一页一张",
          win.layout_box.currentIndex() == 0 and not win.layout_box.isEnabled(),
          win.layout_box.currentText())
    win.paper_box.setCurrentText("A4")
    QApplication.processEvents()
    check("切回 A4 后拼版解锁", win.layout_box.isEnabled())
    win.grab().save(str(SHOTS / "02-参数.png"))

    # ---------------------------------------------------------------- 调序
    print("\n=== 缩略列表调序与移出 ===")
    names = [p.name for p in win.files]
    win.thumb_list.setCurrentRow(0)
    win.move_selected(1)
    check("下移把第 1 张挪到第 2 位", [p.name for p in win.files][:2] == [names[1], names[0]],
          str([p.name for p in win.files][:2]))
    win.thumb_list.setCurrentRow(1)
    win.move_selected(-1)
    check("上移把它挪回第 1 位", [p.name for p in win.files] == names,
          str([p.name for p in win.files]))
    win.on_rows_moved(0, 3)
    check("拖动改序：第 1 张挪到末尾", win.files[-1].name == names[0],
          str([p.name for p in win.files]))
    check("拖动后卡片页序重新编号",
          "1." in win.thumb_list.item(0).text() and "4." in win.thumb_list.item(3).text())
    win.on_rows_moved(3, 0)
    check("再拖回来顺序复原", [p.name for p in win.files] == names)
    win.thumb_list.setCurrentRow(len(win.files) - 1)
    _cap_key = str(win.files[-1])
    win.captions[_cap_key] = "临时图注"
    win.remove_selected()
    check("移出列表会真的少一张", len(win.files) == len(names) - 1, str(len(win.files)))
    check("移出列表同步删掉缩略图缓存", len(win.thumb_list) == len(win.files))
    check("移出列表同步清掉这张的图注", _cap_key not in win.captions,
          str(win.captions))
    win.add_files([str(SAMPLES / names[-1])])
    check("移出后还能再加回来", len(win.files) == len(names), str(len(win.files)))
    _same = SAMPLES / names[-1]
    win.add_files([str(_same.parent / _same.name.upper())])
    check("同一张图换个大小写拖进来不会重复", len(win.files) == len(names),
          str(len(win.files)))

    print("\n=== 缩略图真实鼠标拖拽 ===")
    check("列表用法写在提示里而不是占一行文字",
          "双击" in win.thumb_list.toolTip() and "拖动" in win.thumb_list.toolTip(),
          win.thumb_list.toolTip())
    vp = win.thumb_list.viewport()

    def send_mouse(typ, pos, button, buttons):
        ev = QMouseEvent(typ, pos, QPointF(vp.mapToGlobal(pos.toPoint())), button,
                         buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(vp, ev)

    order = [p.name for p in win.files]
    send_mouse(QEvent.Type.MouseButtonPress, item_center(win, 0),
               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    for r in (1, 2, 3):
        send_mouse(QEvent.Type.MouseMove, item_center(win, r),
                   Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    send_mouse(QEvent.Type.MouseButtonRelease, item_center(win, 3),
               Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    QApplication.processEvents()
    after = [p.name for p in win.files]
    check("鼠标按住第 1 张拖到第 4 张，顺序真的变了",
          after == order[1:] + [order[0]], f"{order} → {after}")
    check("拖完选中跟着被拖的那张走", win.selected_rows() == [3], str(win.selected_rows()))
    win.on_rows_moved(3, 0)
    check("拖回原位顺序复原", [p.name for p in win.files] == order)

    # ---------------------------------------------------------------- 成品预览
    print("\n=== 主窗体右侧的成品预览 ===")
    check("成品预览一开始就排好了页", wait_until(
        lambda: win.sheet.canvas._bg is not None
        and win.sheet.page_label.text().startswith("第 1 /")),
        win.sheet.page_label.text())
    pages_one = win.sheet._pages
    check("一页一张时页数 = 张数", pages_one == len(win.files), str(pages_one))

    win.layout_box.setCurrentIndex(3)      # 一页四张
    check("切成一页四张后页数变成 4 张一页",
          wait_until(lambda: win.sheet._pages == (len(win.files) + 3) // 4),
          str(win.sheet._pages))
    check("改排版后当前页仍是第 1 页", win.sheet.page_label.text().startswith("第 1 /"),
          win.sheet.page_label.text())
    if win.sheet._pages > 1:
        win.sheet.goto(1)
        check("成品预览可以翻页",
              wait_until(lambda: win.sheet._page == 1 and 1 in win.sheet._cache),
              win.sheet.page_label.text())
        check("翻到第 2 页后页码同步", "第 2 /" in win.sheet.page_label.text(),
              win.sheet.page_label.text())
        win.sheet.goto(0)
        wait_until(lambda: win.sheet._page == 0)
    win.layout_box.setCurrentIndex(0)
    check("切回一页一张页数还原", wait_until(lambda: win.sheet._pages == len(win.files)),
          str(win.sheet._pages))
    check("成品预览画的是整页白纸（比单张图高）",
          win.sheet.canvas._bg is not None
          and win.sheet.canvas._bg.height() > win.sheet.canvas._bg.width())

    # ---------------------------------------------------------------- 页码
    print("\n=== 页码与页脚 ===")
    check("默认不勾页码", not win.page_num_check.isChecked())
    check("没勾页码时位置 / 格式 / 起始 / 字号都锁灰",
          not win.page_pos_box.isEnabled() and not win.page_fmt_box.isEnabled()
          and not win.page_start_spin.isEnabled()
          and not win.page_size_spin.isEnabled())
    win.page_num_check.setChecked(True)
    QApplication.processEvents()
    check("勾上页码后那四项解锁",
          win.page_pos_box.isEnabled() and win.page_start_spin.isEnabled()
          and win.page_size_spin.isEnabled())
    check("页码进了整份 PDF 的参数", win.options().page_numbers)
    win.page_start_spin.setValue(5)
    win.footer_edit.setText("创略 · 内部资料")
    QApplication.processEvents()
    opt = win.options()
    check("起始页号与页脚也进了参数",
          opt.page_number_start == 5 and opt.footer_text == "创略 · 内部资料",
          f"{opt.page_number_start} / {opt.footer_text}")
    check("改了页码后成品预览重排",
          wait_until(lambda: win.sheet.canvas._bg is not None))
    win.page_start_spin.setValue(1)
    win.footer_edit.setText("")
    win.page_num_check.setChecked(False)
    QApplication.processEvents()
    check("取消勾选后回到不画页码", not win.options().page_numbers)

    # ---------------------------------------------------------------- 图注与书签
    print("\n=== 图注与书签 ===")
    check("图注默认不勾（文件名多半没信息量）", not win.caption_check.isChecked())
    check("书签默认勾上（不占版面又便宜）", win.bookmark_check.isChecked())
    check("没勾图注时字号锁灰", not win.caption_size_spin.isEnabled())
    win.caption_check.setChecked(True)
    QApplication.processEvents()
    check("勾上图注后字号解锁", win.caption_size_spin.isEnabled())
    opt = win.options()
    check("图注开关与字号进了整份 PDF 的参数",
          opt.captions and abs(opt.caption_size - 9.0) < 0.01,
          f"{opt.captions} / {opt.caption_size}")

    dlg = win.make_caption_dialog()
    check("图注窗口一张图一行", dlg.table.rowCount() == len(win.files),
          str(dlg.table.rowCount()))
    dlg.table.item(0, 1).setText("第一张  说明")
    got = dlg.values()
    check("只回传手写的那几条", got == {str(win.files[0]): "第一张 说明"}, str(got))
    dlg.close()

    win.captions = got
    win._items_key = None
    win.rebuild_list(select_row=0)
    QApplication.processEvents()
    check("缩略图标了「有图注」", "✎" in win.thumb_list.item(0).text(),
          win.thumb_list.item(0).text().replace("\n", " | "))
    check("勾上图注后成品预览重排并带上图注",
          wait_until(lambda: win.sheet.canvas._bg is not None
                     and bool(win.sheet.canvas._layers)
                     and win.sheet.canvas._layers[0].get("caption")),
          str([l.get("caption") for l in win.sheet.canvas._layers]))
    check("画布拿到的是手写的那句",
          win.sheet.canvas._layers[0].get("caption") == "第一张 说明",
          str(win.sheet.canvas._layers[0].get("caption")))

    win.bookmark_check.setChecked(False)
    QApplication.processEvents()
    check("关掉书签后参数跟着变", not win.options().bookmarks)
    win.bookmark_check.setChecked(True)
    QApplication.processEvents()

    # ---------------------------------------------------------------- 手动排版
    print("\n=== 页面编辑窗口（独立大窗口） ===")
    cv_main = win.sheet.canvas
    check("主窗体右栏是只读预览",
          wait_until(lambda: cv_main._bg is not None) and not cv_main.editable)
    check("「页面编辑…」按钮在，且照片就绪后可点",
          win.sheet.btn_edit is not None
          and win.sheet.btn_edit.text().startswith("页面编辑")
          and win.sheet.btn_edit.isEnabled(),
          win.sheet.btn_edit.text() if win.sheet.btn_edit else "None")

    # 按钮本身要真的能开出窗口：直接调 open_page_editor() 不算数，
    # 用户点的是按钮，中间那条信号断了就白搭。
    seen: dict[str, bool] = {}

    def _close_modal_editor():
        dlg = QApplication.activeModalWidget()
        if isinstance(dlg, m.PageEditor):
            seen["opened"] = True
            seen["closed"] = True
            dlg.close()

    def _fallback_close():
        """兜底：万一模态窗口没开出来，别把测试卡在 exec() 里。

        已经成功关过一次就什么都别做——后面还会开一个非模态的编辑窗口，
        而 PageEditor 自己 setModal(True)，activeModalWidget() 照样能拿到它，
        这一下会把正在用的窗口关掉（以前就踩过：编辑器悄悄没了，`_editor`
        变 None，撤销只能刷到右栏那份）。
        """
        if seen.get("closed"):
            return
        w = QApplication.activeModalWidget()
        if w is not None:
            w.close()

    QTimer.singleShot(700, _close_modal_editor)
    QTimer.singleShot(2500, _fallback_close)
    win.sheet.btn_edit.click()      # 模态窗口：exec() 里等上面那个定时器关窗
    QApplication.processEvents()
    check("点按钮真的弹出模态窗口，关掉后回到主界面",
          seen.get("opened") is True and win._editor is None,
          f"opened={seen.get('opened')} editor={win._editor}")

    ed = win.open_page_editor(modal=False)
    QApplication.processEvents()
    check("点它开出一个独立窗口", ed is not None and ed is win._editor)
    check("窗口标题写的是页面编辑", "页面编辑" in ed.windowTitle(), ed.windowTitle())
    check("同一时刻只有一个编辑窗口", win.open_page_editor(modal=False) is ed)
    cv = ed.sheet.canvas
    check("窗口里的画布比右栏那块高得多",
          wait_until(lambda: cv._bg is not None)
          and cv.height() > cv_main.height() + 100,
          f"{cv.height()}px vs 右栏 {cv_main.height()}px")
    check("窗口里天生可编辑、工具条常驻",
          cv.editable and ed.sheet.tools.isVisible() and ed.sheet.editing)

    if ed.sheet._pages > 1:
        QTest.keyClick(ed, Qt.Key.Key_PageDown)
        QApplication.processEvents()
        check("窗口里 PageDown 能翻页", ed.sheet._page == 1, str(ed.sheet._page))
        QTest.keyClick(ed, Qt.Key.Key_PageUp)
        QApplication.processEvents()
        check("PageUp 翻回上一页", ed.sheet._page == 0, str(ed.sheet._page))
        check("翻回来画布还在（走缓存，不用等重排）", cv._bg is not None)

    first_row = cv._layers[0]["row"]
    box = cv._to_px(cv._layers[0]["rect"])
    QTest.mouseClick(cv, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     box.center())
    QApplication.processEvents()
    check("点一张图能选中它", cv.selected_row() == first_row, str(cv.selected_row()))
    check("选中后「居中 / 铺满」可用",
          ed.sheet.btn_center.isEnabled() and ed.sheet.btn_fill.isEnabled())

    QTest.mousePress(cv, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     box.center())
    QTest.mouseMove(cv, box.center() + QPoint(30, 40))
    QTest.mouseRelease(cv, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       box.center() + QPoint(30, 40))
    QApplication.processEvents()
    key = str(win.files[first_row])
    check("拖动后主窗体记住了这一张的位置", key in win.places, str(win.places))
    check("记下的是页面比例（0–1）",
          all(0.0 <= v <= 1.0 for v in win.places.get(key, {}).values()),
          str(win.places.get(key)))
    check("缩略图标了「手动排版」",
          "⇱" in win.thumb_list.item(first_row).text(),
          win.thumb_list.item(first_row).text().replace("\n", " | "))

    # 往纸角猛拖：只能贴到版心边上，不能压进边距、更不能盖住页码
    lim = cv.limits()
    box = cv._to_px(cv.selected_rect())
    QTest.mousePress(cv, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     box.center())
    QTest.mouseMove(cv, QPoint(2, 2))
    QTest.mouseRelease(cv, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       QPoint(2, 2))
    QApplication.processEvents()
    moved = cv.selected_rect()
    check("拖到纸角也只贴到版心边，不压进边距",
          moved[0] >= lim[0] - 0.5 and moved[1] >= lim[1] - 0.5,
          f"{tuple(round(v, 1) for v in moved)} vs 版心 {tuple(round(v, 1) for v in lim)}")

    # 缩放：拖右下角，比例必须保持（不能把人脸压扁）
    before = cv.selected_rect()
    aspect_before = (before[2] - before[0]) / (before[3] - before[1])
    b = cv._to_px(before)
    QTest.mousePress(cv, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(b.right() - 1, b.bottom() - 1))
    QTest.mouseMove(cv, QPoint(b.right() - 30, b.bottom() - 40))
    QTest.mouseRelease(cv, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       QPoint(b.right() - 30, b.bottom() - 40))
    QApplication.processEvents()
    after = cv.selected_rect()
    aspect_after = (after[2] - after[0]) / (after[3] - after[1])
    check("拖角缩放后仍然保持原比例",
          abs(aspect_after - aspect_before) < 0.01,
          f"{aspect_before:.3f} → {aspect_after:.3f}")
    check("缩放真的改变了大小",
          abs((after[2] - after[0]) - (before[2] - before[0])) > 1,
          f"{before[2] - before[0]:.0f} → {after[2] - after[0]:.0f}")

    ed.sheet._arrange("center")
    QApplication.processEvents()
    rect_c = cv.selected_rect()
    pw_c, _ph_c = cv.page_size()
    check("「居中」把图摆到页面中线",
          abs((rect_c[0] + rect_c[2]) / 2 - pw_c / 2) < 1.0,
          f"{(rect_c[0] + rect_c[2]) / 2:.1f} vs {pw_c / 2:.1f}")

    # 复位本页现在连标注一起清，而且动手前要过确认框
    cv2 = ed.sheet.canvas
    pw0, ph0 = cv2.page_size()
    ed.sheet.set_tool("arrow")
    QApplication.processEvents()
    p1 = cv2._to_px([0.10 * pw0, 0.12 * ph0, 0.10 * pw0, 0.12 * ph0]).topLeft()
    p2 = cv2._to_px([0.42 * pw0, 0.18 * ph0, 0.42 * pw0, 0.18 * ph0]).topLeft()
    QTest.mousePress(cv2, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, p1)
    QTest.mouseMove(cv2, p2)
    QTest.mouseRelease(cv2, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, p2)
    QApplication.processEvents()
    check("复位前本页有一支箭头标注",
          any(a["page"] == ed.sheet._page for a in win.annotations))

    m.QMessageBox.question = staticmethod(  # type: ignore[assignment]
        lambda *a, **k: QMessageBox.StandardButton.No)
    ed.sheet._reset_here()
    QApplication.processEvents()
    check("确认框点了「否」：什么都不动",
          win.places != {}
          and any(a["page"] == ed.sheet._page for a in win.annotations))

    m.QMessageBox.question = staticmethod(  # type: ignore[assignment]
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    ed.sheet._reset_here()
    QApplication.processEvents()
    check("「复位本页」清掉这一页的手动位置", win.places == {}, str(win.places))
    check("复位同时把本页的标注也删干净",
          not any(a["page"] == ed.sheet._page for a in win.annotations),
          str([a["kind"] for a in win.annotations]))
    check("复位后缩略图不再标手动排版",
          "⇱" not in win.thumb_list.item(first_row).text())
    check("整次复位在撤销栈里是一步", ed.sheet.btn_undo.isEnabled())
    win.undo()
    QApplication.processEvents()
    check("撤销把复位整个退回去（标注和位置都回来）",
          any(a["page"] == ed.sheet._page for a in win.annotations)
          and win.places != {})
    win.redo()
    QApplication.processEvents()
    check("重做又把复位做回来（后面用例从干净状态开始）",
          win.places == {}
          and not any(a["page"] == ed.sheet._page for a in win.annotations))
    ed.sheet.set_tool("select")
    QApplication.processEvents()
    # 恢复默认：确认框一律回答「否」（防别的用例误触发删除）
    m.QMessageBox.question = staticmethod(  # type: ignore[assignment]
        lambda *a, **k: QMessageBox.StandardButton.No)

    win.paper_box.setCurrentText(m.PAPER_AUTO)
    QApplication.processEvents()
    check("自适应纸张下照样能开编辑窗口（只是拖不了图）",
          win.sheet.btn_edit.isEnabled() and cv._images_locked)
    check("自适应纸张下点不中图片（拖位置本来就没意义）", cv._hit((300, 300)) == -1)
    win.paper_box.setCurrentText("A4")
    QApplication.processEvents()
    check("切回 A4 后图片又能拖了", not cv._images_locked)

    win.grab().save(str(SHOTS / "11-成品预览.png"))

    # ---------------------------------------------------------------- 标注
    print("\n=== 页面标注（文字 / 方框 / 箭头 / 涂黑 / 签名图） ===")
    global STAMP_SRC, TYPED_TEXT
    sig = SHOTS / "签名样张.png"
    sign_im = Image.new("RGBA", (240, 90), (0, 0, 0, 0))
    ImageDraw.Draw(sign_im).rectangle([10, 30, 230, 60],
                                      outline=(20, 40, 120, 255), width=6)
    sign_im.save(sig)
    STAMP_SRC = str(sig)

    cv = ed.sheet.canvas          # 还是刚才那个编辑窗口，接着在上面画
    check("工具条有六种工具", len(ed.sheet.tool_buttons) == 6,
          str(list(ed.sheet.tool_buttons)))
    check("默认是选择工具", ed.sheet.tool == "select" and cv.mode == "select")
    check("标注一开始是空的", win.annotations == [])

    def drag_frac(fx0, fy0, fx1, fy1):
        """在页面上按比例拖一个框——和用户拿鼠标拖是同一条路径。"""
        pw, ph = cv.page_size()
        p1 = cv._to_px([fx0 * pw, fy0 * ph, fx0 * pw, fy0 * ph]).topLeft()
        p2 = cv._to_px([fx1 * pw, fy1 * ph, fx1 * pw, fy1 * ph]).topLeft()
        QTest.mousePress(cv, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, p1)
        QTest.mouseMove(cv, p2)
        QTest.mouseRelease(cv, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, p2)
        QApplication.processEvents()

    ed.sheet.set_tool("box")
    QApplication.processEvents()
    check("切到方框后画布进入绘图模式", cv.mode == "box")
    check("绘图时「居中 / 铺满」变灰",
          not ed.sheet.btn_center.isEnabled() and not ed.sheet.btn_fill.isEnabled())
    drag_frac(0.08, 0.20, 0.45, 0.26)
    check("拖一下就加了一条方框标注",
          len(win.annotations) == 1 and win.annotations[0]["kind"] == "box",
          str([a["kind"] for a in win.annotations]))
    check("标注记在当前页", win.annotations[0]["page"] == ed.sheet._page)

    ed.sheet.set_tool("redact")
    drag_frac(0.08, 0.34, 0.38, 0.355)
    check("涂黑默认纯黑，不跟当前笔色",
          win.annotations[-1]["kind"] == "redact"
          and win.annotations[-1]["color"] == "#000000",
          win.annotations[-1]["color"])

    ed.sheet.set_tool("arrow")
    drag_frac(0.55, 0.15, 0.85, 0.25)
    a1 = win.annotations[-1]
    check("往右下拖：起点落在起点处，位移朝右下",
          a1["kind"] == "arrow" and abs(a1["x"] - 0.55) < 2e-3
          and abs(a1["y"] - 0.15) < 2e-3
          and a1["w"] > 0.2 and a1["h"] > 0.05,
          str((a1["x"], a1["y"], a1["w"], a1["h"])))
    drag_frac(0.85, 0.30, 0.55, 0.40)
    a2 = win.annotations[-1]
    check("往左下拖：位移为负，方向跟着手走（不再只有两条对角线）",
          a2["kind"] == "arrow" and a2["w"] < -0.2 and a2["h"] > 0.05,
          str((a2["x"], a2["y"], a2["w"], a2["h"])))
    drag_frac(0.30, 0.50, 0.75, 0.50)
    a3 = win.annotations[-1]
    check("水平箭头画得出来（宽高不再被最小值撑开）",
          a3["kind"] == "arrow" and a3["w"] > 0.35 and abs(a3["h"]) < 0.004,
          str((a3["x"], a3["y"], a3["w"], a3["h"])))

    ed.sheet.set_tool("stamp")
    drag_frac(0.30, 0.45, 0.60, 0.55)
    check("签名图记下了图片路径",
          win.annotations[-1]["kind"] == "stamp"
          and win.annotations[-1]["src"] == STAMP_SRC,
          win.annotations[-1].get("src", ""))

    ed.sheet.set_tool("text")
    ed.sheet.size_spin.setValue(18)
    drag_frac(0.08, 0.62, 0.55, 0.67)
    check("文字标注带上了输入的内容",
          win.annotations[-1]["kind"] == "text"
          and "已阅" in win.annotations[-1]["text"],
          win.annotations[-1].get("text", ""))
    check("字号取自工具条", win.annotations[-1]["size"] == 18.0,
          str(win.annotations[-1]["size"]))
    total = len(win.annotations)
    check("五种工具一共加出七条标注（箭头三条）", total == 7, str(total))

    # ---- 用户报的坑：写完字，回头点自己的文字框却选不中、删不掉 ----
    pw, ph = cv.page_size()
    txt_i = len(win.annotations) - 1
    t = win.annotations[txt_i]
    tbox = cv._to_px([t["x"] * pw, t["y"] * ph,
                      (t["x"] + t["w"]) * pw, (t["y"] + t["h"]) * ph])

    # 手还停在「文字」工具上就点旧文字框——此时必须当成「选它」
    ed.sheet.set_tool("text")
    QApplication.processEvents()
    n_before = len(win.annotations)
    QTest.mouseClick(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, tbox.center())
    QApplication.processEvents()
    check("拿着文字工具点旧文字框 = 选中它，不是又画一个",
          len(win.annotations) == n_before and cv.selected_anno() == txt_i,
          f"{n_before} → {len(win.annotations)}，选中 {cv.selected_anno()}")
    check("点中之后工具退回「选择」，马上就能拖能删",
          ed.sheet.tool == "select" and cv.mode == "select",
          f"{ed.sheet.tool}/{cv.mode}")
    check("选中文字后「改字」可用", ed.sheet.btn_edit_text.isEnabled())

    # 改字：多行内容进来，框要长高，不能只露第一行
    old_h = win.annotations[txt_i]["h"]
    TYPED_TEXT = "已阅 同意报销\n请财务复核后付款"
    ed.sheet.btn_edit_text.click()
    QApplication.processEvents()
    check("「改字」把新内容写进去了",
          "同意报销" in win.annotations[txt_i]["text"],
          win.annotations[txt_i]["text"].replace("\n", " / "))
    check("文字框跟着长高，装得下两行",
          win.annotations[txt_i]["h"] > old_h + 1e-4,
          f"{old_h:.4f} → {win.annotations[txt_i]['h']:.4f}")

    # 长串英文：以前会从框右边溢出去，看起来有字、点上去却选不中
    ed.sheet.set_tool("text")
    TYPED_TEXT = "fgsgfsdfg"
    drag_frac(0.55, 0.72, 0.60, 0.745)      # 故意拖一个很窄的框
    long_anno = win.annotations[-1]
    long_pt = annotate.to_pt(long_anno, pw, ph)
    need = pdfbuild._text_width("fgsgfsdfg", float(long_anno["size"]))
    check("窄框 + 长串英文：框自动加宽到放得下",
          long_pt[2] - long_pt[0] >= need - 4.0,
          f"框宽 {long_pt[2] - long_pt[0]:.1f}pt，字宽 {need:.1f}pt")
    check("框高度也够放折出来的行",
          long_pt[3] - long_pt[1] >= 18.0 * 1.32,
          f"{long_pt[3] - long_pt[1]:.1f}pt")
    TYPED_TEXT = "已阅 请转财务复核"

    # 选中：点标注要先命中标注，而不是它底下那张图
    first = win.annotations[0]
    pw, ph = cv.page_size()
    center = cv._to_px([first["x"] * pw, first["y"] * ph,
                        (first["x"] + first["w"]) * pw,
                        (first["y"] + first["h"]) * ph]).center()
    ed.sheet.set_tool("select")
    QTest.mouseClick(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, center)
    QApplication.processEvents()
    check("点标注选中的是标注，不是底下的图",
          cv.selected_anno() == 0 and cv.selected_row() < 0,
          f"anno={cv.selected_anno()} row={cv.selected_row()}")
    check("选中标注后「删除」可用", ed.sheet.btn_del_anno.isEnabled())
    check("选中的是方框时「改字」是灰的", not ed.sheet.btn_edit_text.isEnabled())

    # 拖动
    before = dict(win.annotations[0])
    QTest.mousePress(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, center)
    QTest.mouseMove(cv, center + QPoint(26, 20))
    QTest.mouseRelease(cv, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, center + QPoint(26, 20))
    QApplication.processEvents()
    check("拖标注能移动，并同步回主窗体",
          abs(win.annotations[0]["x"] - before["x"]) > 0.004,
          f"{before['x']:.3f} → {win.annotations[0]['x']:.3f}")
    check("移动后尺寸没变",
          abs(win.annotations[0]["w"] - before["w"]) < 0.002)

    # 改颜色 / 字号
    idx = ed.sheet.color_box.findData("#1b58c8")
    ed.sheet.color_box.setCurrentIndex(idx)
    QApplication.processEvents()
    check("改颜色立刻写进选中那条", win.annotations[0]["color"] == "#1b58c8",
          win.annotations[0]["color"])
    ed.sheet.size_spin.setValue(22)
    QApplication.processEvents()
    check("改字号也写进去", win.annotations[0]["size"] == 22.0,
          str(win.annotations[0]["size"]))

    # 方向键微移
    x_before = win.annotations[0]["x"]
    QTest.keyClick(cv, Qt.Key.Key_Right)
    QApplication.processEvents()
    check("方向键微移标注", win.annotations[0]["x"] > x_before,
          f"{x_before:.4f} → {win.annotations[0]['x']:.4f}")

    # 删除
    n = len(win.annotations)
    QTest.keyClick(cv, Qt.Key.Key_Delete)
    QApplication.processEvents()
    check("Delete 删掉选中的标注", len(win.annotations) == n - 1,
          f"{n} → {len(win.annotations)}")

    # Esc 回选择
    ed.sheet.set_tool("box")
    QApplication.processEvents()
    QTest.keyClick(cv, Qt.Key.Key_Escape)
    QApplication.processEvents()
    check("Esc 从绘图工具退回选择",
          ed.sheet.tool == "select" and cv.mode == "select")
    check("状态栏说清有几处标注",
          "标注" in win.statusBar().currentMessage(),
          win.statusBar().currentMessage())

    # ---- 用户追问「别的功能框也一样」：方框 / 涂黑 / 箭头 都得能点回去 ----
    # 以前只有文字框有人管，其余四种拿着自己的工具点回去只会又画一个，
    # 选中之后工具也不退回，想拖开它一按下就又多一条。
    for kind, fx0, fy0, fx1, fy1 in [("box", 0.08, 0.60, 0.40, 0.65),
                                     ("redact", 0.08, 0.70, 0.40, 0.725),
                                     ("arrow", 0.08, 0.80, 0.40, 0.845)]:
        ed.sheet.set_tool(kind)
        drag_frac(fx0, fy0, fx1, fy1)
        n = len(win.annotations)
        mid = cv._to_px([(fx0 + fx1) / 2 * pw, (fy0 + fy1) / 2 * ph,
                         (fx0 + fx1) / 2 * pw, (fy0 + fy1) / 2 * ph]).topLeft()
        ed.sheet.set_tool(kind)      # 手还停在这个工具上
        QTest.mouseClick(cv, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, mid)
        QApplication.processEvents()
        label = annotate.KINDS.get(kind, kind)
        check(f"拿着「{label}」工具点回旧{label}：选中它，不多画一条",
              len(win.annotations) == n and cv.selected_anno() == n - 1,
              f"{n} → {len(win.annotations)}，选中 {cv.selected_anno()}")
        check(f"{label}选中后工具退回选择（不然拖不动它）",
              ed.sheet.tool == "select" and cv.mode == "select")

        before = dict(win.annotations[-1])
        QTest.mousePress(cv, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, mid)
        QTest.mouseMove(cv, mid + QPoint(24, 16))
        QTest.mouseRelease(cv, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, mid + QPoint(24, 16))
        QApplication.processEvents()
        moved = win.annotations[-1]
        check(f"选中的{label}能直接拖走（没变成又画一条）",
              len(win.annotations) == n and abs(moved["x"] - before["x"]) > 0.003,
              f"x {before['x']:.3f}→{moved['x']:.3f}，条数 {len(win.annotations)}")
        check(f"{label}拖动后全量副本也同步了（翻页才不会跳回去）",
              abs(ed.sheet._annos_all[ed.sheet._page_idx[-1]]["x"] - moved["x"]) < 1e-9,
              f"全量 {ed.sheet._annos_all[ed.sheet._page_idx[-1]]['x']:.4f}")

        n = len(win.annotations)
        ed.sheet.btn_del_anno.click()
        QApplication.processEvents()
        check(f"{label} 选中后「删除」按钮真能删掉",
              len(win.annotations) == n - 1,
              f"{n} → {len(win.annotations)}")
        check("删完三份列表一样长（不会从此错位一位）",
              len(win.annotations) == len(ed.sheet._annos_all)
              == len(ed.sheet._page_annos) == len(cv._annos),
              f"{len(win.annotations)}/{len(ed.sheet._annos_all)}/"
              f"{len(ed.sheet._page_annos)}/{len(cv._annos)}")

    # 画完发现框歪了，手一拖——那是在「调刚画的那条」，不是再叠一条
    ed.sheet.set_tool("box")
    drag_frac(0.55, 0.60, 0.85, 0.65)
    n = len(win.annotations)
    check("刚画完的那条自动是选中状态", cv.selected_anno() == n - 1,
          f"选中 {cv.selected_anno()}，共 {n}")
    x_before = win.annotations[-1]["x"]
    c0 = cv._to_px([0.70 * pw, 0.625 * ph, 0.70 * pw, 0.625 * ph]).topLeft()
    QTest.mousePress(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, c0)
    QTest.mouseMove(cv, c0 + QPoint(30, 22))
    QTest.mouseRelease(cv, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, c0 + QPoint(30, 22))
    QApplication.processEvents()
    check("画完立刻拖里面 = 移动它，不会再多出一条",
          len(win.annotations) == n and abs(win.annotations[-1]["x"] - x_before) > 0.003,
          f"条数 {len(win.annotations)}，x {x_before:.3f}→{win.annotations[-1]['x']:.3f}")
    check("工具还留在方框上，可以接着画下一个",
          ed.sheet.tool == "box" and cv.mode == "box")

    # ---- 箭头新交互：像微信截图那样，拖两端改方向长短、点线上才选中 ----
    ed.sheet.set_tool("arrow")
    QApplication.processEvents()
    drag_frac(0.60, 0.12, 0.85, 0.18)      # 一支朝右下的箭头，画完自动选中
    n = len(win.annotations)
    arr = win.annotations[-1]
    ends = cv._anno_arrow_pt(arr)
    tail = cv._pt_to_px(ends[2], ends[3]).toPoint()
    QTest.mousePress(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, tail)
    QTest.mouseMove(cv, tail + QPoint(-40, 30))
    QTest.mouseRelease(cv, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, tail + QPoint(-40, 30))
    QApplication.processEvents()
    arr2 = win.annotations[-1]
    check("拖箭头尖端：终点跟手变向，起点不动",
          len(win.annotations) == n and abs(arr2["x"] - arr["x"]) < 1e-6
          and abs(arr2["y"] - arr["y"]) < 1e-6
          and arr2["w"] < arr["w"] - 0.03,
          f"位移 ({arr['w']:.3f},{arr['h']:.3f}) → ({arr2['w']:.3f},{arr2['h']:.3f})")
    check("拖完端点工具还留在箭头上，可以接着画下一支",
          ed.sheet.tool == "arrow", ed.sheet.tool)
    off = cv._to_px([0.85 * pw, 0.40 * ph, 0.85 * pw, 0.40 * ph]).topLeft()
    ed.sheet.set_tool("select")
    QApplication.processEvents()
    QTest.mouseClick(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, off)
    QApplication.processEvents()
    check("点在箭头包围盒的空白处不算选中（箭头是条线）",
          cv.selected_anno() == -1, str(cv.selected_anno()))
    ed.sheet.width_box.setCurrentIndex(2)   # 粗——但此时没有选中，不该落数据
    QApplication.processEvents()
    check("没选中时改粗细只是换默认值，不动已有的箭头",
          win.annotations[-1]["width"] == 2.6,
          str(win.annotations[-1]["width"]))
    # 拿着箭头工具点回线上：选中它，工具退回选择
    line_mid = cv._to_px([(arr2["x"] + arr2["w"] / 2) * pw,
                          (arr2["y"] + arr2["h"] / 2) * ph,
                          (arr2["x"] + arr2["w"] / 2) * pw,
                          (arr2["y"] + arr2["h"] / 2) * ph]).topLeft()
    ed.sheet.set_tool("arrow")
    QApplication.processEvents()
    QTest.mouseClick(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, line_mid)
    QApplication.processEvents()
    check("拿着箭头工具点线上 = 选中它并退回选择",
          cv.selected_anno() == n - 1 and ed.sheet.tool == "select"
          and cv.mode == "select",
          f"选中 {cv.selected_anno()}，工具 {ed.sheet.tool}")
    ed.sheet.width_box.setCurrentIndex(2)   # 粗
    QApplication.processEvents()
    check("粗细下拉改的就是选中那条箭头",
          win.annotations[-1]["width"] == 4.5,
          str(win.annotations[-1]["width"]))

    # ---------------------------------------------------------------- 撤销 / 重做
    print("\n=== 撤销与重做 ===")
    check("改过东西之后「撤销」可点", ed.sheet.btn_undo.isEnabled())

    # 新建 → 撤销 → 重做
    ed.sheet.set_tool("box")
    drag_frac(0.10, 0.86, 0.30, 0.90)
    n = len(win.annotations)
    win.undo()
    QApplication.processEvents()
    check("撤销把刚画的那条收回去了",
          len(win.annotations) == n - 1 and len(cv._annos) == n - 1,
          f"{n} → {len(win.annotations)}，画布 {len(cv._annos)}")
    check("撤销后「重做」可点", ed.sheet.btn_redo.isEnabled())
    win.redo()
    QApplication.processEvents()
    check("重做把它放回来，画布也同步",
          len(win.annotations) == n and len(cv._annos) == n,
          f"{len(win.annotations)} / 画布 {len(cv._annos)}")

    # 拖动 → 撤销回原位
    ed.sheet.set_tool("select")
    QApplication.processEvents()
    one = cv._to_px([0.20 * pw, 0.88 * ph, 0.20 * pw, 0.88 * ph]).topLeft()
    QTest.mouseClick(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, one)
    QApplication.processEvents()
    check("点得中刚画的那条（撤销重做没把坐标搞乱）", cv.selected_anno() >= 0,
          str(cv.selected_anno()))
    x_before = win.annotations[cv.selected_anno()]["x"]
    QTest.mousePress(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, one)
    QTest.mouseMove(cv, one + QPoint(34, -18))
    QTest.mouseRelease(cv, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, one + QPoint(34, -18))
    QApplication.processEvents()
    moved_x = win.annotations[cv.selected_anno()]["x"]
    win.undo()
    QApplication.processEvents()
    check("撤销一次拖动，位置退回原处",
          abs(win.annotations[cv.selected_anno()]["x"] - x_before) < 1e-6,
          f"{x_before:.4f} → {moved_x:.4f} → {win.annotations[cv.selected_anno()]['x']:.4f}")

    # 连点字号只算一步（撤销会把选中清掉，先点回来）
    QTest.mouseClick(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, one)
    QApplication.processEvents()
    sel = cv.selected_anno()
    check("撤销之后还能再点中它", sel >= 0, str(sel))
    size_before = win.annotations[sel]["size"]
    stack_before = len(win._undo)
    for v in (16, 18, 22, 28):
        ed.sheet.size_spin.setValue(v)
        QApplication.processEvents()
    check("连点四下字号只压一步撤销（不是四步）",
          len(win._undo) - stack_before == 1,
          f"栈长了 {len(win._undo) - stack_before} 步，字号 {size_before}→"
          f"{win.annotations[sel]['size']}")
    win.undo()
    QApplication.processEvents()
    check("一次撤销就把字号退回原值",
          win.annotations[sel]["size"] == size_before,
          f"{size_before} → {win.annotations[sel]['size']}")

    # 删除 → 撤销
    QTest.mouseClick(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, one)
    QApplication.processEvents()
    check("点回来之后能按 Delete", cv.selected_anno() >= 0,
          str(cv.selected_anno()))
    n = len(win.annotations)
    QTest.keyClick(cv, Qt.Key.Key_Delete)
    QApplication.processEvents()
    check("Delete 删掉了", len(win.annotations) == n - 1, f"{n} → {len(win.annotations)}")
    win.undo()
    QApplication.processEvents()
    check("撤销把删掉的标注放回来",
          len(win.annotations) == n and len(cv._annos) == n,
          f"{len(win.annotations)} / 画布 {len(cv._annos)}")
    check("恢复后三份列表还是一样长",
          len(win.annotations) == len(ed.sheet._annos_all) == len(cv._annos),
          f"{len(win.annotations)}/{len(ed.sheet._annos_all)}/{len(cv._annos)}")

    # 图片位置 → 撤销
    places_before = dict(win.places)
    ed.sheet.set_tool("select")
    QApplication.processEvents()
    # 找图上一点没被标注盖住的地方，否则点到的是标注、拖走的是标注
    lx0, ly0, lx1, ly1 = cv._layers[0]["rect"]
    img_c = None
    for fy in (0.12, 0.5, 0.88, 0.3, 0.7):
        cand = cv._to_px([lx0 + (lx1 - lx0) * 0.5, ly0 + (ly1 - ly0) * fy,
                          lx0 + (lx1 - lx0) * 0.5, ly0 + (ly1 - ly0) * fy]).topLeft()
        pt = cv._to_pt(cand)
        if cv._hit_anno(pt) < 0:
            img_c = cand
            break
    check("图上找得到一处没被标注盖住的点", img_c is not None)
    QTest.mouseClick(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, img_c)
    QApplication.processEvents()
    check("点中的是图片不是标注", cv.selected_row() >= 0, str(cv.selected_row()))
    QTest.mousePress(cv, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, img_c)
    QTest.mouseMove(cv, img_c + QPoint(26, 18))
    QTest.mouseRelease(cv, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, img_c + QPoint(26, 18))
    QApplication.processEvents()
    check("拖动图片后记下了手动位置", len(win.places) > len(places_before),
          f"{len(places_before)} → {len(win.places)}")
    win.undo()
    QApplication.processEvents()
    check("撤销图片位置，退回自动排布",
          win.places == places_before, f"{len(win.places)} 条手动位置")

    # 快捷键与「新动作踩断重做」
    win.undo()
    QApplication.processEvents()
    check("重做栈这时有东西", ed.sheet.btn_redo.isEnabled())
    ed.sheet.set_tool("redact")
    drag_frac(0.62, 0.86, 0.86, 0.90)
    check("做了新动作，「重做」就变灰了", not ed.sheet.btn_redo.isEnabled())

    ed.sheet.set_tool("select")
    QApplication.processEvents()
    n = len(win.annotations)
    QTest.keyClick(ed, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()
    check("Ctrl+Z 能撤销（焦点不在画布上也管用）",
          len(win.annotations) == n - 1, f"{n} → {len(win.annotations)}")
    QTest.keyClick(ed, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()
    check("Ctrl+Y 能重做", len(win.annotations) == n,
          f"{len(win.annotations)}")

    # 撤到底再全部重做回来：不崩、按钮自己灰掉、也不影响后面的流程
    n_keep = len(win.annotations)
    for _ in range(80):
        win.undo()
    QApplication.processEvents()
    check("一路撤到底：没崩、按钮自己变灰",
          not ed.sheet.btn_undo.isEnabled() and len(win.annotations) >= 0,
          f"{len(win.annotations)} 条，按钮 {ed.sheet.btn_undo.isEnabled()}")
    for _ in range(80):
        win.redo()
    QApplication.processEvents()
    check("再一路重做回来，条数回到撤之前",
          len(win.annotations) == n_keep,
          f"{len(win.annotations)} / {n_keep}")

    # 自适应纸张：页面比例跟着图走，拖图没意义，但批注照样得能加
    win.paper_box.setCurrentText(m.PAPER_AUTO)
    QTest.qWait(700)
    QApplication.processEvents()
    n0 = len(win.annotations)
    ed.sheet.set_tool("box")
    drag_frac(0.10, 0.10, 0.45, 0.16)
    check("自适应纸张下也能加标注", len(win.annotations) == n0 + 1,
          f"{n0} → {len(win.annotations)}")
    ed.sheet.set_tool("select")
    win.paper_box.setCurrentText("A4")
    QTest.qWait(500)
    QApplication.processEvents()

    # 标过的这条也要落到缩略图外的地方能看见：截个图
    cv.update()
    QApplication.processEvents()
    ed.grab().save(str(SHOTS / "12-页面标注.png"))
    n_before_close = len(win.annotations)
    ed.close()
    QApplication.processEvents()
    check("关窗后主窗体不再持有编辑窗口", win._editor is None)
    check("关窗后标注一条不少", len(win.annotations) == n_before_close,
          f"{n_before_close} → {len(win.annotations)}")
    check("主窗体预览同步到了这些标注",
          wait_until(lambda: len(win.sheet._page_annos) >= 1),
          str(len(win.sheet._page_annos)))

    # ---------------------------------------------------------------- 主流程
    print("\n=== 生成 PDF ===")
    win.dpi_spin.setValue(150)  # 跑快点，测的是界面链路不是清晰度
    win.start()
    check("开始后进入运行态", win._running)
    check("运行中「开始生成」禁用", not win.btn_start.isEnabled())
    check("运行中「取消」可点", win.btn_cancel.isEnabled())
    check("运行中参数区锁住，避免预览与成品不一致", not win.dpi_spin.isEnabled())
    check("运行中「编辑选中」禁用", not win.btn_edit.isEnabled())
    cost = run_and_wait(win)
    check("跑完后退出运行态", not win._running, f"{cost:.1f}s")
    check("跑完后参数区解锁", win.dpi_spin.isEnabled())
    check("跑完后「编辑选中」恢复", win.btn_edit.isEnabled())
    rep = win.report
    check("生成了 PDF", rep is not None and rep.pdf_path is not None and rep.pdf_path.exists(),
          str(rep.pdf_path if rep else None))
    check("PDF 落在指定的输出目录", rep.pdf_path.parent == OUTDIR, str(rep.pdf_path.parent))
    check("PDF 体积大于 0", rep.pdf_bytes > 0, str(rep.pdf_bytes))
    check("进度条走满", win.progress.value() == win.progress.maximum(),
          f"{win.progress.value()}/{win.progress.maximum()}")
    texts = [win.thumb_list.item(r).text() for r in range(win.thumb_list.count())]
    check("每张缩略图都标了「已处理」", all("✓ 已处理" in t for t in texts),
          str([t.replace("\n", " | ") for t in texts]))
    check("每张缩略图都写了实际 dpi", all("dpi" in t for t in texts))
    check("跑完弹出了汇总", any("页" in b and "MB" in b for b in BOXES), str(BOXES[-1:]))
    opened_local = QUrl(OPENED[-1]).toLocalFile() if OPENED else ""
    check("跑完自动打开结果文件夹", bool(opened_local) and Path(opened_local) == OUTDIR,
          opened_local)
    check("底部显示 0 联网结论", "0 次联网" in win.statusBar().currentMessage(),
          win.statusBar().currentMessage())
    check("跑完「打开结果文件夹」可点", win.btn_open.isEnabled())
    # 标注跟着出片一起走：PDF 里能查出刚才写的那行字
    import pymupdf
    doc = pymupdf.open(rep.pdf_path)
    page_text = "\n".join(doc[i].get_text() for i in range(len(doc)))
    doc.close()
    check("生成的 PDF 里带着页面标注的文字", "已阅" in page_text, repr(page_text[:60]))
    check("标注都写进了报告", len(rep.annotations) >= 5, str(len(rep.annotations)))
    by_src = {r.src: r for r in rep.items}
    edited_src = str(win.files[1]) if len(win.files) > 1 else ""
    if edited_src in by_src:
        check("编辑过的那张：出片用的是修改后的图",
              by_src[edited_src].geometry == "用修改后的图",
              by_src[edited_src].geometry)
        others = [r for k, r in by_src.items() if k != edited_src]
        check("没编辑过的那些仍按参数处理原图",
              all(r.geometry != "用修改后的图" for r in others),
              str([r.geometry for r in others]))
    win.grab().save(str(SHOTS / "03-完成.png"))

    n_open = len(OPENED)
    win.open_result()
    check("手动打开结果文件夹会真的打开", len(OPENED) == n_open + 1, str(OPENED[-1:]))

    # ---------------------------------------------------------------- 取消
    print("\n=== 取消 ===")
    build_samples(24)
    win.files.clear()
    win.rebuild_list()
    win.add_files([str(SAMPLES)])
    check("24 张全部进列表", len(win.files) == 24, str(len(win.files)))
    out2 = OUTDIR / f"cancel-{time.time_ns()}"
    out2.mkdir(exist_ok=True)
    win.out_edit.setText(str(out2))
    win.dpi_spin.setValue(300)
    win.start()
    win.cancel()
    run_and_wait(win, timeout=120)
    check("取消后退出运行态", not win._running)
    check("取消后报告标记 cancelled", win.report is not None and win.report.cancelled,
          str(win.report.cancelled if win.report else None))
    check("取消后没有产出 PDF", not list(out2.glob("*.pdf")),
          str([p.name for p in out2.glob("*.pdf")]))
    check("取消后说明了没生成 PDF",
          any("取消" in b for b in BOXES[-2:])
          or (win.report is not None and "未生成 PDF" in win.report.summary),
          str(BOXES[-1:]))
    check("取消后进度条复位、✓ 痕迹清掉（不假装生成完了）",
          win.progress.value() == 0 and not win.results,
          f"progress={win.progress.value()} results={len(win.results)}")

    # ---------------------------------------------------------------- 关窗口
    print("\n=== 关窗口 ===")
    win.files.clear()
    win.rebuild_list()
    win.add_files([str(SAMPLES / f"样张{i}.jpg") for i in (1, 2, 3, 4)])
    win.dpi_spin.setValue(150)
    win.start()
    QApplication.processEvents()
    running_before = win._running
    win.close()
    QApplication.processEvents()
    check("运行中关窗口会安全收尾（不留下跑着的线程）",
          not running_before or (not win._running and not win.isVisible()),
          f"running_before={running_before} / after={win._running}")

    # 关窗口时的退出路径必须能在「没有控制台」的 frozen 环境下跑：
    # windowed 版 sys.stdout / sys.stderr 都是 None，旧代码直接 .flush()
    # 会抛 AttributeError，被 PyInstaller 弹成「Unhandled exception in script」。
    saved_out, saved_err = sys.stdout, sys.stderr
    flush_ok, flush_err = True, ""
    try:
        sys.stdout = None
        sys.stderr = None
        m.flush_streams()
    except Exception as exc:  # noqa: BLE001 - 这里就是要兜住任何异常
        flush_ok, flush_err = False, f"{type(exc).__name__}: {exc}"
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
    check("无控制台（stdout/stderr 为 None）时退出不报错", flush_ok, flush_err)

    rc = 0 if _failed == 0 else 1
    print(f"\n合计 {_passed}/{_passed + _failed} 项通过")
    print("截图：" + str(SHOTS))
    # 解释器收尾时 GC 销毁 Qt 对象的顺序不可控，会触发 0xC0000409 静默崩溃
    # 把缓冲输出吞掉；该存的（样张 / 截图）都已落盘，直接硬退出。
    # 复用 main 的 flush_streams：窗口版 exe 无控制台时 stdout 是 None。
    m.flush_streams()
    os._exit(rc)


if __name__ == "__main__":
    sys.exit(main())
