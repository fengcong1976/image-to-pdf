"""PySide6 桌面界面。

界面约定（主窗体只做三件事：列表选择 / 页面排版 / 预览）：
- 左边是缩略图列表：拖入图片、按住缩略图上下拖改页序、双击或点「编辑选中」
  进编辑窗口。
- 右边全是「整份 PDF」的事：「页面排版」（纸张 / 清晰度 / 边距 / 拼版 / 方向 /
  图间距）、「页码与页脚」、「PDF 输出」，下面常驻的「成品预览」就是将要生成的
  那一页。
- 右栏的「成品预览」只读，只回答「这一页对不对」。要动手就点「页面编辑…」，
  另开一个整页的大窗口（PageEditor）：拖图换位置、拖四角改大小、加文字 /
  方框 / 箭头 / 涂黑 / 签名图都在那里。位置按页面比例存进 ImageInfo.place，
  标注进 core/annotate，预览与出片共用同一套换算，看到的就是印出来的。
- 图像修复与输出不在主窗体：全部收进一个「编辑选中」窗口。窗口里那组修复
  参数默认只作用于当前这张，勾上「同时用于其它图片」就是这一批的默认值。
  点「应用」时顺手把处理后的图写进原图旁的 out，改完当场就能拿到图。
- 「编辑选中」对单张的覆盖写进 ImageInfo.tune，核心在 render_page 里统一
  合并，预览和成品不会分叉。

原图只读、PDF 只写在独立目录，所以「生成」这一步本身不需要撤销——不满意就改
了再生成一份。真正容易手滑的是页面编辑：标注和摆放支持撤销 / 重做（Ctrl+Z /
Ctrl+Y），栈只活在这次运行里，关掉程序就没了。
「关于」只放标准身份信息；用法、有损披露与可验证事实单独放「使用说明」。
"""
from __future__ import annotations

import collections
import copy
import gc
import html
import math
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import (
    QEventLoop, QPoint, QPointF, QRect, QRectF, QSize, Qt, QThread, QTimer,
    QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QIcon, QImage,
    QImageReader, QKeySequence, QPainter, QPen, QPixmap, QPolygonF, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListView, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QDoubleSpinBox, QPlainTextEdit,
    QProgressBar, QProgressDialog, QPushButton, QScrollArea, QSizePolicy,
    QSplitter, QSpinBox, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from PIL import Image, ImageOps

from core import annotate, imaging
from core.about import (
    APP_NAME, DISCLOSURE, HOMEPAGE, USAGE_SHORT, WEBSITE,
)
from core.compliance import NetworkAudit
from core.pdfbuild import content_rect, text_reserve
from core.converter import (
    clean_caption, collect_images, preview_page, save_processed_images,
    sheet_count, sheet_layers,
)
from core.presets import (
    COLOR_NAMES, LAYOUT_NAMES, ORIENTATION_NAMES, PAGE_FORMATS, PAGE_POSITIONS,
    PAPER_AUTO, PAPER_CHOICES, SHADOW_NAMES, ConvertOptions,
)

ICON_PATH = Path(__file__).resolve().parent / "resources" / "app.ico"
APP_USER_MODEL_ID = "Chuangxuan.ImageToPdf"

FILE_FILTER = ("图片 (*.jpg *.jpeg *.jpe *.jfif *.png *.webp *.bmp *.tif *.tiff "
               "*.heic *.heif)")
SUPPORT_TEXT = "jpg / png / webp / bmp / tif / heic"
EMPTY_HINT = "把图片或文件夹拖到这里\n书、文档、票据、截图、照片都可以"

# 一个下拉同时表达「几张一页」和「怎么摆」，比两个下拉少想一次
LAYOUT_CHOICES = [
    ("一页一张", 1, "single"),
    ("一页两张（上下）", 2, "col"),
    ("一页两张（左右）", 2, "row"),
    ("一页四张（2×2）", 4, "grid"),
]

PREVIEW_CACHE_MAX = 24
THUMB_SIZE = 150


def app_icon() -> QIcon:
    return QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()


def set_windows_app_id() -> None:
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def configure_app(app: QApplication) -> None:
    """默认可读性：入口统一设定，界面里不放字号开关。"""
    base_font = app.font()
    base_font.setPointSize(11)
    app.setFont(base_font)
    app.setStyleSheet(
        "QPushButton { min-height: 34px; padding: 4px 12px; }"
        # 输入框压到 28：右栏三组参数要放十个控件，32 会把成品预览挤成一条
        "QComboBox, QSpinBox, QLineEdit { min-height: 28px; padding: 1px 8px; }"
        "QProgressBar { min-height: 24px; }"
        "QCheckBox { padding: 2px 2px; }"
        "QGroupBox { font-weight: 600; margin-top: 12px; }"
        "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
        "QListWidget { background:#fafafa; border:1px solid #d9d9d9; }"
        "QListWidget::item:selected { background:#dce9f9; color:#123; }"
    )


class AboutDialog(QDialog):
    """「关于」只放身份信息，与站内其它项目一致。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        from core.about import APP_VERSION, COMPANY

        self.setWindowTitle(f"关于 {APP_NAME}")
        self.setWindowIcon(app_icon())
        self.resize(560, 300)

        root = QVBoxLayout(self)
        row = QHBoxLayout()
        pic = QLabel()
        pix = app_icon().pixmap(72, 72)
        if not pix.isNull():
            pic.setPixmap(pix)
        row.addWidget(pic)

        info = QLabel(
            f"<h3 style='margin:0'>{html.escape(APP_NAME)} {html.escape(APP_VERSION)}</h3>"
            f"<p>{html.escape(COMPANY)}</p>"
            f"<p>更多免费软件：<a href='{HOMEPAGE}'>{html.escape(WEBSITE)}</a></p>")
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setOpenExternalLinks(True)
        info.setWordWrap(True)
        row.addWidget(info, 1)
        root.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        btns.accepted.connect(self.accept)
        root.addWidget(btns)


class UsageDialog(QDialog):
    """使用说明：几步上手 + 有损披露 + 可自己核对的几件事。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} 使用说明")
        self.setWindowIcon(app_icon())
        self.resize(640, 480)

        root = QVBoxLayout(self)
        steps = "".join(f"<li>{html.escape(s)}</li>" for s in USAGE_SHORT)
        text = QLabel(
            f"<h3>怎么用</h3><ol>{steps}</ol>"
            f"<h3>要如实知道的两件事</h3><p>{html.escape(DISCLOSURE)}</p>"
            "<h3>可以自己核对</h3>"
            "<ul>"
            "<li>PDF 就在第一张图旁边的 out 文件夹里，另有同名 report.json</li>"
            "<li>report.json 里逐张记着用了什么参数、实际多少 dpi</li>"
            "<li>单独编辑过的图片，覆盖参数也写在报告里</li>"
            "<li>底部状态栏写的是这一批有没有联网（应为 0 次）</li>"
            "</ul>")
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.addWidget(text, 1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        btns.accepted.connect(self.accept)
        root.addWidget(btns)


class ImageCanvas(QLabel):
    """编辑窗口的显示画布：只负责按控件大小等比缩放摆图。

    「PS 式自由变换」整套已按用户要求取消：不再画蓝框 / 8 个手柄 / 旋转圆点，
    也不再接任何鼠标手势——画布上的拖动不会改动任何参数。预览就是一张
    干净的效果图，参数一改由预览线程重算出新图替换。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background:#f2f2f2; border:1px solid #c8c8c8;")
        self._src: QPixmap | None = None

    def place_image(self, pix: QPixmap | None) -> None:
        """按当前控件大小等比缩放摆图；重摆永远从源位图出发，不会逐次劣化。"""
        self._src = pix
        if pix is None or pix.isNull():
            self.setPixmap(QPixmap())
            return
        aw, ah = self.width() - 6, self.height() - 6
        if aw < 8 or ah < 8:
            return
        z = min(aw / pix.width(), ah / pix.height())
        pw = max(1, int(pix.width() * z))
        ph = max(1, int(pix.height() * z))
        self.setPixmap(pix.scaled(pw, ph,
                                  Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation))


class ImagePane(QWidget):
    """预览里的一块图片位：拿到图片按当前控件大小缩放，不糊不裁。"""

    def __init__(self, title: str, parent=None, min_size=(160, 150)):
        super().__init__(parent)
        self._pix: QPixmap | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        cap = QLabel(title)
        cap.setStyleSheet("color:#555; font-weight:400;")
        root.addWidget(cap)
        self.canvas = ImageCanvas()
        self.canvas.setMinimumSize(*min_size)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Expanding)
        root.addWidget(self.canvas, 1)

    def clear(self, text: str = "—") -> None:
        self._pix = None
        self.canvas.place_image(None)
        self.canvas.setText(text)

    def set_pil(self, im: Image.Image) -> None:
        self.set_pixmap(pil_to_pixmap(im))

    def set_pixmap(self, pix: QPixmap) -> None:
        self._pix = pix
        self.canvas.setText("")
        self._rescale()

    def resizeEvent(self, event):  # noqa: N802 — Qt 命名
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pix is None or self.canvas.width() < 8:
            return
        # 纯显示画布：按当前控件大小等比缩放摆图，不做任何变换
        self.canvas.place_image(self._pix)


def pil_to_pixmap(im: Image.Image) -> QPixmap:
    im = im.convert("RGB")
    if max(im.size) > 1600:
        ratio = 1600.0 / max(im.size)
        im = im.resize((max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                       Image.LANCZOS)
    buf = im.tobytes("raw", "RGB")
    qimg = QImage(buf, im.width, im.height, im.width * 3, QImage.Format.Format_RGB888)
    # QImage 只是借了 buf 的内存，必须先 copy 再转，否则 buf 被回收就是花屏
    return QPixmap.fromImage(qimg.copy())


def thumb_icon(path: Path, size: int = THUMB_SIZE) -> QIcon:
    """缩略图：JPEG 先 draft 降采样，避免几十张 4000px 原图把界面拖死。"""
    try:
        if path.suffix.lower() in (".heic", ".heif"):
            imaging.ensure_heif()
        with Image.open(path) as opened:
            try:
                opened.draft("RGB", (size * 2, size * 2))
            except Exception:
                pass
            try:
                im = ImageOps.exif_transpose(opened)
            except Exception:
                im = opened.copy()
            im = im.convert("RGB")
            im.thumbnail((size - 8, size - 8), Image.LANCZOS)
            canvas = Image.new("RGB", (size, size), (246, 247, 249))
            canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
        qimg = QImage(canvas.tobytes("raw", "RGB"), size, size, size * 3,
                      QImage.Format.Format_RGB888)
        return QIcon(QPixmap.fromImage(qimg.copy()))
    except Exception:
        # 打不开的图给灰底占位，别让整批加不进来
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.lightGray)
        return QIcon(pix)


# 关窗时若线程还没跑完，绝不能在窗口析构里丢掉 QThread 引用（会直接 abort
# 进程）。用模块级集合托到 finished 再释放，进程收尾时也不会提前析构。
_ORPHAN_THREADS: set = set()


class PreviewWorker(QThread):
    """跑一张的完整处理链（不写任何文件）。"""

    done = Signal(int, object, object, object, object, str, object)

    def __init__(self, token: int, info, opt: ConvertOptions, dpi: int, index: int,
                 *, sig=None, parent=None):
        super().__init__(parent)
        self.token = token
        self.info = info
        self.opt = opt
        self.dpi = dpi
        self.index = index
        self.sig = sig

    def run(self):
        try:
            src, page, rep, box = preview_page(
                self.info, self.opt, self.dpi, self.index)
            self.done.emit(self.token, src, page, rep, box, "", self.sig)
        except BaseException as exc:  # noqa: BLE001 — 线程里漏出去会带崩进程
            self.done.emit(self.token, None, None, None, None,
                           f"{type(exc).__name__}: {exc}", self.sig)


class LivePreview(QWidget):
    """编辑窗口的单画布预览：一张图占满，参数一改立即重算（即时预览）。

    打开窗口先显示原图（Qt 缩放解码，几十毫秒），后台算当前参数的效果，
    算完自动替换——进入就能看到画面，不用再点「看效果」。
    之后任何参数变化都走 300ms 防抖自动重算：连续拖动旋转/改档位不会
    疯狂起线程，停下手指令立刻出图。

    线程安全：worker 列表持有到 finished、token 丢弃过期结果、关窗前 shutdown()。
    这些不是形式主义——预览线程对象被提前回收会直接让整个进程消失。
    """

    DEBOUNCE_MS = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self.files: list[Path] = []
        self.tunes: dict[str, dict] = {}
        self.index = 0
        self.opt = ConvertOptions()
        self._token = 0
        self._workers: list[PreviewWorker] = []
        self._cache: dict[tuple, tuple] = {}
        self._orig_key: str | None = None
        self._orig_pix: QPixmap | None = None
        self._closed = False

        root = QVBoxLayout(self)
        head = QHBoxLayout()
        self.title = QLabel("预览")
        self.title.setStyleSheet("font-weight:600;")
        head.addWidget(self.title, 1)
        self.btn_prev = QPushButton("‹")
        self.btn_prev.setFixedWidth(38)
        self.btn_prev.setToolTip("上一张")
        self.btn_prev.clicked.connect(lambda: self.set_index(self.index - 1))
        head.addWidget(self.btn_prev)
        self.btn_next = QPushButton("›")
        self.btn_next.setFixedWidth(38)
        self.btn_next.setToolTip("下一张")
        self.btn_next.clicked.connect(lambda: self.set_index(self.index + 1))
        head.addWidget(self.btn_next)
        # 编辑窗口只放一张图，翻页箭头没有意义，直接收起来
        self.btn_prev.hide()
        self.btn_next.hide()
        root.addLayout(head)

        self.pane = ImagePane("当前效果")
        root.addWidget(self.pane, 1)

        # 参数变化 → 防抖后自动重算；render_now() 仍可绕过防抖立刻算
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self._start)

        self.info = QLabel("还没有图片")
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setWordWrap(True)
        self.info.setMinimumHeight(34)
        self.info.setStyleSheet("color:#333; font-weight:400;")
        root.addWidget(self.info)
        self.status = QLabel("")
        self.status.setStyleSheet("color:#666;")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._show_empty()

    # ------------------------------------------------------------ 外部接口
    def set_files(self, paths, index=None, opt: ConvertOptions | None = None,
                  tunes: dict | None = None) -> None:
        self._token += 1   # 换了一批图，在途结果全部作废
        self.files = [Path(p) for p in paths]
        self.tunes = {str(k): dict(v) for k, v in (tunes or {}).items() if v}
        if opt is not None:
            self.opt = opt
        if index is not None:
            self.index = index
        self.index = max(0, min(self.index, max(0, len(self.files) - 1)))
        if not self.files:
            self._show_empty()
            return
        self._render_head()
        self._show_idle()

    def set_index(self, index: int) -> None:
        self._token += 1   # 翻页期间旧图的结果不能落到新图上
        if not self.files:
            return
        self.index = max(0, min(int(index), len(self.files) - 1))
        self._render_head()
        self._show_idle()

    def set_options(self, opt: ConvertOptions) -> None:
        self._token += 1   # 参数一变，旧参数算到一半的结果立即作废
        self.opt = opt
        self._cache.clear()
        # 防抖自动重算：连续改参数只算最后一次，停手 300ms 内出图
        if not self._closed and self.files:
            self._debounce.start()
            self.status.setText("参数已更新，正在重算…")

    def render_now(self) -> None:
        """绕过防抖立刻按当前参数算一次。"""
        if self._closed:
            return
        self._debounce.stop()
        self._start()

    def goto(self, index: int) -> None:
        self.set_index(index)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._debounce.stop()
        for worker in list(self._workers):
            try:
                worker.done.disconnect(self._on_done)
            except (TypeError, RuntimeError):
                pass
            if worker.isRunning():
                worker.wait(8000)
            if worker.isRunning():
                # 8 秒还没算完的大图：把线程交给模块级集合托住，等它自己
                # finished 后再释放，绝不在这里让 QThread 随窗口一起析构
                _ORPHAN_THREADS.add(worker)
                def _release(w=worker):
                    _ORPHAN_THREADS.discard(w)
                    w.deleteLater()
                worker.finished.connect(_release)
            try:
                self._workers.remove(worker)
            except ValueError:
                pass
        self._cache.clear()
        self._orig_key = None
        self._orig_pix = None

    # ------------------------------------------------------------ 内部
    def _render_head(self) -> None:
        if not self.files:
            self.title.setText("预览")
        elif len(self.files) == 1:
            self.title.setText("当前效果（参数一改立即重算）")
        else:
            self.title.setText(f"预览 · 第 {self.index + 1} / {len(self.files)} 张"
                               f" · {self.files[self.index].name}")
        self.btn_prev.setEnabled(self.index > 0)
        self.btn_next.setEnabled(bool(self.files) and self.index < len(self.files) - 1)
        self.btn_prev.setVisible(len(self.files) > 1)
        self.btn_next.setVisible(len(self.files) > 1)

    def _sig(self) -> tuple:
        if not self.files:
            return ()
        key = str(self.files[self.index])
        return (key, self.index, self.opt.signature(),
                repr(self.tunes.get(key)))

    def _start(self) -> None:
        if self._closed or not self.files:
            return
        sig = self._sig()
        self._render_head()
        if sig in self._cache:
            self._token += 1   # 直接出缓存也要作废在途 worker，否则旧图会盖回来
            self._apply(*self._cache[sig])
            return
        # 画布上已有的画面（原图或上一版效果）先留着，别清成空白闪一下
        if self.pane._pix is None:
            self.pane.clear("正在处理…")
        self.info.setText("正在按当前参数计算…")
        try:
            found, _ = collect_images([str(self.files[self.index])],
                                      sort_by_exif=False)
        except Exception as exc:  # noqa: BLE001
            self._show_error(f"读不了这张图：{type(exc).__name__}: {exc}")
            return
        if not found:
            self._show_error("这张图打不开（格式不支持或文件损坏）。")
            return
        info = found[0]
        tune = self.tunes.get(str(self.files[self.index]))
        info.tune = dict(tune) if tune else None
        self._token += 1
        worker = PreviewWorker(self._token, info, self.opt, int(self.opt.dpi),
                              self.index, sig=sig)
        worker.done.connect(self._on_done)
        worker.finished.connect(lambda w=worker: self._drop_worker(w))
        self._workers.append(worker)
        worker.start()

    def _drop_worker(self, worker: PreviewWorker) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass

    def _on_done(self, token: int, src, page, rep, box, err: str, sig) -> None:
        if self._closed or token != self._token:
            return   # 参数又变了 / 翻到别的张了，这份结果作废
        if err:
            self._show_error(f"预览失败：{err}")
            return
        # 双保险：只有「启动这份结果时」的参数指纹仍等于当前参数才收下。
        # 否则旧参数的图会被写进新参数的缓存键，之后一直命中脏缓存。
        if sig is not None and sig != self._sig():
            return
        result = (src, page, rep, box)
        key = sig if sig is not None else self._sig()
        self._cache[key] = result
        if len(self._cache) > PREVIEW_CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        self._apply(*result)

    def _apply(self, src, page, rep, box) -> None:
        self.pane.set_pil(page)
        pw, ph = box[0], box[1]
        bits = [f"处理后 {page.width}×{page.height}",
                f"页面 {pw:.0f}×{ph:.0f} pt · 实际 {rep.dpi:.0f} dpi",
                f"第 {rep.page} 页", rep.geometry or "原样嵌入"]
        # 处理链文字里可能已含「去阴影（标准）」，别再重复写一遍
        if rep.shadow != "关" and "去阴影" not in rep.geometry:
            bits.append(f"去阴影（{rep.shadow}）")
        self.info.setText(" · ".join(bits))
        tail = ""
        if rep.note:
            tail = "　<span style='color:#b45309'>" + html.escape(rep.note) + "</span>"
        self.status.setText("按当前参数实时算出，和成品同一条处理链" + tail)

    def _show_error(self, msg: str) -> None:
        self.pane.clear("—")
        self.info.setText(f"<span style='color:#b91c1c'>{html.escape(msg)}</span>")
        self.status.setText("预览失败")

    def _load_original(self) -> QPixmap | None:
        """读当前这张的原图（按显示尺寸降采样），供编辑窗口一打开就有画面。

        先用 Qt 的缩放解码：一张 2400px 的 JPEG 只要 20–40ms，比 PIL 解码+缩放
        快三倍多，开窗不会顿一下。Qt 不认识的格式（HEIC 等）再退回 PIL。
        同一张只读一次，翻页才重读；读失败返回 None，由调用方给提示、不抛异常。
        """
        if not self.files:
            return None
        path = self.files[self.index]
        key = str(path)
        if key == self._orig_key and self._orig_pix is not None:
            return self._orig_pix
        pix = self._read_original_qt(path) or self._read_original_pil(path)
        if pix is None:
            return None
        self._orig_key = key
        self._orig_pix = pix
        return pix

    @staticmethod
    def _read_original_qt(path: Path) -> QPixmap | None:
        """Qt 缩放解码；setAutoTransform 顺手把 EXIF 方向掰正，竖拍不会躺倒。"""
        try:
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and max(size.width(), size.height()) > 1400:
                scale = 1400.0 / max(size.width(), size.height())
                reader.setScaledSize(QSize(max(1, int(size.width() * scale)),
                                           max(1, int(size.height() * scale))))
            img = reader.read()
        except Exception:  # noqa: BLE001 — 读图失败只是没画面，不该带崩窗口
            return None
        if img.isNull():
            return None
        return QPixmap.fromImage(img.convertToFormat(QImage.Format.Format_RGB32))

    @staticmethod
    def _read_original_pil(path: Path) -> QPixmap | None:
        """Qt 认不出的格式（HEIC / 少见 TIFF）走 PIL，与缩略图同一条路。"""
        try:
            if path.suffix.lower() in (".heic", ".heif"):
                imaging.ensure_heif()
            with Image.open(path) as opened:
                try:
                    opened.draft("RGB", (1400, 1400))
                except Exception:
                    pass
                try:
                    im = ImageOps.exif_transpose(opened)
                except Exception:
                    im = opened.copy()
                im = im.convert("RGB")
                im.thumbnail((1200, 1200), Image.LANCZOS)
            return pil_to_pixmap(im)
        except Exception:  # noqa: BLE001 — 同上
            return None

    def _show_empty(self) -> None:
        self.pane.clear("—")
        self.info.setText("还没有图片")
        self.status.setText("")

    def _show_idle(self) -> None:
        """刚打开 / 翻到另一张：先把原图摆上（几十毫秒），随后自动算当前效果。

        进窗口就有画面，算完自动替换成处理后的图——不需要任何按钮。
        """
        pix = self._load_original()
        if pix is not None:
            self.pane.set_pixmap(pix)
        else:
            self.pane.clear("这张图打不开")
        self.info.setText("参数已就绪")
        self.status.setText("正在准备预览…")
        self._debounce.start()


class SheetRenderer(QThread):
    """成品预览的渲染线程：一个常驻线程按请求顺序排页。

    为什么不在主线程算：一页要走完每张图的找纸 / 纠偏 / 去阴影，0.5–1 秒，
    卡在界面线程上就是明显的顿挫。

    为什么用队列而不是每页开线程：翻页时「当前页」要能插队，前后页要能提前
    排上；同时开几个线程只会互相抢 CPU。任务里带 items 和 opt，参数一变旧任务
    直接作废，不用重建线程。

    交回来的不是一张烤好的整页图，而是 sheet_layers 的分层结果：白纸一张 +
    每张图的矩形与像素。界面因此能在拖动时只改矩形就把图挪到位。
    """

    done = Signal(int, int, object, str)   # gen, page, layers, err

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs: collections.deque[tuple] = collections.deque()
        self._wake = threading.Condition()
        self._stop = False

    def request(self, gen: int, page_no: int, items, opt: ConvertOptions, *,
                urgent: bool = False) -> None:
        with self._wake:
            self._jobs = collections.deque(j for j in self._jobs if j[1] != page_no)
            job = (int(gen), int(page_no), list(items), opt)
            if urgent:
                self._jobs.appendleft(job)
            else:
                self._jobs.append(job)
            self._wake.notify()

    def clear(self) -> None:
        with self._wake:
            self._jobs.clear()

    def stop(self) -> None:
        with self._wake:
            self._stop = True
            self._jobs.clear()
            self._wake.notify_all()

    def run(self) -> None:
        while True:
            with self._wake:
                while not self._stop and not self._jobs:
                    self._wake.wait(0.2)
                if self._stop:
                    return
                gen, page_no, items, opt = self._jobs.popleft()
            try:
                data = sheet_layers(items, opt, page_no)
                err = ""
            except BaseException as exc:  # noqa: BLE001 — 线程里漏出去会带崩进程
                data, err = None, f"{type(exc).__name__}: {exc}"
            if not self._stop:
                self.done.emit(gen, page_no, data, err)


def _clamp_delta(delta: float, lo: float, hi: float) -> float:
    """把位移夹进 [lo, hi]；lo > hi 说明这张图比可摆放区域还大，就不让它动。"""
    if lo > hi:
        return 0.0
    return max(lo, min(hi, delta))


class SheetView(QWidget):
    """整页画布：一张白纸，外加每张图按自己的矩形贴上去，最上面是标注。

    为什么不把整页烤成一张 pixmap：手动排版要跟手，烤图就得每动一下就重排
    一整页（0.3–1 秒），拖起来像在拉一张湿纸。分层之后拖动只改一个矩形，
    松手再让引擎按真实参数重排一次，看得见的和印出来的一致。

    交互（只在「页面编辑」打开时生效）：
    - 点中一张图 / 一条标注 = 选中；拖动 = 在本页里移动（不会跑到纸外）
    - 四角小方块 = 缩放。图锁原图比例（不拉伸，避免把人脸压扁），标注自由缩放
    - 选了工具（文字 / 方框 / 箭头 / 涂黑 / 签名图）后在纸上拖一下就画出来
    - 方向键微移 1pt，Shift+方向键 10pt；Delete 删掉选中的标注
    """

    boxSelected = Signal(int)          # items 下标；-1 = 没选中
    boxChanged = Signal(int, object)   # items 下标, {"x","y","w","h"}（页面比例）
    annoSelected = Signal(int)         # 本页标注下标；-1 = 没选中
    annoChanged = Signal(int, dict)    # 标注下标, {"x","y","w","h"}
    annoCreated = Signal(str, dict, bool)   # kind, 页面比例矩形, 是否反向
    annoDeleted = Signal(int)          # 标注下标
    annoEdited = Signal(int)           # 双击文字标注，请求改内容
    modeRequested = Signal(str)        # 画布要求切回某个工具（Esc）

    PAD = 10
    HIT = 7.0          # 命中容差（屏幕像素，换算成 pt 再比）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet("background:#e6e7e9;")
        self._bg: QPixmap | None = None
        self._page: tuple[float, float] | None = None
        self._layers: list[dict] = []
        self._annos: list[dict] = []
        self._scaled: dict[int, tuple[tuple[int, int], QPixmap]] = {}
        self._stamps: dict[str, QPixmap] = {}
        self._editable = False
        self._sel = -1
        self._sel_anno = -1
        self._drag: dict | None = None
        self._rubber: list[float] | None = None    # 正在拖出来的标注矩形（pt）
        # 拖拽中的箭头预览用这两项画：颜色 / 粗细跟工具条当前选择走
        self._preview_color = "#d0021b"
        self._preview_width = 2.6
        self._mode = "select"
        self._images_locked = False
        self._placeholder = ""
        self._limits: tuple[float, float, float, float] | None = None

    # ------------------------------------------------------------ 数据
    def clear(self, text: str = "") -> None:
        self._bg = None
        self._page = None
        self._layers = []
        self._annos = []
        self._scaled.clear()
        self._sel = -1
        self._sel_anno = -1
        self._drag = None
        self._rubber = None
        self._placeholder = text
        self.update()

    def set_page(self, bg, page_pt, layers, annos=None) -> None:
        """bg 是白纸（含页码/页脚），layers 是 [(下标, PIL 图, 矩形 pt, 图注, 字号)]。

        图注挂在图层里而不是画进白纸：拖动 / 缩放这一张时，那行字要贴着图走。
        前三个元素是老格式，多出来的两个缺了就当没有图注。

        annos 是这一页的标注（文字 / 方框 / 箭头 / 涂黑 / 签名图），位置是
        0–1 的页面比例。它们在画布上单独画，所以改一条标注不用重排整页。
        """
        self._bg = pil_to_pixmap(bg) if bg is not None else None
        self._page = (float(page_pt[0]), float(page_pt[1])) if page_pt else None
        self._layers = []
        for item in layers:
            cap = str(item[3]) if len(item) > 3 and item[3] else ""
            size = float(item[4]) if len(item) > 4 else 9.0
            self._layers.append({
                "row": int(item[0]), "pix": pil_to_pixmap(item[1]),
                "rect": list(item[2]), "caption": cap, "cap_pt": size,
            })
        self._annos = [dict(a) for a in (annos or [])]
        self._scaled.clear()
        self._placeholder = ""
        if not (0 <= self._sel < len(self._layers)):
            self._sel = -1
        if not (0 <= self._sel_anno < len(self._annos)):
            self._sel_anno = -1
        self.update()

    def set_annotations(self, annos) -> None:
        """只换标注层：加一条、删一条、改颜色之后用，不碰图片层。"""
        self._annos = [dict(a) for a in (annos or [])]
        if not (0 <= self._sel_anno < len(self._annos)):
            self._sel_anno = -1
        self.update()

    def set_mode(self, mode: str) -> None:
        """切工具。切到绘图工具时清掉选中，免得「拖一下变成移动那张图」。"""
        self._mode = mode if mode in ("select", "text", "box", "arrow",
                                      "redact", "stamp") else "select"
        self._rubber = None
        if self._mode != "select":
            self._sel = -1
            self._sel_anno = -1
            self.boxSelected.emit(-1)
            self.annoSelected.emit(-1)
        self.unsetCursor()
        self.update()

    @property
    def mode(self) -> str:
        return self._mode

    def set_editable(self, on: bool) -> None:
        self._editable = bool(on)
        if not self._editable:
            self._sel = -1
            self._sel_anno = -1
            self._drag = None
            self._rubber = None
            self._mode = "select"
            self.boxSelected.emit(-1)
            self.annoSelected.emit(-1)
        self.unsetCursor()
        self.update()

    def set_limits(self, rect) -> None:
        """可摆放范围（版心，pt）。图不许越过边距，更不许掉进页码那条带里。"""
        self._limits = tuple(float(v) for v in rect) if rect else None

    def limits(self) -> tuple[float, float, float, float]:
        if self._limits is not None:
            return self._limits
        if self._page is None:
            return (0.0, 0.0, 0.0, 0.0)
        return (0.0, 0.0, self._page[0], self._page[1])

    @property
    def editable(self) -> bool:
        return self._editable

    def selected_row(self) -> int:
        if 0 <= self._sel < len(self._layers):
            return self._layers[self._sel]["row"]
        return -1

    def page_size(self) -> tuple[float, float] | None:
        return self._page

    def selected_rect(self) -> list[float] | None:
        if 0 <= self._sel < len(self._layers):
            return list(self._layers[self._sel]["rect"])
        return None

    def set_layer_rect(self, rect, *, emit: bool = False) -> None:
        """外部（居中 / 铺满按钮）直接改选中那张的矩形。"""
        if not (0 <= self._sel < len(self._layers)):
            return
        self._layers[self._sel]["rect"] = [float(v) for v in rect]
        self._scaled.pop(self._sel, None)
        self.update()
        if emit:
            self._emit_change()

    def selected_frac(self) -> dict:
        """选中那张的位置（页面比例），没选中就是空字典。"""
        if not (0 <= self._sel < len(self._layers)) or self._page is None:
            return {}
        pw, ph = self._page
        x0, y0, x1, y1 = self._layers[self._sel]["rect"]
        return {"x": max(0.0, min(1.0, x0 / pw)), "y": max(0.0, min(1.0, y0 / ph)),
                "w": max(0.001, min(1.0, (x1 - x0) / pw)),
                "h": max(0.001, min(1.0, (y1 - y0) / ph))}

    # ------------------------------------------------------------ 标注
    def selected_anno(self) -> int:
        return self._sel_anno if 0 <= self._sel_anno < len(self._annos) else -1

    def selected_anno_obj(self) -> dict | None:
        i = self.selected_anno()
        return dict(self._annos[i]) if i >= 0 else None

    def _anno_pt(self, a: dict) -> list[float]:
        """标注的 0–1 比例矩形 → 纸张 pt。和出片走的是同一套换算。"""
        if self._page is None:
            return [0.0, 0.0, 0.0, 0.0]
        pw, ph = self._page
        return annotate.to_pt(a, pw, ph)

    def _anno_arrow_pt(self, a: dict) -> list[float]:
        """箭头的 [起点x, 起点y, 终点x, 终点y]（pt）。位移带方向。

        箭头不是矩形，不能用 _anno_pt（它会把包围盒规范化，方向信息就丢了）；
        拖动 / 拖端点全靠这份「有符号矩形」来回算。
        """
        if self._page is None:
            return [0.0, 0.0, 0.0, 0.0]
        p1, p2 = annotate.arrow_ends(a, self._page[0], self._page[1])
        return [p1[0], p1[1], p2[0], p2[1]]

    def _hit_anno(self, pt) -> int:
        """标注压在图上面，所以先在标注里找。细框也点得中靠的是 _tol。

        箭头按「点到线段的距离」判，点包围盒空白不算命中——不然两条交叉的
        箭头没法各选各的，用户也会觉得「明明点在箭头上却选中了旁边那个框」。
        容差随线宽走：粗箭头线本身就有几个像素宽，得连着线一起算上。
        """
        t = self._tol()
        k = max(0.05, self._k())
        for i in reversed(range(len(self._annos))):
            a = self._annos[i]
            if a.get("kind") == "arrow":
                p1, p2 = annotate.arrow_ends(a, self._page[0], self._page[1])
                tol = max(t, float(a.get("width") or 1.6) * k * 0.5 + 2.0 / k)
                if annotate._seg_dist(pt[0], pt[1], p1, p2) <= tol:
                    return i
                continue
            x0, y0, x1, y1 = self._anno_pt(a)
            if x0 - t <= pt[0] <= x1 + t and y0 - t <= pt[1] <= y1 + t:
                return i
        return -1

    def _arrow_handle(self, index: int, pt) -> int | None:
        """点到了箭头的哪个端点？0=起点 1=终点（箭头尖），都没碰到返回 None。"""
        p1, p2 = annotate.arrow_ends(self._annos[index],
                                     self._page[0], self._page[1])
        t = self._tol() * 1.2
        if math.hypot(pt[0] - p1[0], pt[1] - p1[1]) <= t:
            return 0
        if math.hypot(pt[0] - p2[0], pt[1] - p2[1]) <= t:
            return 1
        return None

    def delete_anno(self) -> None:
        """删掉选中的那条标注。数据在主窗体，这里只发信号。

        先把自己这份删掉再发信号：槽里收到通知后可能整份重建列表
        （``set_annotations`` 一灌，``_annos`` 就是新对象了），要是先发信号
        再删，后面那句 pop 删掉的就是别人的第二条。
        """
        i = self.selected_anno()
        if not (0 <= i < len(self._annos)):
            return
        self._annos.pop(i)
        self._sel_anno = -1
        self.annoDeleted.emit(i)
        self.annoSelected.emit(-1)
        self.update()

    def clear_anno_selection(self) -> None:
        """撤销/重做之后用：列表整份换过，旧下标可能指向别的标注了。"""
        if self._sel_anno < 0:
            return
        self._sel_anno = -1
        self.annoSelected.emit(-1)
        self.update()

    def _anno_frac(self, i: int) -> dict:
        a = self._annos[i]
        return {"x": a["x"], "y": a["y"], "w": a["w"], "h": a["h"]}

    # ------------------------------------------------------------ 几何
    def _disp(self) -> QRect:
        """纸在控件里占的那块（等比、居中、四周留 PAD）。"""
        if self._page is None:
            return QRect()
        pw, ph = self._page
        avail_w = max(1, self.width() - 2 * self.PAD)
        avail_h = max(1, self.height() - 2 * self.PAD)
        k = min(avail_w / pw, avail_h / ph)
        w, h = max(1, round(pw * k)), max(1, round(ph * k))
        return QRect((self.width() - w) // 2, (self.height() - h) // 2, w, h)

    def _k(self) -> float:
        disp = self._disp()
        return disp.width() / self._page[0] if self._page else 1.0

    def _to_pt(self, pos) -> tuple[float, float]:
        disp = self._disp()
        k = self._k()
        return ((pos.x() - disp.x()) / k, (pos.y() - disp.y()) / k)

    def _to_px(self, rect_pt) -> QRect:
        disp = self._disp()
        k = self._k()
        return QRect(round(disp.x() + rect_pt[0] * k), round(disp.y() + rect_pt[1] * k),
                     max(1, round((rect_pt[2] - rect_pt[0]) * k)),
                     max(1, round((rect_pt[3] - rect_pt[1]) * k)))

    def _tol(self) -> float:
        return self.HIT / max(0.05, self._k())

    def _corner(self, index: int, pt) -> str:
        """点到了哪个角？返回 nw/ne/sw/se，都没碰到就空串。"""
        x0, y0, x1, y1 = self._layers[index]["rect"]
        t = self._tol()
        near_l, near_r = abs(pt[0] - x0) <= t, abs(pt[0] - x1) <= t
        near_t, near_b = abs(pt[1] - y0) <= t, abs(pt[1] - y1) <= t
        if near_l and near_t:
            return "nw"
        if near_r and near_t:
            return "ne"
        if near_l and near_b:
            return "sw"
        if near_r and near_b:
            return "se"
        return ""

    def _hit(self, pt) -> int:
        """从上往下找：后画的图压在上面，点中的应该是它。"""
        if self._images_locked:
            return -1     # 自适应纸张下页面跟着图走，拖位置没有意义
        for i in reversed(range(len(self._layers))):
            x0, y0, x1, y1 = self._layers[i]["rect"]
            if x0 <= pt[0] <= x1 and y0 <= pt[1] <= y1:
                return i
        return -1

    def set_images_locked(self, on: bool) -> None:
        """锁住「拖图片」，但保留标注。

        自适应纸张下页面比例就是图片的比例，把图拖到别处没有意义（引擎也会
        忽略），所以那里只让用户加标注、不让拖图。以前是干脆把整个编辑功能
        关掉，结果用自适应纸张的人一个批注都加不了——那是误伤。
        """
        self._images_locked = bool(on)
        if self._images_locked:
            self._sel = -1
            self.boxSelected.emit(-1)
        self.unsetCursor()
        self.update()

    # ------------------------------------------------------------ 绘制
    def _scaled_pix(self, index: int, size) -> QPixmap:
        key = (max(1, size.width()), max(1, size.height()))
        hit = self._scaled.get(index)
        if hit is not None and hit[0] == key:
            return hit[1]
        pix = self._layers[index]["pix"].scaled(
            QSize(*key), Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self._scaled[index] = (key, pix)
        return pix

    def paintEvent(self, event):  # noqa: N802 — Qt 命名
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#e6e7e9"))
        if self._bg is None or self._page is None:
            painter.setPen(QColor("#666"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             self._placeholder or "…")
            return
        disp = self._disp()
        painter.drawPixmap(disp, self._bg)
        for i, layer in enumerate(self._layers):
            box = self._to_px(layer["rect"])
            painter.drawPixmap(box, self._scaled_pix(i, box.size()))
        self._paint_captions(painter)
        self._paint_annos(painter)
        if self._editable:
            self._paint_frames(painter)

    def _paint_annos(self, painter: QPainter) -> None:
        """把这一页的标注画上去。

        画的是和出片同一份几何（annotate.to_pt），只是单位从 pt 换成屏幕像素，
        所以画布上看到的粗细、折行、箭头长度和 PDF 里基本一致。
        """
        if not self._annos or self._page is None:
            return
        k = self._k()
        painter.save()
        for i, a in enumerate(self._annos):
            box = self._to_px(self._anno_pt(a))
            if box.width() <= 0 or box.height() <= 0:
                continue
            col = QColor(a.get("color") or "#000000")
            kind = a.get("kind")
            if kind == "redact":
                painter.fillRect(box, col)
            elif kind == "box":
                pen = QPen(col)
                pen.setWidthF(max(1.0, float(a.get("width") or 1.6) * k))
                painter.setPen(pen)
                painter.setBrush(col if a.get("fill") else Qt.BrushStyle.NoBrush)
                painter.drawRect(box.adjusted(0, 0, -1, -1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            elif kind == "arrow":
                self._paint_arrow(painter, a, col, k)
            elif kind == "stamp":
                pix = self._stamp_pix(a.get("src") or "")
                if not pix.isNull():
                    # 和出片一样等比放进框里居中，别把人家的签名拉扁
                    size = pix.size().scaled(box.size(),
                                             Qt.AspectRatioMode.KeepAspectRatio)
                    scaled = pix.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
                    painter.drawPixmap(
                        box.left() + (box.width() - scaled.width()) // 2,
                        box.top() + (box.height() - scaled.height()) // 2, scaled)
            elif kind == "text":
                self._paint_anno_text(painter, a, box, col)
        painter.restore()

    def _paint_arrow(self, painter: QPainter, a: dict, col: QColor,
                     k: float) -> None:
        if self._page is None:
            return
        p1, p2 = annotate.arrow_ends(a, self._page[0], self._page[1])
        self._paint_arrow_line(painter, p1, p2, col,
                               float(a.get("width") or 1.6), k)

    def _paint_arrow_line(self, painter: QPainter, p1, p2, col: QColor,
                          width_pt: float, k: float) -> None:
        """从起点到终点画一支箭头（实心三角头）。拖拽预览和成型的箭头共用，
        所以「拖的时候看到什么样，松手就是什么样」。"""
        q1 = self._pt_to_px(*p1)
        q2 = self._pt_to_px(*p2)
        dx, dy = q2.x() - q1.x(), q2.y() - q1.y()
        length = math.hypot(dx, dy)
        if length < 2:
            return
        w_px = max(1.0, width_pt * k)
        head = max(6.0, min(22.0 * k, length * 0.22 + w_px * 2.0))
        ux, uy = dx / length, dy / length
        base = QPointF(q2.x() - ux * head, q2.y() - uy * head)
        pen = QPen(col)
        pen.setWidthF(w_px)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(q1, base)
        half = head * 0.42
        nx, ny = -uy, ux
        tri = QPolygonF([
            q2,
            QPointF(base.x() + nx * half, base.y() + ny * half),
            QPointF(base.x() - nx * half, base.y() - ny * half),
        ])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(col)
        painter.drawPolygon(tri)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_anno_text(self, painter: QPainter, a: dict, box: QRect,
                         col: QColor) -> None:
        px = max(6, round(float(a.get("size") or 14.0) * self._k()))
        font = QFont(self.font())
        font.setPixelSize(px)
        painter.setFont(font)
        painter.setPen(col)
        fm = painter.fontMetrics()
        lines = annotate.wrap_text(a.get("text") or "", box.width() - 2, px,
                                   lambda s, _size: fm.horizontalAdvance(s))
        if not lines:
            return
        lh = px * annotate.LINE_H
        lines = lines[:max(1, int(box.height() // lh))]
        top = box.top() + max(0.0, (box.height() - len(lines) * lh) / 2.0)
        for i, ln in enumerate(lines):
            w = fm.horizontalAdvance(ln)
            if a.get("align") == "center":
                x = box.left() + (box.width() - w) / 2.0
            elif a.get("align") == "right":
                x = box.right() - w
            else:
                x = box.left()
            painter.drawText(QPointF(x, top + i * lh + px * 0.98), ln)

    def _pt_to_px(self, x: float, y: float) -> QPointF:
        disp = self._disp()
        k = self._k()
        return QPointF(disp.x() + x * k, disp.y() + y * k)

    def _stamp_pix(self, path: str) -> QPixmap:
        """签名图缓存。用和出片同一套裁边逻辑，所见即所得。"""
        if path in self._stamps:
            return self._stamps[path]
        pix = QPixmap()
        try:
            im, alpha = annotate.load_stamp(path) if path else (None, None)
            if im is not None:
                if alpha is not None:
                    im = im.convert("RGBA")
                    im.putalpha(alpha)
                pix = pil_to_pixmap(im)
        except (OSError, ValueError):
            pix = QPixmap()
        self._stamps[path] = pix
        return pix

    def _paint_captions(self, painter: QPainter) -> None:
        """图注：贴在每张图的下沿居中，太长了按控件宽度省略。

        按 pt 字号乘当前缩放换算成像素，所以放大预览时字也跟着大，不会变成
        一行看不出来的小点。
        """
        k = self._k()
        for layer in self._layers:
            text = layer.get("caption")
            if not text:
                continue
            box = self._to_px(layer["rect"])
            px = max(7, round(float(layer.get("cap_pt") or 9.0) * k))
            font = QFont(self.font())
            font.setPixelSize(px)
            painter.setFont(font)
            painter.setPen(QColor("#292929"))
            fm = painter.fontMetrics()
            body = fm.elidedText(str(text), Qt.TextElideMode.ElideRight,
                                 max(24, box.width() + 8))
            # 图被拖到贴纸底时，字别画到纸外面去
            top = min(box.bottom() + round(px * 1.1), self._disp().bottom() - px)
            painter.drawText(QRect(box.left() - 6, top, box.width() + 12, px + 8),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                             body)

    def _paint_frames(self, painter: QPainter) -> None:
        for i, layer in enumerate(self._layers):
            box = self._to_px(layer["rect"])
            if i == self._sel:
                pen = QPen(QColor("#1f6fd0"))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.drawRect(box.adjusted(0, 0, -1, -1))
                painter.setBrush(QColor("#ffffff"))
                painter.setPen(QPen(QColor("#1f6fd0"), 2))
                for corner in self._handles(box):
                    painter.drawRect(corner)
                painter.setBrush(Qt.BrushStyle.NoBrush)
            elif self._sel_anno < 0:
                # 选了标注时不再给每张图描虚框，否则满屏都是框，看不清标在哪
                pen = QPen(QColor(120, 150, 200, 160))
                pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawRect(box.adjusted(0, 0, -1, -1))
        if 0 <= self._sel_anno < len(self._annos):
            sel = self._annos[self._sel_anno]
            if sel.get("kind") == "arrow" and self._page is not None:
                # 箭头是条线，不是块：矩形选框 + 四角点对它没有意义。
                # 两端各画一个圆点手柄——抓住哪头就能把哪头拖到新位置，
                # 方向和长度跟着走，跟微信截图的手感一致。
                p1, p2 = annotate.arrow_ends(sel, self._page[0], self._page[1])
                for p in (p1, p2):
                    q = self._pt_to_px(*p)
                    painter.setBrush(QColor("#ffffff"))
                    painter.setPen(QPen(QColor("#0b8a3e"), 2))
                    painter.drawEllipse(q, 5.0, 5.0)
                painter.setBrush(Qt.BrushStyle.NoBrush)
            else:
                box = self._to_px(self._anno_pt(sel))
                pen = QPen(QColor("#0b8a3e"))
                pen.setWidth(2)
                painter.setPen(pen)
                painter.drawRect(box.adjusted(-1, -1, 1, 1))
                painter.setBrush(QColor("#ffffff"))
                painter.setPen(QPen(QColor("#0b8a3e"), 2))
                for corner in self._handles(box):
                    painter.drawRect(corner)
                painter.setBrush(Qt.BrushStyle.NoBrush)
        if self._rubber is not None:
            if self._mode == "arrow" and self._page is not None:
                # 拖的时候直接画临时箭头，所见即所得——以前画的是个虚线矩形，
                # 松手却出来一支斜箭头，用户永远在猜「这框到底会变成什么」
                self._paint_arrow_line(
                    painter, (self._rubber[0], self._rubber[1]),
                    (self._rubber[2], self._rubber[3]),
                    QColor(self._preview_color), self._preview_width, self._k())
            else:
                pen = QPen(QColor("#0b8a3e"))
                pen.setStyle(Qt.PenStyle.DashLine)
                pen.setWidth(1)
                painter.setPen(pen)
                painter.drawRect(self._to_px(self._rubber).adjusted(0, 0, -1, -1))

    @staticmethod
    def _handles(box: QRect) -> list[QRect]:
        s = 8
        x0, y0 = box.left(), box.top()
        x1, y1 = box.right(), box.bottom()
        return [QRect(x0 - s // 2, y0 - s // 2, s, s),
                QRect(x1 - s // 2, y0 - s // 2, s, s),
                QRect(x0 - s // 2, y1 - s // 2, s, s),
                QRect(x1 - s // 2, y1 - s // 2, s, s)]

    # ------------------------------------------------------------ 交互
    def set_preview_style(self, color: str, width: float) -> None:
        """拖拽中的箭头用什么颜色 / 粗细——跟随工具条的当前选择。"""
        self._preview_color = str(color or "#d0021b")
        self._preview_width = max(1.0, float(width or 2.6))
        if self._rubber is not None:
            self.update()

    def _begin_anno_drag(self, anno: int, pt) -> None:
        """点中一条标注后决定怎么跟手。

        箭头是条线：抓住端点 = 拖这一头（改方向和长度），抓在中间 = 整条挪。
        其余标注是块：抓四角缩放，抓中间挪。rect0 对箭头存的是
        [起点x, 起点y, 终点x, 终点y]，位移带方向，一路到 make 都不做规范化。
        """
        a = self._annos[anno]
        if a.get("kind") == "arrow":
            end = self._arrow_handle(anno, pt)
            self._drag = {
                "mode": "endpoint" if end is not None else "move",
                "target": "anno", "end": end, "corner": "", "start": pt,
                "rect0": self._anno_arrow_pt(a), "moved": False,
            }
        else:
            corner = self._corner_anno(anno, pt)
            self._drag = {
                "mode": "resize" if corner else "move",
                "target": "anno", "corner": corner, "start": pt,
                "rect0": self._anno_pt(a), "moved": False,
            }
        self.update()

    def mousePressEvent(self, event):  # noqa: N802 — Qt 命名
        if not self._editable or self._bg is None:
            return super().mousePressEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        pt = self._to_pt(event.pos())
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self._mode != "select":
            # 刚画完的那条是自动选中的：从它里面按下，意思是「调一调」，不是
            # 「在它身上再叠一个」。不这么分，画完发现框歪了、手一拖，就又多
            # 出一条新的，方框、箭头、涂黑全这样。
            anno = self._hit_anno(pt) if self._sel_anno >= 0 else -1
            if anno >= 0 and anno == self._sel_anno:
                self._begin_anno_drag(anno, pt)
                return
            # 绘图工具：按下就开始拉框，松手时由 _finish_create 决定造什么
            self._rubber = [pt[0], pt[1], pt[0], pt[1]]
            self._drag = {"mode": "create", "target": "anno", "start": pt,
                          "moved": False}
            self.update()
            return

        anno = self._hit_anno(pt)
        if anno >= 0:
            # 标注画在图上面，点中的就该是它——否则涂黑条根本拖不动
            self._sel = -1
            self._sel_anno = anno
            self.boxSelected.emit(-1)
            self.annoSelected.emit(anno)
            self._begin_anno_drag(anno, pt)
            return

        index = self._hit(pt)
        self._sel_anno = -1
        self.annoSelected.emit(-1)
        if index < 0:
            self._sel = -1
            self.boxSelected.emit(-1)
            self.update()
            return
        self._sel = index
        self.boxSelected.emit(self._layers[index]["row"])
        corner = self._corner(index, pt)
        self._drag = {
            "mode": "resize" if corner else "move",
            "target": "image", "corner": corner, "start": pt,
            "rect0": list(self._layers[index]["rect"]), "moved": False,
        }
        self.update()

    def mouseMoveEvent(self, event):  # noqa: N802 — Qt 命名
        if not self._editable or self._bg is None:
            return super().mouseMoveEvent(event)
        pt = self._to_pt(event.pos())
        if self._drag is None:
            if self._mode != "select":
                self.setCursor(Qt.CursorShape.CrossCursor)
                return
            anno = self._hit_anno(pt)
            if anno >= 0:
                if self._annos[anno].get("kind") == "arrow":
                    self.setCursor(Qt.CursorShape.SizeFDiagCursor
                                   if self._arrow_handle(anno, pt) is not None
                                   else Qt.CursorShape.SizeAllCursor)
                    return
                corner = self._corner_anno(anno, pt)
                if corner:
                    self.setCursor(Qt.CursorShape.SizeFDiagCursor
                                   if corner in ("nw", "se")
                                   else Qt.CursorShape.SizeBDiagCursor)
                else:
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                return
            index = self._hit(pt)
            if index < 0:
                self.unsetCursor()
            elif self._corner(index, pt):
                corner = self._corner(index, pt)
                self.setCursor(Qt.CursorShape.SizeFDiagCursor
                               if corner in ("nw", "se") else Qt.CursorShape.SizeBDiagCursor)
            else:
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            return

        if self._drag.get("mode") == "create":
            self._rubber = [self._drag["start"][0], self._drag["start"][1],
                            pt[0], pt[1]]
            self.update()
            return

        if self._drag.get("target") == "anno":
            i = self.selected_anno()
            if i < 0:
                return
            rect = self._drag_anno_rect(pt)
            if rect is None:
                return
            self._annos[i] = annotate.make(
                self._annos[i].get("kind", "box"), self._annos[i].get("page", 0),
                self._frac_of(rect), **{k: v for k, v in self._annos[i].items()
                                        if k not in ("kind", "page", "x", "y",
                                                     "w", "h")})
            self._drag["moved"] = True
            self.update()
            return

        if self._sel < 0 or self._sel >= len(self._layers):
            return
        rect = self._drag_rect(pt)
        if rect is None:
            return
        self._layers[self._sel]["rect"] = rect
        self._scaled.pop(self._sel, None)
        self._drag["moved"] = True
        self.update()

    def _frac_of(self, rect_pt) -> tuple[float, float, float, float]:
        """pt 矩形 → 页面比例。标注一律按比例存，换纸换边距都不跑位。"""
        if self._page is None:
            return (0.0, 0.0, 0.0, 0.0)
        pw, ph = self._page
        return (rect_pt[0] / pw, rect_pt[1] / ph,
                (rect_pt[2] - rect_pt[0]) / pw, (rect_pt[3] - rect_pt[1]) / ph)

    def _drag_anno_rect(self, pt) -> list[float] | None:
        """标注的拖动 / 缩放。和图片不同，标注不锁比例——方框和涂黑条就是要
        拉成各种扁长形状，锁比例反而没法用。

        箭头是「起点+终点」而不是矩形：move 平移整条线，endpoint 固定一头
        拖另一头（想往哪个方向指就往哪拖，水平垂直斜线都行）。rect0 和返回值
        都是 [起点x, 起点y, 终点x, 终点y]，顺序对 make 也是无所谓的——make
        对箭头不做任何规范化，方向就留在位移的符号里。
        """
        d = self._drag
        if not d or self._page is None:
            return None
        pw, ph = self._page
        x0, y0, x1, y1 = d["rect0"]
        if d["mode"] == "endpoint":
            x = min(max(0.0, pt[0]), pw)
            y = min(max(0.0, pt[1]), ph)
            if d.get("end") == 0:
                return [x, y, x1, y1]
            return [x0, y0, x, y]
        if d["mode"] == "move":
            dx = pt[0] - d["start"][0]
            dy = pt[1] - d["start"][1]
            if (self._annos[self.selected_anno()].get("kind") if
                    0 <= self.selected_anno() < len(self._annos)
                    else "") == "arrow":
                # 两头都得留在纸内，包围盒的左右和起终点谁前谁后不确定
                dx = _clamp_delta(dx, -min(x0, x1), pw - max(x0, x1))
                dy = _clamp_delta(dy, -min(y0, y1), ph - max(y0, y1))
            else:
                dx = _clamp_delta(dx, -x0, pw - x1)
                dy = _clamp_delta(dy, -y0, ph - y1)
            return [x0 + dx, y0 + dy, x1 + dx, y1 + dy]

        c = d["corner"]
        if "w" in c:
            x0 = min(max(0.0, pt[0]), x1 - 6.0)
        if "e" in c:
            x1 = max(min(pw, pt[0]), x0 + 6.0)
        if "n" in c:
            y0 = min(max(0.0, pt[1]), y1 - 6.0)
        if "s" in c:
            y1 = max(min(ph, pt[1]), y0 + 6.0)
        return [x0, y0, x1, y1]

    def _corner_anno(self, index: int, pt) -> str:
        x0, y0, x1, y1 = self._anno_pt(self._annos[index])
        t = self._tol()
        near_l, near_r = abs(pt[0] - x0) <= t, abs(pt[0] - x1) <= t
        near_t, near_b = abs(pt[1] - y0) <= t, abs(pt[1] - y1) <= t
        if near_l and near_t:
            return "nw"
        if near_r and near_t:
            return "ne"
        if near_l and near_b:
            return "sw"
        if near_r and near_b:
            return "se"
        return ""

    def _drag_rect(self, pt) -> list[float] | None:
        d = self._drag
        if not d or self._page is None:
            return None
        lim = self.limits()
        x0, y0, x1, y1 = d["rect0"]
        if d["mode"] == "move":
            dx = pt[0] - d["start"][0]
            dy = pt[1] - d["start"][1]
            dx = _clamp_delta(dx, lim[0] - x0, lim[2] - x1)
            dy = _clamp_delta(dy, lim[1] - y0, lim[3] - y1)
            return [x0 + dx, y0 + dy, x1 + dx, y1 + dy]

        # 缩放：锁住原比例，用对角那一角当锚点；越出版心就整体收回来
        ax = x1 if "w" in d["corner"] else x0
        ay = y1 if "n" in d["corner"] else y0
        sx = 1.0 if "e" in d["corner"] else -1.0
        sy = 1.0 if "s" in d["corner"] else -1.0
        want_w = max(1.0, sx * (pt[0] - ax))
        want_h = max(1.0, sy * (pt[1] - ay))
        scale = max(want_w / (x1 - x0), want_h / (y1 - y0))
        min_pt = 24.0
        scale = max(min_pt / max(1e-6, (x1 - x0)), scale)
        # 以锚点为中心能长多大：不能越出版心
        if sx > 0:
            scale = min(scale, (lim[2] - ax) / (x1 - x0))
        else:
            scale = min(scale, (ax - lim[0]) / (x1 - x0))
        if sy > 0:
            scale = min(scale, (lim[3] - ay) / (y1 - y0))
        else:
            scale = min(scale, (ay - lim[1]) / (y1 - y0))
        w = (x1 - x0) * scale
        h = (y1 - y0) * scale
        nx0, nx1 = (ax, ax + w) if sx > 0 else (ax - w, ax)
        ny0, ny1 = (ay, ay + h) if sy > 0 else (ay - h, ay)
        return [nx0, ny0, nx1, ny1]

    def mouseDoubleClickEvent(self, event):  # noqa: N802 — Qt 命名
        """双击文字标注改内容——写完发现错一个字，不该逼人删了重画。"""
        if not self._editable or self._bg is None or self._mode != "select":
            return super().mouseDoubleClickEvent(event)
        pt = self._to_pt(event.pos())
        i = self._hit_anno(pt)
        if i >= 0 and self._annos[i].get("kind") == "text":
            self.annoEdited.emit(i)
            return
        return super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 — Qt 命名
        if self._drag is None:
            return super().mouseReleaseEvent(event)
        drag = self._drag
        self._drag = None
        if self._page is None:
            return
        if drag.get("mode") == "create":
            self._finish_create(drag["start"])
            return
        if drag.get("target") == "anno":
            i = self.selected_anno()
            if drag.get("moved") and i >= 0:
                self.annoChanged.emit(i, self._anno_frac(i))
            return
        if not drag.get("moved") or self._sel < 0:
            return
        self._emit_change()

    def _finish_create(self, start) -> None:
        """松手：把刚才拖出来的框变成一条标注交给主窗体。

        判定「太小」用的是**纸面上的尺寸**（pt），不是屏幕像素：画布被压到
        三百来像素高时，一条 4mm 的遮盖条只有 5 个屏幕像素，按像素判会把
        用户认真拖出来的细条当成误点。文字标注哪怕只点一下也要建，因为
        接下来会弹输入框，空框反而让人以为没点上。

        还有个更常见的场景：手上还拿着「文字/方框」工具，点了一下**已有的**
        标注——那意思一定是「我要选它」，不是「再画一个」。所以只要没拖动、
        且落点在一条已有标注上，就当选中处理，并把工具退回「选择」：选中之后
        十有八九是要拖它、拖角改大小、按 Delete 删，工具不退回就一样也做不了。
        想在同一处再画一个，拖一下就行（拖动算新框，点一下算选中）。
        """
        rubber = self._rubber
        self._rubber = None
        if rubber is None or self._page is None:
            return
        w_pt = abs(rubber[2] - rubber[0])
        h_pt = abs(rubber[3] - rubber[1])
        if self._mode == "arrow":
            # 箭头按线长判误点：水平/垂直箭头宽或高本来就是 0，拿宽高判会把
            # 认真画出来的水平箭头当成没拖动
            tiny = math.hypot(rubber[2] - rubber[0],
                              rubber[3] - rubber[1]) < 6.0
        else:
            tiny = w_pt < 3.0 or h_pt < 2.0
        if tiny:
            hit = self._hit_anno(start)
            if hit >= 0:
                self._sel = -1
                self._sel_anno = hit
                self.boxSelected.emit(-1)
                self.annoSelected.emit(hit)
                # 选中之后工具退回「选择」。不退回的话，接下来想把它拖开 / 拖角
                # 放大，一按下就又开一个新框——方框、箭头、涂黑、签名图都是这
                # 样，等于选中了也动不了。想再画一个拖一下就行（拖动仍算新框）。
                self.modeRequested.emit("select")
                self.update()
                return
        if tiny and self._mode != "text":
            self.update()
            return
        if self._mode == "text" and tiny:
            # 点一下放文字：给一个默认大小的框，不用先拖准再写字
            rubber = [start[0], start[1], start[0] + 180.0, start[1] + 26.0]
            pw, ph = self._page
            rubber[2] = min(rubber[2], pw - 2)
            rubber[3] = min(rubber[3], ph - 2)
        if self._mode == "arrow":
            # 箭头存「起点 + 有符号位移」：往哪拖就朝哪指，方向全在位移里，
            # 不再像旧版那样靠 flip 猜——那只有两条对角线，水平箭头画不出来
            frac = self._frac_of(rubber)
            self.annoCreated.emit("arrow", {"x": frac[0], "y": frac[1],
                                            "w": frac[2], "h": frac[3]},
                                  False)
            self.update()
            return
        frac = self._frac_of(rubber)
        rect = (min(frac[0], frac[0] + frac[2]), min(frac[1], frac[1] + frac[3]),
                abs(frac[2]), abs(frac[3]))
        self.annoCreated.emit(self._mode, {"x": rect[0], "y": rect[1],
                                           "w": rect[2], "h": rect[3]}, False)
        self.update()

    def _emit_change(self) -> None:
        if not (0 <= self._sel < len(self._layers)) or self._page is None:
            return
        pw, ph = self._page
        x0, y0, x1, y1 = self._layers[self._sel]["rect"]
        self.boxChanged.emit(self._layers[self._sel]["row"], {
            "x": max(0.0, min(1.0, x0 / pw)),
            "y": max(0.0, min(1.0, y0 / ph)),
            "w": max(0.001, min(1.0, (x1 - x0) / pw)),
            "h": max(0.001, min(1.0, (y1 - y0) / ph)),
        })

    def keyPressEvent(self, event):  # noqa: N802 — Qt 命名
        if not self._editable or self._page is None:
            return super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.selected_anno() >= 0:
                self.delete_anno()
                return
            return super().keyPressEvent(event)
        if event.key() == Qt.Key.Key_Escape and self._mode != "select":
            self.set_mode("select")
            self.modeRequested.emit("select")
            return
        step = 10.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
        dx = {Qt.Key.Key_Left: -step, Qt.Key.Key_Right: step}.get(event.key(), 0.0)
        dy = {Qt.Key.Key_Up: -step, Qt.Key.Key_Down: step}.get(event.key(), 0.0)
        if not dx and not dy:
            return super().keyPressEvent(event)
        if self.selected_anno() >= 0:
            # 方向键微调标注：按 pt 挪，换算回比例存
            i = self.selected_anno()
            pw, ph = self._page
            if self._annos[i].get("kind") == "arrow":
                # 箭头平移只能挪起点（位移是方向，动了方向就变了）；也不能
                # 走下面的规范化矩形路径——那一趟会把方向信息洗掉
                a = dict(self._annos[i])
                p1, p2 = annotate.arrow_ends(a, pw, ph)
                ddx = _clamp_delta(dx, -min(p1[0], p2[0]), pw - max(p1[0], p2[0]))
                ddy = _clamp_delta(dy, -min(p1[1], p2[1]), ph - max(p1[1], p2[1]))
                a["x"] = max(0.0, min(1.0, a["x"] + ddx / pw))
                a["y"] = max(0.0, min(1.0, a["y"] + ddy / ph))
                self._annos[i] = a
                self.update()
                self.annoChanged.emit(i, self._anno_frac(i))
                return
            x0, y0, x1, y1 = self._anno_pt(self._annos[i])
            dx = _clamp_delta(dx, -x0, pw - x1)
            dy = _clamp_delta(dy, -y0, ph - y1)
            rect = self._frac_of([x0 + dx, y0 + dy, x1 + dx, y1 + dy])
            self._annos[i] = annotate.make(
                self._annos[i].get("kind", "box"), self._annos[i].get("page", 0),
                rect, **{k: v for k, v in self._annos[i].items()
                         if k not in ("kind", "page", "x", "y", "w", "h")})
            self.update()
            self.annoChanged.emit(i, self._anno_frac(i))
            return
        if self._sel < 0:
            return super().keyPressEvent(event)
        lim = self.limits()
        x0, y0, x1, y1 = self._layers[self._sel]["rect"]
        dx = _clamp_delta(dx, lim[0] - x0, lim[2] - x1)
        dy = _clamp_delta(dy, lim[1] - y0, lim[3] - y1)
        self._layers[self._sel]["rect"] = [x0 + dx, y0 + dy, x1 + dx, y1 + dy]
        self._scaled.pop(self._sel, None)
        self.update()
        self._emit_change()


class SheetPreview(QWidget):
    """成品预览：把待生成的那份 PDF 一页一页画在主窗体右侧。

    主窗体要回答的问题只有三个——放了哪些图、怎么排版、出来长什么样。
    前两个是缩略列表和页面排版，第三个就是这里，翻页和页码都收在这一块，
    不用为了看成品另开窗口。

    渲染走 converter.sheet_layers，与出片同一条代码路径（同样的纸张 / 拼版 /
    边距 / 页码 / 单张覆盖），所以不会「预览挺好、出来不对」。

    这个组件有两种形态，同一个类，只差一个 compact 开关：

    - **compact（主窗体右栏）**：只读。一页缩在三百来像素高的方框里，看清版式
      够用，动手改就太挤了，所以这里只留一个「页面编辑…」按钮往外开窗。
    - **非 compact（页面编辑窗口）**：独立大窗口里的那一个。整页铺满一屏，
      图可以直接拖位置、拖角缩放：拖动只改画布里的矩形（跟手），松手才把位置
      交给主窗体存下来并重排一次（准）。工具条上还能往页面上加文字、方框、
      箭头、涂黑和签名图——都是矢量，改一条不用重排整页，立刻就能看到结果。
    """

    placeChanged = Signal(int, object)   # items 下标, {"x","y","w","h"} 或 None
    resetPage = Signal(object)          # 要复位的手动位置（items 下标列表）
    editModeChanged = Signal(bool)
    editRequested = Signal()            # 点了「页面编辑…」，请主窗体开窗
    annoAdded = Signal(dict)            # 新建的一条标注（已带 page）
    annoUpdated = Signal(int, dict)     # 全局下标, 改动后的整条标注
    annoRemoved = Signal(int)           # 全局下标
    # 「我要改了」——在真正动数据之前发一次，主窗体据此压一份撤销快照。
    # 带一个 tag：连续调字号那种一路 valueChanged 的操作靠它合并成一步。
    willChange = Signal(str)
    undoRequested = Signal()
    redoRequested = Signal()

    CACHE_MAX = 12

    def __init__(self, parent=None, compact: bool = True):
        super().__init__(parent)
        self._compact = bool(compact)
        self._editing = False
        # 主窗体那边按钮要挤在一行里，编辑窗口里则要顺手点得到，两种密度
        pad = "2px 6px" if self._compact else "6px 14px"
        self._btn_css = f"QPushButton{{min-height:0;padding:{pad};}}"
        # 默认的 Preferred 策略下这一块只长到 sizeHint，右边会留一条空白
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._items: list = []
        self._opt: ConvertOptions = ConvertOptions()
        self._pages = 0
        self._page = 0
        self._gen = 0
        self._cache: dict[int, dict] = {}
        self._pending: set[int] = set()
        self._closed = False
        self._sel_row = -1
        self._annos_all: list[dict] = []      # 主窗体那份的副本（全部页）
        self._page_annos: list[dict] = []     # 当前页的标注
        self._page_idx: list[int] = []        # 当前页每条对应的全局下标
        self._tool = "select"
        self._syncing_style = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(4 if self._compact else 10)
        title = QLabel("成品预览" if self._compact else "页面编辑")
        title.setStyleSheet("font-weight:600;")
        head.addWidget(title)

        self.btn_edit = None
        if self._compact:
            # 主窗体这块只读：一页缩在这么小的方框里画不准，动手请开独立窗口
            self.btn_edit = QPushButton("页面编辑…")
            self.btn_edit.setStyleSheet(self._btn_css)
            self.btn_edit.setToolTip("打开一个独立大窗口：拖动图片换位置、拖四角改大小，"
                                     "也能往页面上加文字、方框、箭头、涂黑和签名图")
            self.btn_edit.clicked.connect(self.editRequested.emit)
            head.addWidget(self.btn_edit)
        head.addStretch(1)

        big = not self._compact
        self.btn_prev = QPushButton("‹ 上一页" if big else "‹")
        if big:
            self.btn_prev.setStyleSheet(self._btn_css)
        else:
            # 全局样式表给按钮压了 min-height:34px，这两个小箭头得单独摘掉，
            # 否则会被撑到 46px 高、把页码挤出右边界
            self.btn_prev.setStyleSheet("QPushButton{min-height:0;padding:0;}")
            self.btn_prev.setFixedSize(26, 22)
        self.btn_prev.setToolTip("上一页（PageUp）")
        self.btn_prev.clicked.connect(lambda: self.goto(self._page - 1))
        head.addWidget(self.btn_prev)
        self.page_label = QLabel("—")
        self.page_label.setStyleSheet("color:#555;")
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if big:
            self.page_label.setStyleSheet("color:#222;font-size:13px;font-weight:600;")
            self.page_label.setMinimumWidth(110)
            self.page_label.setFixedHeight(26)
        else:
            self.page_label.setFixedHeight(22)
            self.page_label.setMinimumWidth(76)
        head.addWidget(self.page_label)
        self.btn_next = QPushButton("下一页 ›" if big else "›")
        if big:
            self.btn_next.setStyleSheet(self._btn_css)
        else:
            self.btn_next.setStyleSheet("QPushButton{min-height:0;padding:0;}")
            self.btn_next.setFixedSize(26, 22)
        self.btn_next.setToolTip("下一页（PageDown）")
        self.btn_next.clicked.connect(lambda: self.goto(self._page + 1))
        head.addWidget(self.btn_next)
        root.addLayout(head)

        # 编辑时才出现的工具条：平时不占地方，主界面还是一屏看得到成品
        self.tools = QWidget()
        tl = QVBoxLayout(self.tools)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(3)

        row1 = QHBoxLayout()
        row1.setContentsMargins(0, 0, 0, 0)
        row1.setSpacing(3)
        self.tool_buttons: dict[str, QPushButton] = {}
        for key, label, tip in (
            ("select", "选择", "拖动图或标注、拖四角改大小（Esc 也能回到这里）"),
            ("text", "文字", "在纸上拖一个框，松手后填字；点一下也能放一行默认大小的字"),
            ("box", "方框", "拖出矩形框，用来圈重点"),
            ("arrow", "箭头", "从起点拖到终点，方向长短随意；选中后拖两端调整，粗细在下面选"),
            ("redact", "涂黑", "拖出矩形盖住隐私信息，出片时是实心色块"),
            ("stamp", "签名图", "选一张 PNG/JPG 签名或印章放到页面上，透明底会被保留"),
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setStyleSheet(self._btn_css)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _c=False, k=key: self.set_tool(k))
            self.tool_buttons[key] = btn
            row1.addWidget(btn)
        self.btn_del_anno = QPushButton("删除")
        self.btn_del_anno.setStyleSheet(self._btn_css)
        self.btn_del_anno.setToolTip("删掉选中的标注（也可以按 Delete 键）")
        self.btn_del_anno.clicked.connect(self._delete_anno)
        row1.addWidget(self.btn_del_anno)
        self.btn_edit_text = QPushButton("改字")
        self.btn_edit_text.setStyleSheet(self._btn_css)
        self.btn_edit_text.setToolTip("改选中那条文字的内容（也可以直接双击它）")
        self.btn_edit_text.setEnabled(False)
        self.btn_edit_text.clicked.connect(self._edit_selected_text)
        row1.addWidget(self.btn_edit_text)
        tl.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(4)
        # 撤销排在最左：画歪了、删错了，第一反应是往这儿点
        self.btn_undo = QPushButton("撤销")
        self.btn_undo.setStyleSheet(self._btn_css)
        self.btn_undo.setToolTip("撤销上一步（Ctrl+Z）")
        self.btn_undo.setEnabled(False)
        self.btn_undo.clicked.connect(self.undoRequested.emit)
        row2.addWidget(self.btn_undo)
        self.btn_redo = QPushButton("重做")
        self.btn_redo.setStyleSheet(self._btn_css)
        self.btn_redo.setToolTip("重做刚撤销的那一步（Ctrl+Y）")
        self.btn_redo.setEnabled(False)
        self.btn_redo.clicked.connect(self.redoRequested.emit)
        row2.addWidget(self.btn_redo)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        row2.addWidget(sep)
        row2.addWidget(QLabel("字号"))
        self.size_spin = QSpinBox()
        self.size_spin.setRange(6, 72)
        self.size_spin.setSuffix(" pt")
        self.size_spin.setMinimumWidth(88)
        self.size_spin.setStyleSheet("QSpinBox{min-height:22px;}")
        self.size_spin.setToolTip("文字标注的字号；选中某一处时改这里会改那一处")
        self.size_spin.setValue(14)
        self.size_spin.valueChanged.connect(self._on_style_changed)
        row2.addWidget(self.size_spin)
        self.color_box = QComboBox()
        # 宽度跟着内容走：钉死一个固定值的话，字体稍宽一点文字就被切一半
        self.color_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.color_box.setStyleSheet("QComboBox{min-height:22px;}")
        self.color_box.setToolTip("标注颜色")
        for name, val in annotate.COLORS:
            self.color_box.addItem(self._color_icon(val), name, val)
        # 默认红：签批、圈重点几乎都用红笔，黑色容易和正文糊在一起
        self.color_box.setCurrentIndex(max(0, self.color_box.findData("#d0021b")))
        # AdjustToContents 只抬 sizeHint：布局空间一紧，控件还是会被压到
        # minimumSizeHint 以下，「红」「蓝」就切没了。填充完选项把内容宽度
        # 钉成硬下限，布局再挤也压不下去（92px ≈ 色块 + 两个字 + 箭头）。
        self.color_box.setMinimumWidth(max(
            92, self.color_box.sizeHint().width()))
        self.color_box.currentIndexChanged.connect(self._on_style_changed)
        row2.addWidget(self.color_box)
        row2.addWidget(QLabel("粗细"))
        self.width_box = QComboBox()
        self.width_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.width_box.setStyleSheet("QComboBox{min-height:22px;}")
        self.width_box.setToolTip("箭头 / 方框的线宽；选中的那一条会跟着改")
        for name, val in (("细", 1.6), ("中", 2.6), ("粗", 4.5)):
            self.width_box.addItem(name, val)
        self.width_box.setCurrentIndex(1)
        # 同 color_box：钉硬下限，64px 保证「粗」字完整
        self.width_box.setMinimumWidth(max(
            64, self.width_box.sizeHint().width()))
        self.width_box.currentIndexChanged.connect(self._on_style_changed)
        row2.addWidget(self.width_box)
        self.btn_center = QPushButton("居中")
        self.btn_center.setStyleSheet(self._btn_css)
        self.btn_center.setToolTip("把选中的图摆到页面正中，大小不变")
        self.btn_center.clicked.connect(lambda: self._arrange("center"))
        row2.addWidget(self.btn_center)
        self.btn_fill = QPushButton("铺满")
        self.btn_fill.setStyleSheet(self._btn_css)
        self.btn_fill.setToolTip("把选中的图放大到页面版心，保持比例")
        self.btn_fill.clicked.connect(lambda: self._arrange("fill"))
        row2.addWidget(self.btn_fill)
        self.btn_reset = QPushButton("复位本页")
        self.btn_reset.setStyleSheet(self._btn_css)
        self.btn_reset.setToolTip("这一页手动摆过的图全部退回按拼版自动落格")
        self.btn_reset.clicked.connect(self._reset_here)
        row2.addWidget(self.btn_reset)
        row2.addStretch(1)
        tl.addLayout(row2)

        self.sel_label = QLabel("")
        self.sel_label.setStyleSheet("color:#555;" if self._compact
                                     else "color:#333;font-size:12px;")
        self.sel_label.setWordWrap(True)
        tl.addWidget(self.sel_label)
        self.tools.setVisible(False)
        root.addWidget(self.tools)

        self.canvas = SheetView()
        self.canvas.clear("还没有图片")
        self.canvas.boxChanged.connect(self._on_box_changed)
        self.canvas.boxSelected.connect(self._on_box_selected)
        self.canvas.annoSelected.connect(self._on_anno_selected)
        self.canvas.annoChanged.connect(self._on_anno_changed)
        self.canvas.annoCreated.connect(self._on_anno_created)
        self.canvas.annoDeleted.connect(self._on_anno_deleted)
        self.canvas.annoEdited.connect(self._on_anno_double)
        self.canvas.modeRequested.connect(self.set_tool)
        root.addWidget(self.canvas, 1)

        # 独立窗口里的这一个天生就是编辑态：没有「只读」这一说
        if not self._compact:
            self.set_edit_mode(True)
        self._sync_preview_style()

        self._renderer = SheetRenderer(self)
        self._renderer.done.connect(self._on_page_ready)
        self._renderer.start()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._render_now)

    @staticmethod
    def _color_icon(val: str) -> QIcon:
        pix = QPixmap(14, 14)
        pix.fill(QColor(val))
        return QIcon(pix)

    # ------------------------------------------------------------ 外部接口
    def set_document(self, items, opt: ConvertOptions, *, keep_view: bool = False) -> None:
        """换图或换参数：页数重算、旧结果作废，当前页延时重排。

        keep_view=True 用在「刚手动挪了一下」：位置本来就是画布里那个，
        清空画布只会闪一下白，没必要。
        """
        if self._closed:
            return
        self._items, self._opt = list(items), opt.normalized()
        self._gen += 1
        self._cache.clear()
        self._pending.clear()
        self._renderer.clear()
        self._pages = sheet_count(self._items, self._opt) if self._items else 0
        self._page = max(0, min(self._page, max(0, self._pages - 1)))
        if self._pages and self._sel_row >= len(self._items):
            self._sel_row = -1
        self._sync_labels()
        # 自适应纸张也允许编辑：那里只是拖不了图，标注照样能加
        if self.btn_edit is not None:
            self.btn_edit.setEnabled(bool(self._pages))
        if not self._pages:
            self.canvas.clear("还没有图片")
            return
        self.canvas.set_images_locked(self._opt.paper == PAPER_AUTO)
        if not keep_view and self.canvas._bg is None:
            self.canvas.clear("正在排版…")
        self._timer.start(160)   # 拖着调清晰度时别每一格都重排一页

    def goto(self, page_no: int) -> None:
        if self._closed or not self._pages:
            return
        target = max(0, min(int(page_no), self._pages - 1))
        if target != self._page:
            self._sel_row = -1
        self._page = target
        self._sync_page_annos()
        self._sync_labels()
        cached = self._cache.get(self._page)
        if cached is not None:
            self._show(cached)
            self._prefetch()
            return
        if not self._editing:
            self.canvas.clear("正在排版…")
        self._request(self._page, urgent=True)

    def page_of_row(self, row: int) -> int:
        per = max(1, int(self._opt.normalized().per_page))
        return max(0, int(row)) // per

    def set_edit_mode(self, on: bool) -> None:
        """切到编辑态。compact 形态（主窗体右栏）永远是只读的，这里直接拒绝。

        独立窗口里一进来就是编辑态，工具条常驻；主窗体那边只留「页面编辑…」
        按钮，点了往外开窗，不再就地把小画布变成编辑器。
        """
        if self._compact:
            return
        on = bool(on)
        if on == self._editing:
            return
        self._editing = on
        self.tools.setVisible(on)
        self.canvas.set_editable(on)
        if on:
            self.set_tool(self._tool)
        self._sync_sel()
        self.editModeChanged.emit(on)

    @property
    def editing(self) -> bool:
        return self._editing

    def set_annotations(self, annos) -> None:
        """接住主窗体那份标注（全部页）。它按页筛出本页的给画布。"""
        self._annos_all = [dict(a) for a in (annos or [])]
        self._sync_page_annos()
        self.canvas.set_annotations(self._page_annos)

    def restore_annotations(self, annos) -> None:
        """撤销 / 重做后整份换掉：连带把选中清掉。

        只调 set_annotations 的话，`_sel_anno` 还是旧下标——长度没变、内容变了
        的时候它不会越界，于是「握着」的是另一条标注，改字号会改错人。
        """
        self.set_annotations(annos)
        self.canvas.clear_anno_selection()
        self._sync_tool_state()

    def set_undo_state(self, can_undo: bool, can_redo: bool) -> None:
        """主窗体是撤销栈的主人，按钮能不能点由它说了算。"""
        self.btn_undo.setEnabled(bool(can_undo))
        self.btn_redo.setEnabled(bool(can_redo))

    def set_tool(self, key: str) -> None:
        """切工具。按钮互斥，画布按这个决定「拖出来的是框还是要移动图」。"""
        self._tool = key
        for k, btn in self.tool_buttons.items():
            btn.setChecked(k == key)
        self.canvas.set_mode(key)
        self._sync_tool_state()

    @property
    def tool(self) -> str:
        return self._tool

    def _sync_page_annos(self) -> None:
        """按当前页号筛出本页标注，并记住每条对应的全局下标。

        画布里的拖动是按「本页第几条」回调的，主窗体存的是全局列表；
        中间这层映射不能省，否则翻页之后删掉的会是另一页的标注。
        """
        self._page_annos = []
        self._page_idx = []
        for i, a in enumerate(self._annos_all):
            if int(a.get("page", 0)) == self._page:
                self._page_annos.append(a)
                self._page_idx.append(i)

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        self._renderer.stop()
        self._renderer.wait(8000)

    # ------------------------------------------------------------ 内部
    def _render_now(self) -> None:
        if self._closed or not self._pages:
            return
        cached = self._cache.get(self._page)
        if cached is not None:
            self._show(cached)
            self._prefetch()
            return
        if not self._editing:
            self.canvas.clear("正在排版…")
        self._request(self._page, urgent=True)

    def _request(self, page_no: int, *, urgent: bool = False) -> None:
        if page_no < 0 or page_no >= self._pages or page_no in self._cache:
            return
        self._pending.add(page_no)
        self._renderer.request(self._gen, page_no, self._items, self._opt,
                               urgent=urgent)

    def _prefetch(self) -> None:
        """当前页出来了顺手排上前后页：来回翻不用每次都等半秒。"""
        for near in (self._page + 1, self._page - 1):
            self._request(near)

    def _on_page_ready(self, gen: int, page_no: int, data, err: str) -> None:
        if self._closed or gen != self._gen:
            return   # 参数或清单变了，这份结果已经过时
        self._pending.discard(page_no)
        if err or not data or data.get("bg") is None:
            if page_no == self._page:
                self.canvas.clear("这一页排不出来")
            return
        self._cache[page_no] = data
        if len(self._cache) > self.CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        if page_no == self._page:
            self._show(data)
            self._prefetch()

    def _show(self, data) -> None:
        self._sync_page_annos()
        self.canvas.set_page(data["bg"], data["page_pt"], data["layers"],
                             self._page_annos)
        pw, ph = data["page_pt"]
        # 可摆放范围就是版心：图不掉进边距，更不会盖住页码那条带
        self.canvas.set_limits(content_rect(pw, ph, self._opt.margin_mm,
                                            text_reserve(self._opt)))
        self._sync_sel()

    def _sync_labels(self) -> None:
        self.page_label.setText(f"第 {self._page + 1} / {self._pages} 页"
                                if self._pages else "—")
        self.btn_prev.setEnabled(self._pages > 0 and self._page > 0)
        self.btn_next.setEnabled(self._pages > 0 and self._page < self._pages - 1)

    # ------------------------------------------------------------ 页面编辑
    def _sync_tool_state(self) -> None:
        """工具决定提示语，也决定哪些控件有意义。

        画图时「居中 / 铺满」是给图用的，得禁掉；选中一条标注时「字号 / 颜色」
        才有意义，其余时候它们是「下一条新标注的默认值」。
        """
        drawing = self._tool != "select"
        anno = self.canvas.selected_anno() >= 0
        obj = (self.canvas.selected_anno_obj() or {}) if anno else {}
        self.btn_center.setEnabled(not drawing and self.canvas.selected_row() >= 0)
        self.btn_fill.setEnabled(not drawing and self.canvas.selected_row() >= 0)
        self.btn_del_anno.setEnabled(anno)
        # 「改字」只对文字标注有意义，别的类型点了也没事干，索性禁掉
        self.btn_edit_text.setEnabled(obj.get("kind") == "text")
        self.size_spin.setEnabled(self._tool == "text" or anno)
        self.color_box.setEnabled(self._tool in ("text", "box", "arrow", "redact")
                                  or anno)
        self.width_box.setEnabled(self._tool in ("box", "arrow")
                                  or obj.get("kind") in ("box", "arrow"))
        if anno:
            a = obj
            if a.get("kind") == "arrow":
                hint = "　拖两端改方向长短 · 拖中间移动 · Delete 删除"
            else:
                hint = "　拖动移动 · 拖四角缩放 · Delete 删除"
            self.sel_label.setText(
                f"已选：{annotate.KINDS.get(a.get('kind'), '标注')}"
                + (f"「{a.get('text', '')[:16]}」" if a.get("text") else "")
                + hint)
            self._sync_style_widgets(a)
        elif drawing:
            self.sel_label.setText("在页面上拖一下，松手就画出来（Esc 取消）")
        elif self.canvas.selected_row() >= 0:
            row = self.canvas.selected_row()
            page = self.page_of_row(row) + 1
            self.sel_label.setText(f"已选第 {row + 1} 张（第 {page} 页）")
        else:
            self.sel_label.setText("点图或标注选中，拖动换位置" if self._editing else "")

    def _sync_style_widgets(self, a: dict) -> None:
        """把选中标注的字号 / 颜色 / 粗细回填到工具条，改之前先看到现状。"""
        self._syncing_style = True
        try:
            if a.get("size"):
                self.size_spin.setValue(int(round(float(a["size"]))))
            idx = self.color_box.findData(a.get("color"))
            if idx >= 0:
                self.color_box.setCurrentIndex(idx)
            if a.get("kind") in ("box", "arrow"):
                # 粗细就三档，标注上的值不一定正好等于某一档，取最近的
                want = float(a.get("width") or 2.6)
                best, dist = 0, 1e9
                for j in range(self.width_box.count()):
                    d = abs(float(self.width_box.itemData(j)) - want)
                    if d < dist:
                        best, dist = j, d
                self.width_box.setCurrentIndex(best)
        finally:
            self._syncing_style = False

    def _sync_preview_style(self) -> None:
        """把当前笔色 / 粗细交给画布：拖拽中的箭头要按这个画预览。"""
        self.canvas.set_preview_style(self.color_box.currentData() or "#d0021b",
                                      float(self.width_box.currentData() or 2.6))

    def _store_anno(self, local: int, anno: dict) -> None:
        """改动一条标注：本页副本、全量副本、主窗体，三处一起写。

        只写本页副本是个很隐蔽的坑——翻一页再翻回来（或改个参数重排）本页
        副本是从全量那份重建的，于是标注「自己跳回旧位置」，怎么拖都留不住。
        """
        if not (0 <= local < len(self._page_annos)):
            return
        self._page_annos[local] = anno
        gidx = self._page_idx[local] if local < len(self._page_idx) else -1
        if 0 <= gidx < len(self._annos_all):
            self._annos_all[gidx] = anno
        if gidx >= 0:
            self.annoUpdated.emit(gidx, dict(anno))

    def _on_style_changed(self, *_args) -> None:
        if getattr(self, "_syncing_style", False):
            return
        self._sync_preview_style()
        i = self.canvas.selected_anno()
        if i < 0:
            return     # 没选中时只是改「下一条」的默认值，不用落数据
        a = dict(self._page_annos[i])
        old = (self._page_annos[i].get("size"), self._page_annos[i].get("color"),
               self._page_annos[i].get("width"))
        a["size"] = float(self.size_spin.value())
        a["color"] = self.color_box.currentData()
        if a.get("kind") in ("box", "arrow"):
            a["width"] = float(self.width_box.currentData())
        if (a["size"], a["color"], a.get("width")) == old:
            return     # 值没变（选中的是别的类型时同步控件也会走到这儿）
        # tag 带上页号和下标：同一路狂点字号合成一步，换一条就另算一步
        self.willChange.emit(f"style:{self._page}:{i}")
        self._store_anno(i, a)
        self.canvas.set_annotations(self._page_annos)

    def _sync_sel(self) -> None:
        row = self.canvas.selected_row()
        self._sel_row = row
        self._sync_tool_state()

    def _on_box_selected(self, row: int) -> None:
        self._sel_row = row
        self._sync_tool_state()

    def _on_box_changed(self, row: int, rect: dict) -> None:
        self.willChange.emit(f"place:{int(row)}")
        self.placeChanged.emit(int(row), dict(rect))

    # ------------------------------------------------------------ 标注
    def _on_anno_selected(self, index: int) -> None:
        self._sync_tool_state()

    def _on_anno_changed(self, index: int, frac: dict) -> None:
        """画布拖完一条标注：同步到两份副本，再交给主窗体存。"""
        if not (0 <= index < len(self._page_annos)):
            return
        self.willChange.emit(f"move:{self._page}:{index}")
        a = dict(self._page_annos[index])
        a.update({k: float(v) for k, v in frac.items()})
        self._store_anno(index, a)

    def _on_anno_created(self, kind: str, frac: dict, flip: bool) -> None:
        """画布拖出一个框：补齐样式，文字/签名图要先问一句。"""
        text = ""
        src = ""
        if kind == "text":
            text, ok = QInputDialog.getMultiLineText(
                self, "输入文字", "要写在页面上的字（可换行）：", "")
            if not ok or not text.strip():
                return      # 取消就当没画过，不留一个空框
        elif kind == "stamp":
            src, _ = QFileDialog.getOpenFileName(
                self, "选择签名或印章图片", "",
                "图片 (*.png *.jpg *.jpeg *.webp *.bmp)")
            if not src:
                return
        a = annotate.make(
            kind, self._page, (frac["x"], frac["y"], frac["w"], frac["h"]),
            size=float(self.size_spin.value()),
            # 涂黑默认纯黑：叫「涂黑」结果出来是红条，谁都以为出错了。
            # 真要白色遮盖，选中它再把颜色改白就行。
            color="#000000" if kind == "redact"
            else (self.color_box.currentData() or "#d0021b"),
            width=float(self.width_box.currentData() or 2.6),
            text=text.strip()[:2000], flip=bool(flip), src=src,
        )
        a = self._fit_anno_text(a)
        # 到这儿对话框都过了、确实要加这一条，才值得压一份撤销快照；
        # 用户点了取消不该在历史里留一步空操作。
        self.willChange.emit("")
        self._annos_all.append(a)
        self._sync_page_annos()
        self.canvas.set_annotations(self._page_annos)
        # 新加的那条自动选中，方便马上调字号或接着拖
        self.canvas._sel_anno = len(self._page_annos) - 1
        self.canvas.annoSelected.emit(self.canvas._sel_anno)
        self.canvas.update()
        self.annoAdded.emit(dict(a))
        self._sync_tool_state()

    def _on_anno_double(self, index: int) -> None:
        """双击文字标注改内容。改错一个字不用删了重画。"""
        if not (0 <= index < len(self._page_annos)):
            return
        a = self._page_annos[index]
        if a.get("kind") != "text":
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "修改文字", "要写在页面上的字（可换行）：", a.get("text", ""))
        if not ok:
            return
        a = dict(a)
        a["text"] = text.strip()[:2000]
        if a["text"] == self._page_annos[index].get("text"):
            return     # 打开的原文没改，别在历史里留一步空撤销
        a = self._fit_anno_text(a)
        self.willChange.emit(f"text:{self._page}:{index}")
        self._store_anno(index, a)
        self.canvas.set_annotations(self._page_annos)

    def _edit_selected_text(self) -> None:
        """工具条上的「改字」：选的正好是文字就改它，省得记双击。"""
        i = self.canvas.selected_anno()
        if i >= 0:
            self._on_anno_double(i)

    def _fit_anno_text(self, a: dict) -> dict:
        """文字框按内容长大：写三行就得看得见三行。

        量宽用的是出片那套字宽（PDF 字体），所以「窗口里装得下」约等于
        「PDF 里也装得下」；宽高只增不减，用户拖出来的位置和大致尺寸还认得出。
        """
        size = self.canvas.page_size()
        if not size or a.get("kind") != "text":
            return dict(a)
        return annotate.fit_text(a, size[0], size[1])

    def _on_anno_deleted(self, index: int) -> None:
        """画布删掉一条：本页副本和全量副本都得跟着少一条。

        少删全量那份会埋一个很阴的坑：三份列表（主窗体、全量、本页）从此错
        位一位，之后点中的和删掉的可能不是同一条，换个页更乱。所以删完立刻
        重建映射再灌回画布，三份永远一致。
        """
        if not (0 <= index < len(self._page_idx)):
            return
        self.willChange.emit("")      # 先记下删之前的样子，撤销才能把它放回来
        gidx = self._page_idx[index]
        self._page_annos.pop(index)
        self._page_idx.pop(index)
        if 0 <= gidx < len(self._annos_all):
            self._annos_all.pop(gidx)
        self._sync_page_annos()
        self.canvas.set_annotations(self._page_annos)
        self.annoRemoved.emit(gidx)
        self._sync_tool_state()

    def _delete_anno(self) -> None:
        self.canvas.delete_anno()

    def _current_rows(self) -> list[int]:
        per = max(1, int(self._opt.normalized().per_page))
        start = self._page * per
        return list(range(start, min(len(self._items), start + per)))

    def _reset_here(self) -> None:
        """复位本页：图片位置退回自动排布，本页的标注也全部清掉。

        这一下丢的东西不少——手动摆的位置、加的文字 / 方框 / 箭头 / 涂黑 /
        签名图都会没，所以先弹确认问一句再动手；真做了会给撤销栈压一份
        快照（整次复位算一步），后悔按 Ctrl+Z 能整个找回来。
        """
        rows = self._current_rows()
        here = [i for i, a in enumerate(self._annos_all)
                if int(a.get("page", 0)) == self._page]
        if not rows and not here:
            return
        drop = []
        if rows:
            drop.append(f"{len(rows)} 张图的手动位置（回到自动排版）")
        if here:
            counts: dict[str, int] = {}
            for a in (self._annos_all[i] for i in here):
                k = str(a.get("kind", "box"))
                counts[k] = counts.get(k, 0) + 1
            drop.append("本页的 " + "、".join(
                f"{annotate.KINDS.get(k, k)}×{n}" for k, n in counts.items()))
        ans = QMessageBox.question(
            self, "复位本页",
            "复位会把这一页的这些编辑全部撤销：\n\n"
            + "\n".join(f"　· {d}" for d in drop)
            + "\n\n确定复位吗？（复位后按 Ctrl+Z 可以整个找回来）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.willChange.emit("")
        if rows:
            self.resetPage.emit(rows)
        # 标注从后往前删：前面下标不会因为 pop 而移动
        for gidx in reversed(here):
            self._annos_all.pop(gidx)
            self.annoRemoved.emit(gidx)
        self._sync_page_annos()
        self.canvas.set_annotations(self._page_annos)
        self._sync_tool_state()

    def _arrange(self, mode: str) -> None:
        """居中 / 铺满：算好新矩形交给画布，再发一次位置变更。"""
        rect = self.canvas.selected_rect()
        if rect is None or self.canvas.page_size() is None:
            return
        pw, ph = self.canvas.page_size()
        x0, y0, x1, y1 = rect
        w, h = x1 - x0, y1 - y0
        if mode == "fill":
            lim = self.canvas.limits()
            scale = min((lim[2] - lim[0]) / max(1e-6, w), (lim[3] - lim[1]) / max(1e-6, h))
            w, h = w * scale, h * scale
        cx, cy = pw / 2.0, ph / 2.0
        self.canvas.set_layer_rect([cx - w / 2.0, cy - h / 2.0,
                                    cx + w / 2.0, cy + h / 2.0])
        row = self.canvas.selected_row()
        if row >= 0:
            self._on_box_changed(row, self.canvas.selected_frac())


class PageEditor(QDialog):
    """页面编辑：一个独立的大窗口。

    为什么不在主窗体右栏就地编：那块预览只有三百来像素高（要让参数区露出
    来），一页 A4 缩进去连正文都糊成一团，写字只能写个大概、拖角只能拖个
    大概。于是分成两件事——右栏只负责「看一眼对不对」，真要动手就开这扇窗。

    画布、渲染、标注、拖动/缩放全部复用 SheetPreview（非 compact 形态），
    与出片走同一条代码路径，所以这里看到的排版就是印出来的排版。

    窗口大小在同一次运行里记住：拖大一次，下次打开还是那么大。
    """

    _last_size: tuple[int, int] | None = None

    def __init__(self, parent, items, opt: ConvertOptions, annotations):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} · 页面编辑")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setSizeGripEnabled(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(6)

        self.sheet = SheetPreview(compact=False)
        self.sheet.setMinimumHeight(360)
        root.addWidget(self.sheet, 1)

        bar = QHBoxLayout()
        bar.setSpacing(10)
        hint = QLabel("拖动图片或标注换位置 · 拖四角缩放 · 方向键微移 · Delete 删除 "
                      "· Ctrl+Z 撤销 / Ctrl+Y 重做 · PageUp / PageDown 换页")
        hint.setStyleSheet("color:#555;")
        bar.addWidget(hint, 1)
        self.btn_done = QPushButton("完成")
        self.btn_done.setStyleSheet(
            "QPushButton{min-height:30px;padding:0 22px;font-weight:600;}")
        self.btn_done.setToolTip("关上这个窗口，回到主界面（改动已经记下了）")
        self.btn_done.setDefault(True)
        self.btn_done.clicked.connect(self.accept)
        bar.addWidget(self.btn_done)
        root.addLayout(bar)

        self.sheet.set_document(items, opt)
        self.sheet.set_annotations(annotations)

        # 标注改动在这里就知道，关窗时主窗体要不要补一次刷新全看它
        self.annotations_changed = False
        self.sheet.annoAdded.connect(self._mark_changed)
        self.sheet.annoUpdated.connect(lambda _i, _a: self._mark_changed())
        self.sheet.annoRemoved.connect(lambda _i: self._mark_changed())

        w, h = PageEditor._last_size or (1240, 900)
        scr = self.screen() or QApplication.primaryScreen()
        if scr is not None:
            avail = scr.availableGeometry()
            # 笔记本 1366×768 上 1240×900 装不下，按屏幕收一收，别顶到任务栏外面
            w = min(w, max(880, avail.width() - 80))
            h = min(h, max(620, avail.height() - 90))
        self.resize(w, h)

    def _mark_changed(self, *_args) -> None:
        self.annotations_changed = True

    def keyPressEvent(self, event):  # noqa: N802 — Qt 命名
        """PageUp / PageDown 翻页，Ctrl+Z / Ctrl+Y 撤销重做。

        撤销键放在窗口这一层而不是画布：画布没焦点时（比如刚点过工具条按钮、
        字号框）也照样能按。字号框里有自己的文字撤销，那种情况下由它自己处理，
        标准行为，不抢。
        """
        if self._is_redo(event):
            self.sheet.redoRequested.emit()
            return
        if event.matches(QKeySequence.StandardKey.Undo):
            self.sheet.undoRequested.emit()
            return
        if event.key() in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            step = -1 if event.key() == Qt.Key.Key_PageUp else 1
            self.sheet.goto(self.sheet._page + step)
            return
        super().keyPressEvent(event)

    @staticmethod
    def _is_redo(event) -> bool:
        """Ctrl+Y 是 Windows 的老习惯，Ctrl+Shift+Z 是别处过来的肌肉记忆，都认。"""
        if event.matches(QKeySequence.StandardKey.Redo):
            return True
        mods = event.modifiers()
        return (event.key() == Qt.Key.Key_Z
                and bool(mods & Qt.KeyboardModifier.ControlModifier)
                and bool(mods & Qt.KeyboardModifier.ShiftModifier))

    def done(self, result: int) -> None:
        """窗口关闭的唯一出口：点「完成」、点 ×、按 Esc 都到这里。

        QDialog 的 accept/reject 走的是 done() 而不是 closeEvent，所以窗口
        尺寸和渲染线程的收尾都挂在这里，三条路都跑得到。
        """
        PageEditor._last_size = (self.width(), self.height())
        self.sheet.shutdown()
        super().done(result)


class ThumbList(QListWidget):
    """缩略图列表：自己处理拖拽，不用 Qt 的 InternalMove 模态循环。

    IconMode 下没有「行高」概念，用 indexAt(pos).row() 定位卡片；
    跨到另一张就发一次 rowsMoved，由主窗口重排并重建列表。
    """

    rowsMoved = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        self.setGridSize(QSize(THUMB_SIZE + 46, THUMB_SIZE + 86))
        self.setSpacing(10)
        self.setWordWrap(True)
        self.setUniformItemSizes(False)
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        self._drag_row = -1
        self._press_pos = None

    def mousePressEvent(self, event):  # noqa: N802 — Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self.indexAt(event.position().toPoint())
            self._drag_row = idx.row() if idx.isValid() else -1
            self._press_pos = event.position()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 — Qt 命名
        dragging = (self._drag_row >= 0 and self._press_pos is not None
                    and bool(event.buttons() & Qt.MouseButton.LeftButton))
        if not dragging:
            return super().mouseMoveEvent(event)
        if (event.position() - self._press_pos).manhattanLength() < QApplication.startDragDistance():
            return super().mouseMoveEvent(event)
        idx = self.indexAt(event.position().toPoint())
        row = idx.row() if idx.isValid() else -1
        if row < 0 or row == self._drag_row or not 0 <= row < self.count():
            return super().mouseMoveEvent(event)
        src, dst = self._drag_row, row
        self._drag_row = dst
        self.rowsMoved.emit(src, dst)
        self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseReleaseEvent(self, event):  # noqa: N802 — Qt 命名
        self._drag_row = -1
        self._press_pos = None
        self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):  # noqa: N802 — Qt 命名
        if self._drag_row < 0:
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        super().leaveEvent(event)


# ---------------------------------------------------------------- 编辑选中


def default_batch_values() -> dict:
    """整批「图像修复」的出厂值。

    只放图片本身的东西。页面排版与 PDF 输出（质量 / 体积 / 目录）属于整份
    PDF，控件在主窗体右栏，不在这份状态里——放进来就会出现「在同一批里
    改了输出，却要另外开个窗口才能看见」的混乱。
    """
    d = ConvertOptions()
    # 裁边 / 纠偏 / 透视矫正默认不勾：修复是基于估计的自动处理，不该在用户
    # 没开口之前就动他的图；要用的人在编辑窗口里自己点亮。
    return {
        "crop": False, "deskew": False, "perspective": False,
        "shadow": d.shadow, "color_mode": d.color_mode,
    }


class SaveImagesWorker(QThread):
    """把处理后的图写进 out；几十张时放在界面线程上会把窗口卡住。"""

    progressed = Signal(int, int, str)
    finished_with = Signal(object, str)

    def __init__(self, items, opt: ConvertOptions, parent=None):
        super().__init__(parent)
        self.items = items
        self.opt = opt
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self):
        try:
            reps = save_processed_images(
                self.items, self.opt,
                progress=lambda d, n, rep: self.progressed.emit(d, n, rep.dst),
                cancel=lambda: self._cancel,
            )
            self.finished_with.emit(reps, "")
        except BaseException as exc:  # noqa: BLE001 — 线程里漏出去会带崩进程
            self.finished_with.emit(None, f"{type(exc).__name__}: {exc}")


class EditDialog(QDialog):
    """「编辑选中」：只管图片本身，不碰整份 PDF 的东西。

    这里能改的只有裁边 / 纠偏 / 透视拉正 / 去阴影 / 画面效果——参数写在
    ImageInfo.tune 里，只影响像素。纸张、清晰度、边距、拼版、JPEG 质量、
    体积上限、保存目录全部属于整份 PDF，留在主窗体右栏，本窗口一个都不放。
    上一次把「输出」塞进来，用户根本分不清改哪个是改一张、改哪个是改整本。

    勾上「同时用于其它图片」，这组参数成为这一批的默认值（已有单张设置
    会被清掉，否则「作用于其它图片」名不副实）。

    点「应用」顺手把处理后的图写进原图旁的 out：改完图是要拿到图的，不该
    为了要一张 jpg 先去生成 PDF。存的是全分辨率那张，不缩进纸格子；之后
    出片优先用这张（见 core.converter.edited_copy）。
    """

    def __init__(self, items, index: int, opt: ConvertOptions, batch: dict,
                 parent=None):
        super().__init__(parent)
        self.items = list(items)
        self.index = max(0, min(int(index), max(0, len(self.items) - 1)))
        self.page_opt = opt
        self.batch = dict(batch)
        self.saved: list = []          # 应用时存下来的图，给主窗体做提示
        self.save_error = ""
        self._syncing = False
        self._save_worker: SaveImagesWorker | None = None
        self._save_result: tuple = (None, "")
        self._save_cancelled = False
        info = self.items[self.index] if self.items else None
        self.path = info.path if info is not None else Path("")
        tune = getattr(info, "tune", None)
        self.tune = dict(tune) if isinstance(tune, dict) else None

        self.setWindowTitle(f"编辑选中 · 第 {self.index + 1}/{len(self.items)} 张")
        self.setWindowIcon(app_icon())
        self.resize(980, 690)

        root = QVBoxLayout(self)
        head = QLabel(f"<b>{html.escape(self.path.name)}</b>"
                      f"　<span style='color:#555'>第 {self.index + 1} / "
                      f"{len(self.items)} 张 · 只改图片本身</span>")
        head.setWordWrap(True)
        head.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(head)

        body = QHBoxLayout()
        self.view = LivePreview(self)
        body.addWidget(self.view, 3)

        side = QVBoxLayout()
        side.setSpacing(8)
        side.addWidget(self._build_fix_group())
        side.addWidget(self._build_save_group())
        side.addStretch(1)
        acts = QHBoxLayout()
        self.btn_follow = QPushButton("跟随整批")
        self.btn_follow.setToolTip("修复项恢复成整批默认值；应用后这张不再有单独设置")
        self.btn_follow.clicked.connect(self._follow_batch)
        acts.addWidget(self.btn_follow)
        # 「看效果」按钮退役：参数一改立即重算，按钮只会让人以为不点就不算
        side.addLayout(acts)
        body.addLayout(side, 2)
        root.addLayout(body, 1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel
                                | QDialogButtonBox.StandardButton.Apply)
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        apply_btn = btns.button(QDialogButtonBox.StandardButton.Apply)
        apply_btn.setText("应用")
        apply_btn.setDefault(True)
        btns.rejected.connect(self.reject)
        apply_btn.clicked.connect(self._apply)
        root.addWidget(btns)

        self._fill(self._initial_values())
        self.view.set_files([self.path], 0, self._compose(), {})
        self._connect()

    # ------------------------------------------------------------ 搭界面
    def _build_fix_group(self) -> QGroupBox:
        box = QGroupBox("图像修复（只改图片）")
        grid = QGridLayout(box)
        grid.setVerticalSpacing(8)
        self.crop_check = QCheckBox("裁边")
        self.deskew_check = QCheckBox("纠偏")
        self.persp_check = QCheckBox("透视拉正")
        grid.addWidget(self.crop_check, 0, 0)
        grid.addWidget(self.deskew_check, 0, 1)
        grid.addWidget(self.persp_check, 1, 0, 1, 2)
        grid.addWidget(QLabel("去阴影"), 2, 0)
        self.shadow_box = QComboBox()
        for key, name in SHADOW_NAMES.items():
            self.shadow_box.addItem(name, key)
        grid.addWidget(self.shadow_box, 2, 1, 1, 2)
        grid.addWidget(QLabel("画面效果"), 3, 0)
        self.color_box = QComboBox()
        for key, name in COLOR_NAMES.items():
            self.color_box.addItem(name, key)
        grid.addWidget(self.color_box, 3, 1, 1, 2)
        self.apply_all = QCheckBox("同时用于其它图片")
        self.apply_all.setToolTip("勾上 = 这一批全部按这组设置处理，已有的单张设置会清掉")
        grid.addWidget(self.apply_all, 4, 0, 1, 3)
        return box

    def _build_save_group(self) -> QGroupBox:
        box = QGroupBox("保存处理后的图")
        grid = QGridLayout(box)
        grid.setVerticalSpacing(8)
        self.save_images = QCheckBox("应用时存到原图旁的 out")
        self.save_images.setChecked(True)
        self.save_images.setToolTip("存全分辨率那张，同名覆盖；黑白存 PNG。出片优先用它")
        grid.addWidget(self.save_images, 0, 0, 1, 2)
        where = QLabel("路径：原图目录 / out / 原文件名")
        where.setStyleSheet("color:#666; font-size:12px;")
        grid.addWidget(where, 1, 0, 1, 2)
        return box

    # ------------------------------------------------------------ 取值
    def _initial_values(self) -> dict:
        """这张有单独设置就显示它的，没有就显示整批默认。"""
        values = dict(self.batch)
        if self.tune:
            for key in ("crop", "deskew", "perspective", "shadow", "color_mode"):
                if key in self.tune:
                    values[key] = self.tune[key]
        return values

    def _fill(self, values: dict) -> None:
        self._syncing = True
        try:
            self.crop_check.setChecked(bool(values.get("crop")))
            self.deskew_check.setChecked(bool(values.get("deskew")))
            self.persp_check.setChecked(bool(values.get("perspective")))
            self.shadow_box.setCurrentIndex(
                max(0, self.shadow_box.findData(values.get("shadow", "off"))))
            self.color_box.setCurrentIndex(
                max(0, self.color_box.findData(values.get("color_mode", "color"))))
        finally:
            self._syncing = False

    def _connect(self) -> None:
        for widget in (self.crop_check, self.deskew_check, self.persp_check,
                       self.apply_all, self.save_images):
            widget.toggled.connect(self._on_changed)
        for box in (self.shadow_box, self.color_box):
            box.currentIndexChanged.connect(self._on_changed)

    def repair_values(self) -> dict:
        """这组修复参数：勾了「同时用于其它图片」就是整批的，不勾就只属于这张。"""
        crop = self.crop_check.isChecked()
        deskew = self.deskew_check.isChecked()
        persp = self.persp_check.isChecked()
        return {
            "geometry": bool(crop or deskew or persp),
            "crop": crop, "deskew": deskew, "perspective": persp,
            "shadow": self.shadow_box.currentData(),
            "color_mode": self.color_box.currentData(),
        }

    def output_values(self) -> dict:
        """本窗口不再管 PDF 输出；留着这个方法只为兼容老调用，永远返回空。"""
        return {}

    def apply_to_all(self) -> bool:
        return self.apply_all.isChecked()

    def _compose(self) -> ConvertOptions:
        """页面与输出来自主窗体，只有修复项来自本窗口。

        预览与「应用时存图」都走这一组参数，保证存下来的图和之后出片
        是同一个观感。
        """
        r = self.repair_values()
        # 自由变换已取消：强制中性，哪怕旧设置里残留过旋转 / 拉伸也不再生效
        return self.page_opt.copy(
            shadow=r["shadow"], geometry=r["geometry"],
            perspective=r["perspective"], crop=r["crop"], deskew=r["deskew"],
            color_mode=r["color_mode"],
            rotate=0.0, scale_x=1.0, scale_y=1.0,
        )

    # ------------------------------------------------------------ 动作
    def _on_changed(self, *_args) -> None:
        if self._syncing:
            return
        self.view.set_options(self._compose())

    def _follow_batch(self) -> None:
        b = self.batch
        self._syncing = True
        try:
            self.crop_check.setChecked(bool(b["crop"]))
            self.deskew_check.setChecked(bool(b["deskew"]))
            self.persp_check.setChecked(bool(b["perspective"]))
            self.shadow_box.setCurrentIndex(
                max(0, self.shadow_box.findData(b["shadow"])))
            self.color_box.setCurrentIndex(
                max(0, self.color_box.findData(b["color_mode"])))
        finally:
            self._syncing = False
        self.view.set_options(self._compose())

    def _show_effect(self) -> None:
        """兼容保留：立即重算一次（界面已改成参数一改自动重算）。"""
        self.view.set_options(self._compose())
        self.view.render_now()

    def _apply(self) -> None:
        if self.save_images.isChecked():
            ok = self._save_images()
            if not ok:
                if self._save_cancelled:
                    QMessageBox.information(
                        self, APP_NAME, "已取消保存，设置没有应用。\n"
                        "要应用设置请重新点「应用」，或关掉「应用时存到原图旁的 out」。")
                elif self.save_error:
                    QMessageBox.warning(self, APP_NAME,
                                        f"没能保存处理后的图：\n{self.save_error}"
                                        "\n设置没有应用。")
                else:
                    QMessageBox.warning(self, APP_NAME,
                                        "没有图片保存成功，设置没有应用。")
                return
            if self.save_error:
                # 只要有一张成功旧版就返回 True，其余失败被完全吞掉；现在说清楚
                QMessageBox.warning(
                    self, APP_NAME,
                    f"有 {self.save_failed} 张没存下来：\n{self.save_error}\n"
                    "其余已保存，设置仍会应用。")
        self.accept()

    # ------------------------------------------------------------ 存图
    def _save_images(self) -> bool:
        """把处理后的图写进 out；勾了「同时用于其它图片」就整批都写。"""
        opt = self._compose()
        self.save_failed = 0
        if self.apply_all.isChecked():
            targets = [replace(item, tune=None) for item in self.items]
        else:
            targets = [replace(self.items[self.index], tune=None)]
        if len(targets) == 1:
            self._save_result = (save_processed_images(targets, opt), "")
        else:
            self._save_with_progress(targets, opt)
        if self._save_cancelled:
            self.save_error = ""
            return False
        reps, err = self._save_result
        self.saved = [r for r in (reps or []) if r.status != "failed" and r.dst]
        failed = [r for r in (reps or []) if r.status == "failed"]
        self.save_failed = len(failed)
        bits = [err] if err else []
        if failed:
            bits.append("；".join(f"{Path(r.src).name}：{r.note}" for r in failed[:3]))
        self.save_error = " ".join(part for part in bits if part)
        return bool(self.saved) and not err

    def _save_with_progress(self, targets, opt: ConvertOptions) -> None:
        prog = QProgressDialog("正在保存处理后的图片…", "取消", 0, len(targets), self)
        prog.setWindowTitle("保存处理后的图片")
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setAutoReset(False)
        prog.setValue(0)
        loop = QEventLoop()
        worker = SaveImagesWorker(targets, opt, self)
        self._save_worker = worker

        def on_progress(done: int, total: int, dst: str) -> None:
            prog.setValue(done)
            if dst:
                prog.setLabelText(f"正在保存 {done}/{total}：{Path(dst).name}")

        worker.progressed.connect(on_progress)
        worker.finished_with.connect(
            lambda reps, err: self._finish_save(reps, err, loop))
        prog.canceled.connect(worker.cancel)
        worker.start()
        loop.exec()
        worker.wait(60000)
        self._save_cancelled = prog.wasCanceled()
        prog.close()
        self._save_worker = None

    def _finish_save(self, reps, err: str, loop: QEventLoop) -> None:
        self._save_result = (reps, err)
        loop.quit()

    def done(self, result):  # noqa: N802 — Qt 命名
        if self._save_worker is not None:
            self._save_worker.cancel()
            self._save_worker.wait(30000)
        self.view.shutdown()
        super().done(result)


class CaptionDialog(QDialog):
    """一次给整批图片写图注：左边是文件，右边是那行小字。

    留空 = 用文件名清洗后的结果，提示里直接把「会显示成什么」写出来，所以想
    沿用文件名的人不必先把名字抄一遍。文件名是 IMG_2026… 这类纯编号时提示会
    说明「不显示」——省得用户以为坏了。
    """

    def __init__(self, paths, captions: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑图注")
        self.setMinimumSize(680, 460)
        self._paths = [Path(p) for p in paths]
        given = dict(captions or {})

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        tip = QLabel("每张图下面那一行小字。留空 = 用文件名；"
                     "文件名是 IMG_2026… / 微信图片_… 这类纯编号时会自动不显示。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#555;")
        root.addWidget(tip)

        self.table = QTableWidget(len(self._paths), 2)
        self.table.setHorizontalHeaderLabels(["图片", "图注"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for r, path in enumerate(self._paths):
            name = QTableWidgetItem(f"{r + 1}. {path.name}")
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name.setToolTip(str(path))
            self.table.setItem(r, 0, name)
            auto = clean_caption(path.stem)
            cell = QTableWidgetItem(given.get(str(path), ""))
            cell.setToolTip("留空则显示：" + (auto or "（不显示：文件名里没有可用的字）"))
            self.table.setItem(r, 1, cell)
        if self._paths:
            self.table.setColumnWidth(0, 240)
            self.table.setCurrentCell(0, 1)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_auto = QPushButton("全部改回用文件名")
        self.btn_auto.setToolTip("清掉所有手写图注，回到「用文件名」")
        self.btn_auto.clicked.connect(self.clear_all)
        row.addWidget(self.btn_auto)
        row.addStretch(1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        row.addWidget(box)
        root.addLayout(row)

    def clear_all(self) -> None:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 1)
            if item is not None:
                item.setText("")

    def values(self) -> dict[str, str]:
        """只回传真正手写的那些；留空的交给文件名兜底。"""
        out: dict[str, str] = {}
        for r, path in enumerate(self._paths):
            item = self.table.item(r, 1)
            text = " ".join((item.text() if item else "").split())
            if text:
                out[str(path)] = text[:60]
        return out


class Worker(QThread):
    """整批转换线程：进度回主线程、取消只置标志，不碰控件。"""

    progressed = Signal(int, int, object)
    finished_with = Signal(object, str)

    def __init__(self, items, out_dir, opt: ConvertOptions, parent=None,
                 annotations=None):
        super().__init__(parent)
        self.items = items
        self.out_dir = out_dir
        self.opt = opt
        self.annotations = list(annotations or [])
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self):
        try:
            with NetworkAudit() as audit:
                from core.converter import convert_batch

                report = convert_batch(
                    self.items, out_dir=self.out_dir, options=self.opt,
                    progress=lambda d, n, i: self.progressed.emit(d, n, i),
                    cancel=lambda: self._cancel,
                    annotations=self.annotations, network_audit=audit,
                )
                report.network_attempts = audit.attempts
            self.finished_with.emit(report, audit.summary())
        except BaseException as exc:  # noqa: BLE001 — 线程里漏出去会带崩进程
            self.finished_with.emit(None, f"{type(exc).__name__}: {exc}")


class MainWindow(QMainWindow):
    UNDO_MAX = 60         # 最多回退 60 步，再多也没人按得回去
    UNDO_COALESCE = 1.0   # 同一处连续改动（比如一直点字号）合成一步的时间窗

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · 创炫互动")
        self.setWindowIcon(app_icon())
        self.resize(1400, 880)
        self.setAcceptDrops(True)

        self.files: list[Path] = []
        # 整批图像修复 + 输出设置只存状态，不在主窗体摆控件
        self.batch_values: dict = default_batch_values()
        self.tunes: dict[str, dict] = {}
        # 成品预览里手动拖出来的位置（页面比例），按文件路径存；空 = 自动落格
        self.places: dict[str, dict] = {}
        # 每张图手写的图注，按文件路径存；空 = 用文件名清洗后的结果
        self.captions: dict[str, str] = {}
        # 页面上加的文字 / 方框 / 箭头 / 涂黑 / 签名图。每条自带 page（逻辑页号），
        # 所以删图导致页数变少时，多出来的那几条还留在列表里，加回来接着用。
        self.annotations: list[dict] = []
        # 撤销 / 重做：存的是「改动之前那一版」的完整状态（标注 + 每张图的位置）。
        # 存全量而不是存动作序列——量小、不会漏状态，也不存在「反操作算错」。
        self._undo: collections.deque = collections.deque(maxlen=self.UNDO_MAX)
        self._redo: collections.deque = collections.deque(maxlen=self.UNDO_MAX)
        self._undo_tag = ""
        self._undo_at = 0.0
        # 页面编辑窗口（同一时刻最多一个）
        self._editor = None
        self.results: dict[str, str] = {}
        self._size_cache: dict[str, str] = {}
        self._thumb_cache: dict[str, QIcon] = {}
        self._path_to_row: dict[str, int] = {}
        self._items: list = []
        self._items_key: tuple | None = None
        self._sel_row = 0
        self.report = None
        self.worker: Worker | None = None
        self._running = False
        self._closing = False
        self._syncing = False

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_left())
        right = self._build_right()
        # 两列参数在 460 以下会开始互相挤，宁可让左边缩略图少一格
        right.setMinimumWidth(460)
        split.addWidget(right)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        split.setSizes([640, 470])
        outer.addWidget(split, 1)
        outer.addLayout(self._build_bottom_row())
        self.setCentralWidget(root)

        # 先铺默认值再接线：否则铺值时的 valueChanged 会被当成「用户改了参数」
        self._set_defaults()
        self._connect_params()
        self.statusBar().showMessage("全程离线 · 原图只读")
        self._after_params_changed()
        self.rebuild_list()   # 统一空态：提示可见、列表隐藏、按钮与进度条一致

    # ------------------------------------------------------------ 界面搭建
    def _build_left(self) -> QWidget:
        box = QWidget()
        root = QVBoxLayout(box)
        root.setContentsMargins(10, 10, 6, 8)

        self.empty_hint = QLabel(EMPTY_HINT)
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setStyleSheet(
            "padding: 22px; border: 1px dashed #999; border-radius: 8px; color:#444;")
        root.addWidget(self.empty_hint)

        row = QHBoxLayout()
        self.btn_files = QPushButton("选择图片")
        self.btn_files.setToolTip(f"选一张或多张图片（{SUPPORT_TEXT}）")
        self.btn_files.clicked.connect(self.pick_files)
        row.addWidget(self.btn_files)
        self.btn_dir = QPushButton("选择文件夹")
        self.btn_dir.clicked.connect(self.pick_folder)
        row.addWidget(self.btn_dir)
        self.btn_up = QPushButton("上移")
        self.btn_up.setToolTip("把选中的图往前挪一页")
        self.btn_up.clicked.connect(lambda: self.move_selected(-1))
        row.addWidget(self.btn_up)
        self.btn_down = QPushButton("下移")
        self.btn_down.setToolTip("把选中的图往后挪一页")
        self.btn_down.clicked.connect(lambda: self.move_selected(1))
        row.addWidget(self.btn_down)
        self.btn_del = QPushButton("移出列表")
        self.btn_del.setToolTip("只移出本批，原图不动")
        self.btn_del.clicked.connect(self.remove_selected)
        row.addWidget(self.btn_del)
        self.btn_edit = QPushButton("编辑选中")
        self.btn_edit.setToolTip("改这一张的修复与输出；勾上「同时用于其它图片」就是整批生效")
        self.btn_edit.clicked.connect(self.edit_selected)
        row.addWidget(self.btn_edit)
        row.addStretch(1)
        root.addLayout(row)

        self.thumb_list = ThumbList()
        self.thumb_list.setToolTip("拖动缩略图改页序 · 双击编辑这张 · Delete 移出本批")
        self.thumb_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.thumb_list.doubleClicked.connect(lambda _: self.edit_selected())
        self.thumb_list.rowsMoved.connect(self.on_rows_moved)
        self.del_shortcut = QShortcut(QKeySequence("Delete"), self.thumb_list)
        self.del_shortcut.activated.connect(self.remove_selected)
        root.addWidget(self.thumb_list, 1)

        self.sel_label = QLabel("还没有图片")
        self.sel_label.setStyleSheet("color:#333; font-weight:600;")
        self.sel_label.setWordWrap(True)
        root.addWidget(self.sel_label)
        return box

    def _build_right(self) -> QWidget:
        """右栏只放「整份 PDF」的参数：页面排版 + PDF 输出，下面是成品预览。

        分工是硬边界：这里每一项都作用于整本 PDF；只影响单张图片的修复项
        在「编辑选中」窗口里，两边不重叠，用户不用猜「改这个到底改哪儿」。
        """
        box = QWidget()
        root = QVBoxLayout(box)
        root.setContentsMargins(6, 10, 10, 8)

        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        panel = QWidget()
        # 三组参数挤在一栏里，全局那套 32px 的输入框高度在这里会把成品预览
        # 压到只剩一条；只在这一块收紧，别处的可点区域不受影响。
        panel.setObjectName("paramPanel")
        panel.setStyleSheet(
            "#paramPanel QComboBox, #paramPanel QSpinBox, #paramPanel QLineEdit"
            "{ min-height: 24px; padding: 0 6px; }"
            "#paramPanel QGroupBox { margin-top: 9px; }")
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(0, 0, 2, 0)
        pl.setSpacing(4)
        pl.addWidget(self._build_page_group())
        pl.addWidget(self._build_pager_group())
        pl.addWidget(self._build_caption_group())
        pl.addWidget(self._build_output_group())
        pl.addStretch(1)
        # 窗口被压矮时参数区自己滚，不把成品预览挤没
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        split.addWidget(scroll)

        self.sheet = SheetPreview(compact=True)
        self.sheet.setMinimumHeight(240)
        split.addWidget(self.sheet)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([panel.sizeHint().height(), 360])
        root.addWidget(split, 1)
        self._right_split = split
        self._param_panel = panel
        # 布局稳定后才知道实际可用高度，那时再按「参数区要多少给多少、
        # 剩下的全给成品预览」分配一次；只用一次，之后由用户自己拖。
        QTimer.singleShot(0, self._fit_right_split)
        return box

    def _fit_right_split(self) -> None:
        split = getattr(self, "_right_split", None)
        if split is None:
            return
        total = split.height()
        if total <= 0:
            return
        hint = self._param_panel.sizeHint().height()
        # 参数区要多少给多少，剩下的全给成品预览（右栏只负责「看一眼对不对」）
        mine = max(240, min(hint, total - 320))
        split.setSizes([mine, max(160, total - mine)])

    # ------------------------------------------------------------ 页面编辑窗口
    def open_page_editor(self, modal: bool = True):
        """开一个独立窗口来编辑这一份 PDF 的页面。

        之前是在右栏那块预览里就地编辑，画布只有三百来像素高，写字拖角全靠
        猜。现在拆成两件事：右栏看效果，动手去独立窗口。modal=False 给自动
        化测试用（模态 exec() 会挡住测试线程）。
        """
        if self._running or not self.files or not getattr(self, "sheet", None):
            return None
        if self._editor is not None:
            self._editor.raise_()
            self._editor.activateWindow()
            return self._editor
        dlg = PageEditor(self, self.preview_items(), self.options(),
                         self.annotations)
        dlg.sheet.placeChanged.connect(self.on_place_changed)
        dlg.sheet.resetPage.connect(self.on_reset_page)
        dlg.sheet.annoAdded.connect(self.on_anno_added)
        dlg.sheet.annoUpdated.connect(self.on_anno_updated)
        dlg.sheet.annoRemoved.connect(self.on_anno_removed)
        dlg.sheet.willChange.connect(self.push_undo)
        dlg.sheet.undoRequested.connect(self.undo)
        dlg.sheet.redoRequested.connect(self.redo)
        dlg.finished.connect(lambda _result=0, d=dlg: self._editor_closed(d))
        self._editor = dlg
        # 窗口可能是在已有历史之后才打开的（先改右栏、再开窗），按钮状态要对上
        self._sync_undo()
        if modal:
            dlg.exec()
        else:
            dlg.show()
        return dlg

    def _editor_closed(self, dlg) -> None:
        """窗口关了：右栏那份预览补上刚才的改动，再回到只读。"""
        if self._editor is dlg:
            self._editor = None
        if dlg.annotations_changed:
            self.sheet.set_annotations(self.annotations)
            self._update_anno_hint()
        self.sheet.set_document(self.preview_items(), self.options(),
                               keep_view=True)

    def showEvent(self, event):  # noqa: N802 — Qt 命名
        super().showEvent(event)
        # 窗口第一次真正显示出来才有高度可分，这时再决定参数区该占多少
        if not getattr(self, "_split_fitted", False):
            self._split_fitted = True
            QTimer.singleShot(0, self._fit_right_split)

    def _form(self, title: str) -> tuple[QGroupBox, QGridLayout]:
        group = QGroupBox(title)
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        grid.setContentsMargins(8, 6, 8, 4)
        # 标签列只留够「最低画质」的宽度；两列布局里多 20px 就会把右边控件挤出边框
        grid.setColumnMinimumWidth(0, 62)
        grid.setColumnMinimumWidth(2, 62)
        return group, grid

    def _build_page_group(self) -> QGroupBox:
        """页面排版：整份 PDF 的纸张 / 清晰度 / 边距 / 拼版 / 方向。

        两列排布不是为了好看——右栏在 660 高的窗口里放不下九行单列，
        挤到最后会把「保存到」推出可视区，逼用户去滚。这里只占四行。
        """
        group, grid = self._form("页面排版（整份 PDF）")
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        grid.addWidget(QLabel("纸张"), 0, 0)
        self.paper_box = QComboBox()
        self.paper_box.addItems(PAPER_CHOICES)
        self.paper_box.setToolTip("「自适应」让页面按图片自身比例走，书页 / 长图 / 截图都不被塞进 A4")
        grid.addWidget(self.paper_box, 0, 1)

        grid.addWidget(QLabel("清晰度"), 0, 2)
        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(72, 600)
        self.dpi_spin.setSingleStep(10)
        self.dpi_spin.setSuffix(" dpi")
        self.dpi_spin.setToolTip("目标打印分辨率；源图不够大时不会放大凑数")
        grid.addWidget(self.dpi_spin, 0, 3)

        grid.addWidget(QLabel("边距"), 1, 0)
        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(0, 40)
        self.margin_spin.setSuffix(" mm")
        grid.addWidget(self.margin_spin, 1, 1)

        grid.addWidget(QLabel("拼版"), 1, 2)
        self.layout_box = QComboBox()
        for name, _n, _lay in LAYOUT_CHOICES:
            self.layout_box.addItem(name)
        grid.addWidget(self.layout_box, 1, 3)

        grid.addWidget(QLabel("方向"), 2, 0)
        self.orient_box = QComboBox()
        for key, name in ORIENTATION_NAMES.items():
            self.orient_box.addItem(name, key)
        self.orient_box.setToolTip("自动 = 一页一张时横图自动换成横版")
        grid.addWidget(self.orient_box, 2, 1)

        grid.addWidget(QLabel("图间距"), 2, 2)
        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(0, 20)
        self.gap_spin.setSuffix(" mm")
        self.gap_spin.setToolTip("一页多张时，格子之间留多宽的缝；一页一张用不上")
        grid.addWidget(self.gap_spin, 2, 3)
        return group

    def _build_pager_group(self) -> QGroupBox:
        """页码与页脚：整份 PDF 的事，和「这张图长什么样」无关。

        页码默认关——证件、票据这种一两页的成品，页码只是噪音。位置、格式、
        起始页号都能改，是因为「接在别的文档后面」要求页码从 7 开始，
        或者只要「3 / 12」这种最省地方的写法。
        """
        group, grid = self._form("页码与页脚（整份 PDF）")
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self.page_num_check = QCheckBox("显示页码")
        self.page_num_check.setToolTip("按下面的位置、格式和起始页号，给每页打页码")
        grid.addWidget(self.page_num_check, 0, 0, 1, 2)

        grid.addWidget(QLabel("位置"), 0, 2)
        self.page_pos_box = QComboBox()
        for key, name in PAGE_POSITIONS.items():
            self.page_pos_box.addItem(name, key)
        self.page_pos_box.setToolTip("页码落在纸的哪条边上；页脚文字会自动让到另一侧，不会压字")
        grid.addWidget(self.page_pos_box, 0, 3)

        grid.addWidget(QLabel("格式"), 1, 0)
        self.page_fmt_box = QComboBox()
        for key, name in PAGE_FORMATS.items():
            self.page_fmt_box.addItem(name.replace("{n}", "1").replace("{total}", "5"), key)
        grid.addWidget(self.page_fmt_box, 1, 1, 1, 3)

        grid.addWidget(QLabel("起始"), 2, 0)
        self.page_start_spin = QSpinBox()
        self.page_start_spin.setRange(0, 9999)
        self.page_start_spin.setToolTip("第一页的页号；接在打印好的文档后面时用得上")
        grid.addWidget(self.page_start_spin, 2, 1)

        grid.addWidget(QLabel("字号"), 2, 2)
        self.page_size_spin = QSpinBox()
        self.page_size_spin.setRange(6, 24)
        self.page_size_spin.setSuffix(" pt")
        grid.addWidget(self.page_size_spin, 2, 3)

        grid.addWidget(QLabel("页脚"), 3, 0)
        self.footer_edit = QLineEdit()
        self.footer_edit.setMaxLength(80)
        self.footer_edit.setPlaceholderText("留空不画；如「2026年11月 报销单」")
        self.footer_edit.setToolTip("每页左下角的一行小字，和页码并排但不重叠")
        grid.addWidget(self.footer_edit, 3, 1, 1, 3)
        return group

    def _build_caption_group(self) -> QGroupBox:
        """图注与书签：图片下方那行小字，以及阅读器侧边栏的目录。

        图注默认关。理由是文件名大多长成 IMG_20261109_124512，印在纸上对读者
        没有任何信息量，还顺手泄露拍摄时间；真需要说明的场景（合同第几页、
        病历、施工记录）自己勾上就有，而且能逐张写。
        书签默认开。它不占版面、几十项只多几 KB，几十页的扫描件全靠它跳页。
        """
        group, grid = self._form("图注与书签（整份 PDF）")
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self.caption_check = QCheckBox("图下方显示图注")
        self.caption_check.setToolTip(
            "每张图下面一行小字。没自己写的用文件名，文件名是 IMG_2026… / 微信图片_… "
            "这种纯编号时自动留空，不会印一串数字")
        grid.addWidget(self.caption_check, 0, 0, 1, 2)

        grid.addWidget(QLabel("字号"), 0, 2)
        self.caption_size_spin = QSpinBox()
        self.caption_size_spin.setRange(6, 18)
        self.caption_size_spin.setSuffix(" pt")
        self.caption_size_spin.setToolTip("图注字号；格子矮时它最多占格高的三成")
        grid.addWidget(self.caption_size_spin, 0, 3)

        self.bookmark_check = QCheckBox("生成 PDF 书签（侧边栏目录，点一下跳页）")
        self.bookmark_check.setToolTip("每张图一个目录项，用图注或文件名命名；不占版面")
        grid.addWidget(self.bookmark_check, 1, 0, 1, 4)

        self.btn_caption = QPushButton("编辑图注…")
        self.btn_caption.setToolTip("一次给多张写说明；留空的那张仍按文件名")
        self.btn_caption.clicked.connect(self.edit_captions)
        grid.addWidget(self.btn_caption, 2, 0, 1, 4)
        return group

    def _build_output_group(self) -> QGroupBox:
        """整份 PDF 的输出：质量、体积上限、保存目录。

        这些不是「某张图」的事：质量决定 PDF 里全部图片的编码，体积是整本
        的上限，目录是这份 PDF 落在哪。所以和页面排版并排放在主窗体，
        而单张修复留在「编辑选中」窗口里。
        """
        group, grid = self._form("PDF 输出（整份 PDF）")
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        quality_label = QLabel("质量")
        quality_label.setToolTip("JPEG 质量；黑白（1 位无损）不走这个质量档")
        grid.addWidget(quality_label, 0, 0)
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(40, 95)
        self.quality_spin.setToolTip("JPEG 质量；黑白（1 位无损）不走这个质量档")
        grid.addWidget(self.quality_spin, 0, 1)

        self.use_target = QCheckBox("体积 ≤")
        self.use_target.setToolTip("勾上后按实测体积自动降 DPI 与质量，往上贴这个上限")
        grid.addWidget(self.use_target, 0, 2)
        self.target_mb = QSpinBox()
        self.target_mb.setRange(1, 200)
        self.target_mb.setSuffix(" MB")
        grid.addWidget(self.target_mb, 0, 3)

        grid.addWidget(QLabel("最低画质"), 1, 0)
        self.floor_box = QComboBox()
        self.floor_box.addItem("150 dpi（打印可辨认）", 150)
        self.floor_box.addItem("96 dpi（只给人看）", 96)
        grid.addWidget(self.floor_box, 1, 1, 1, 3)

        grid.addWidget(QLabel("保存到"), 2, 0)
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("留空 = 第一张图旁的 out")
        self.out_edit.setToolTip("整份 PDF 的保存目录；处理后的单张图另存在原图旁的 out")
        line.addWidget(self.out_edit, 1)
        self.btn_out = QPushButton("浏览")
        self.btn_out.setFixedWidth(56)
        self.btn_out.setToolTip("选择 PDF 保存目录")
        self.btn_out.clicked.connect(self.pick_out_dir)
        line.addWidget(self.btn_out)
        grid.addLayout(line, 2, 1, 1, 3)
        return group

    def _build_bottom_row(self) -> QHBoxLayout:
        """底部进度条与动作按钮，独立于左右分栏。"""
        row = QHBoxLayout()
        row.setContentsMargins(10, 6, 10, 8)
        row.setSpacing(8)
        self.progress = QProgressBar()
        self.progress.setFormat("%v / %m")
        row.addWidget(self.progress, 1)

        self.btn_start = QPushButton("开始生成 PDF")
        self.btn_start.setMinimumWidth(150)
        self.btn_start.setDefault(True)
        self.btn_start.clicked.connect(self.start)
        row.addWidget(self.btn_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel)
        row.addWidget(self.btn_cancel)
        self.btn_open = QPushButton("打开结果文件夹")
        self.btn_open.clicked.connect(self.open_result)
        row.addWidget(self.btn_open)
        self.btn_usage = QPushButton("使用说明")
        self.btn_usage.clicked.connect(self.show_usage)
        row.addWidget(self.btn_usage)
        self.btn_about = QPushButton("关于")
        self.btn_about.clicked.connect(self.show_about)
        row.addWidget(self.btn_about)
        return row

    # ------------------------------------------------------------ 参数
    def options(self) -> ConvertOptions:
        """整份 PDF 的参数：页面排版与输出都读主窗体控件，修复读整批状态。

        一处显式合并，不靠任何预设偷偷补默认值。主窗体上没有单张的东西，
        单张覆盖只存在于 ImageInfo.tune，由 core.converter 在落纸前合并。
        """
        _name, per_page, layout = LAYOUT_CHOICES[self.layout_box.currentIndex()]
        v = self.batch_values
        crop, deskew, persp = v["crop"], v["deskew"], v["perspective"]
        return ConvertOptions(
            preset="general",
            paper=self.paper_box.currentText(),
            dpi=self.dpi_spin.value(),
            margin_mm=float(self.margin_spin.value()),
            per_page=per_page, layout=layout, gap_mm=float(self.gap_spin.value()),
            shadow=v["shadow"],
            geometry=bool(crop or deskew or persp), perspective=persp,
            crop=crop, deskew=deskew, max_angle=15.0,
            orientation=self.orient_box.currentData(),
            auto_landscape=self.orient_box.currentData() == "auto",
            color_mode=v["color_mode"],
            quality=self.quality_spin.value(),
            target_bytes=(self.target_mb.value() * 1048576
                          if self.use_target.isChecked() else None),
            floor_dpi=int(self.floor_box.currentData() or 150),
            page_numbers=self.page_num_check.isChecked(),
            page_number_pos=self.page_pos_box.currentData(),
            page_number_fmt=self.page_fmt_box.currentData(),
            page_number_start=self.page_start_spin.value(),
            page_number_size=float(self.page_size_spin.value()),
            footer_text=self.footer_edit.text(),
            captions=self.caption_check.isChecked(),
            caption_size=float(self.caption_size_spin.value()),
            bookmarks=self.bookmark_check.isChecked(),
        )

    def _after_params_changed(self) -> None:
        if self._syncing:
            return
        self._sync_paper_layout()
        self._refresh_buttons()
        self._refresh_sheet()

    def _on_params_changed(self) -> None:
        self._after_params_changed()

    def _sync_paper_layout(self) -> None:
        """自适应纸张只对一页一张成立，拼版被选时把它拉回一页一张。"""
        self._syncing = True
        try:
            if self.paper_box.currentText() == PAPER_AUTO and self.layout_box.currentIndex() != 0:
                self.layout_box.setCurrentIndex(0)
        finally:
            self._syncing = False
        # 自适应页面跟着单张图走，多张拼一页没有唯一比例：锁死拼版，别给错觉
        self.layout_box.setEnabled(self.paper_box.currentText() != PAPER_AUTO)
        self.gap_spin.setEnabled(self.paper_box.currentText() != PAPER_AUTO
                                 and self.layout_box.currentIndex() != 0)
        # 自适应纸张只是不让拖图，标注照样能加，所以按钮一直可用
        if self.sheet.btn_edit is not None:
            self.sheet.btn_edit.setEnabled(bool(self.files) and not self._running)

    def _sync_pager_enabled(self) -> None:
        """没勾页码时，位置 / 格式 / 起始 / 字号 都不该看起来能改。"""
        on = self.page_num_check.isChecked() and not self._running
        for w in (self.page_pos_box, self.page_fmt_box, self.page_start_spin,
                  self.page_size_spin):
            w.setEnabled(on)

    def _sync_caption_enabled(self) -> None:
        """没勾图注时，字号不该看起来能改；图注编辑按钮始终可用（它写的是数据）。"""
        self.caption_size_spin.setEnabled(
            self.caption_check.isChecked() and not self._running)

    def _set_params_enabled(self, enabled: bool) -> None:
        for w in (self.paper_box, self.dpi_spin, self.margin_spin,
                  self.layout_box, self.orient_box, self.quality_spin,
                  self.use_target, self.out_edit, self.btn_out,
                  self.page_num_check, self.footer_edit, self.gap_spin,
                  self.caption_check, self.bookmark_check, self.btn_caption):
            w.setEnabled(enabled)
        # 生成时不让翻页：预览每页也要跑一遍找纸 / 去阴影，会和出片抢 CPU
        self.sheet.setEnabled(enabled)
        self._sync_paper_layout()
        self._sync_target_enabled()
        self._sync_pager_enabled()
        self._sync_caption_enabled()

    def _sync_target_enabled(self) -> None:
        """体积上限没勾时，它和「最低画质」都不该像是能改的。"""
        on = self.use_target.isChecked()
        self.target_mb.setEnabled(on and not self._running)
        self.floor_box.setEnabled(on and not self._running)

    def _connect_params(self) -> None:
        for w in (self.paper_box, self.layout_box, self.orient_box,
                  self.floor_box, self.page_pos_box, self.page_fmt_box):
            w.currentIndexChanged.connect(self._on_params_changed)
        for w in (self.dpi_spin, self.margin_spin, self.quality_spin,
                  self.target_mb, self.page_start_spin, self.page_size_spin,
                  self.gap_spin):
            w.valueChanged.connect(self._on_params_changed)
        for w in (self.page_num_check, self.caption_check):
            w.toggled.connect(self._on_pager_toggled)
        self.bookmark_check.toggled.connect(self._on_params_changed)
        self.caption_size_spin.valueChanged.connect(self._on_params_changed)
        self.footer_edit.textChanged.connect(self._on_params_changed)
        self.use_target.toggled.connect(self._on_target_toggled)
        # 保存目录不影响画面，敲字时不必重排一次成品预览
        self.sheet.placeChanged.connect(self.on_place_changed)
        self.sheet.resetPage.connect(self.on_reset_page)
        self.sheet.annoAdded.connect(self.on_anno_added)
        self.sheet.annoUpdated.connect(self.on_anno_updated)
        self.sheet.annoRemoved.connect(self.on_anno_removed)
        self.sheet.editRequested.connect(self.open_page_editor)
        # 撤销栈由主窗体持有（它是标注和位置的唯一真源），预览只报「我要改了」
        self.sheet.willChange.connect(self.push_undo)
        self.sheet.undoRequested.connect(self.undo)
        self.sheet.redoRequested.connect(self.redo)

    def _on_pager_toggled(self, _checked: bool) -> None:
        # 页码和图注是两组控件，一起刷——反正它们都只是「开关 → 子项可用性」
        self._sync_pager_enabled()
        self._sync_caption_enabled()
        self._on_params_changed()

    def _on_target_toggled(self, _checked: bool) -> None:
        self._sync_target_enabled()
        self._on_params_changed()

    def pick_out_dir(self) -> None:
        here = self.out_edit.text().strip()
        start = here or (str(self.files[0].parent) if self.files else "")
        chosen = QFileDialog.getExistingDirectory(self, "选择 PDF 保存目录", start)
        if chosen:
            self.out_edit.setText(chosen)

    def _set_defaults(self) -> None:
        d = ConvertOptions()
        self.paper_box.setCurrentText(d.paper)
        self.dpi_spin.setValue(d.dpi)
        self.margin_spin.setValue(int(d.margin_mm))
        self.layout_box.setCurrentIndex(0)
        self.orient_box.setCurrentIndex(0)
        self.gap_spin.setValue(int(d.gap_mm))
        self.quality_spin.setValue(d.quality)
        self.use_target.setChecked(False)
        self.target_mb.setValue(20)
        self.floor_box.setCurrentIndex(0)
        self.page_num_check.setChecked(d.page_numbers)
        self.page_pos_box.setCurrentIndex(
            max(0, self.page_pos_box.findData(d.page_number_pos)))
        self.page_fmt_box.setCurrentIndex(
            max(0, self.page_fmt_box.findData(d.page_number_fmt)))
        self.page_start_spin.setValue(d.page_number_start)
        self.page_size_spin.setValue(int(d.page_number_size))
        self.footer_edit.setText(d.footer_text)
        self.caption_check.setChecked(d.captions)
        self.caption_size_spin.setValue(int(d.caption_size))
        self.bookmark_check.setChecked(d.bookmarks)
        self._sync_target_enabled()
        self._sync_pager_enabled()
        self._sync_caption_enabled()

    # ------------------------------------------------------------ 文件与列表
    def pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择图片", "", FILE_FILTER)
        if paths:
            self.add_files(paths)

    def pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if path:
            self.add_files([path])

    def add_files(self, raw) -> None:
        if self._running:
            return
        # Windows 路径大小写不敏感：IMG.JPG 与 img.jpg 是同一张，别加两遍
        existing = {os.path.normcase(str(p.resolve())) for p in self.files}
        skipped: list[str] = []
        added = 0
        for item in raw:
            found, bad = collect_images([str(item)], sort_by_exif=False)
            skipped.extend(bad)
            for info in found:
                key = str(info.path)
                dedup = os.path.normcase(str(info.path.resolve()))
                if dedup in existing:
                    continue
                existing.add(dedup)
                self.files.append(info.path)
                self._size_cache[key] = f"{info.width}×{info.height}"
                self._thumb_cache[key] = thumb_icon(info.path)
                added += 1
        if added:
            self._reset_undo()      # 换了图，旧快照里的位置和页号都对不上了
            self.rebuild_list()
        if skipped:
            self.statusBar().showMessage("部分文件没加进来：" + "；".join(skipped[:3]), 8000)
        elif raw and not added:
            # collect_images 对「既不是文件也不是目录」的路径（超长路径、
            # 无权限 UNC）静默返回空；以前这里一个提示都没有
            self.statusBar().showMessage(
                "没有加进来：路径不存在、无权限或不是支持的图片。", 8000)

    def dragEnterEvent(self, event):  # noqa: N802 — Qt 命名
        mime = event.mimeData()
        if mime.hasUrls() and any(u.isLocalFile() for u in mime.urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802 — Qt 命名
        if self._running:
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_files(paths)
            event.acceptProposedAction()
        else:
            self.statusBar().showMessage("拖入的不是本地文件。", 5000)

    def _icon(self, path: Path) -> QIcon:
        key = str(path)
        if key not in self._thumb_cache:
            self._thumb_cache[key] = thumb_icon(path)
        return self._thumb_cache[key]

    def _item_text(self, row: int) -> str:
        p = self.files[row]
        key = str(p)
        lines = [f"{row + 1}. {p.name}", self._size_cache.get(key, "—")]
        if key in self.tunes:
            lines.append("★ 单独修复")
        if key in self.places:
            lines.append("⇱ 手动排版")
        if key in self.captions:
            lines.append("✎ 有图注")
        if key in self.results:
            lines.append(self.results[key])
        return "\n".join(lines)

    def rebuild_list(self, select_row: int | None = None) -> None:
        row = self._sel_row if select_row is None else select_row
        self.thumb_list.blockSignals(True)
        self.thumb_list.clear()
        self._path_to_row.clear()
        for i, p in enumerate(self.files):
            key = str(p)
            self._path_to_row[key] = i
            item = QListWidgetItem(self._icon(p), self._item_text(i))
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setToolTip(f"{p}\n\n双击编辑这张；拖动改页序")
            item.setSizeHint(self.thumb_list.gridSize())
            self.thumb_list.addItem(item)
        row = max(0, min(row, len(self.files) - 1)) if self.files else 0
        if self.files:
            self.thumb_list.setCurrentRow(row)
        self.thumb_list.blockSignals(False)

        self.empty_hint.setVisible(not self.files)
        self.thumb_list.setVisible(bool(self.files))
        self._sync_progress()
        self._refresh_buttons()
        self._on_selection_changed()
        self._refresh_sheet()

    def _update_item_text(self, row: int) -> None:
        item = self.thumb_list.item(row)
        if item is not None and 0 <= row < len(self.files):
            item.setText(self._item_text(row))

    def selected_rows(self) -> list[int]:
        return sorted({self.thumb_list.row(i)
                       for i in self.thumb_list.selectedItems()})

    def _on_selection_changed(self) -> None:
        rows = self.selected_rows()
        if rows:
            self._sel_row = rows[0]
        if not self.files:
            self.sel_label.setText("还没有图片")
        elif rows:
            p = self.files[rows[0]]
            extra = ""
            if str(p) in self.tunes:
                extra += "　★ 这张有单独修复设置"
            if str(p) in self.places:
                extra += "　⇱ 手动摆过位置"
            if str(p) in self.captions:
                extra += "　✎ 有图注"
            self.sel_label.setText(f"已选中 {len(rows)} 张 · 第 {rows[0] + 1} 张："
                                   f"{p.name}{extra}")
            # 选中哪张，成品预览就跟到它所在那页：点完缩略图不用再自己翻页
            self.sheet.goto(self.sheet.page_of_row(rows[0]))
        else:
            self.sel_label.setText(f"共 {len(self.files)} 张 · 点一张缩略图选中")
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        has = bool(self.files)
        selected = bool(self.selected_rows())
        idle = not self._running
        self.btn_start.setEnabled(has and idle)
        self.btn_open.setEnabled(self.report is not None
                                 and self.report.pdf_path is not None)
        self.btn_cancel.setEnabled(self._running)
        self.btn_files.setEnabled(idle)
        self.btn_dir.setEnabled(idle)
        for btn in (self.btn_up, self.btn_down, self.btn_del, self.btn_edit):
            btn.setEnabled(has and idle and selected)
        # 自适应纸张也能开编辑窗口：那里只是拖不了图，标注照常能加
        self.sheet.btn_edit.setEnabled(has and idle)

    def _sync_progress(self) -> None:
        self.progress.setRange(0, max(1, len(self.files)))
        if not self._running:
            self.progress.setValue(0)

    def move_selected(self, delta: int) -> None:
        rows = self.selected_rows()
        if not rows or self._running:
            return
        if delta < 0 and rows[0] == 0:
            return
        if delta > 0 and rows[-1] == len(self.files) - 1:
            return
        order = list(range(len(self.files)))
        if delta < 0:
            for r in rows:
                order[r - 1], order[r] = order[r], order[r - 1]
        else:
            for r in reversed(rows):
                order[r + 1], order[r] = order[r], order[r + 1]
        self.files = [self.files[i] for i in order]
        self._reset_undo()          # 页序变了，撤销回去只会把人绕晕
        self.rebuild_list(select_row=max(0, min(rows[0] + delta, len(self.files) - 1)))

    def on_rows_moved(self, src: int, dst: int) -> None:
        if not (0 <= src < len(self.files) and 0 <= dst < len(self.files)):
            return
        moved = self.files.pop(src)
        self.files.insert(dst, moved)
        self._reset_undo()
        self.rebuild_list(select_row=dst)

    def remove_selected(self) -> None:
        rows = set(self.selected_rows())
        if not rows or self._running:
            return
        for r in rows:
            key = str(self.files[r])
            self.tunes.pop(key, None)
            self.places.pop(key, None)
            self.results.pop(key, None)
            self.captions.pop(key, None)
            self._size_cache.pop(key, None)
            self._thumb_cache.pop(key, None)
        self.files = [p for i, p in enumerate(self.files) if i not in rows]
        self._reset_undo()
        self.rebuild_list(select_row=min(rows))

    # ------------------------------------------------------------ 预览与编辑
    def make_edit_dialog(self) -> EditDialog:
        """造一个「编辑选中」窗口；单独留出来方便界面自检不进模态循环。"""
        rows = self.selected_rows()
        items, _skipped = self.current_items()
        index = 0
        if rows and items:
            target = str(self.files[rows[0]])
            index = next((i for i, it in enumerate(items) if str(it.path) == target), 0)
        return EditDialog(items, index, self.options(),
                          dict(self.batch_values), self)

    def edit_selected(self) -> None:
        """编辑选中的第一张：改修复参数，可选整批生效，顺手把图存进 out。"""
        if self._running or not self.selected_rows():
            return
        dlg = self.make_edit_dialog()
        if not dlg.items:
            QMessageBox.warning(self, APP_NAME,
                                "这些图片一张都打不开，没法编辑。请重新选一次文件。")
            return
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_edit(dlg)

    def make_caption_dialog(self) -> CaptionDialog:
        """造一个图注窗口；单独留出来方便界面自检不进模态循环。"""
        return CaptionDialog(self.files, self.captions, self)

    def edit_captions(self) -> None:
        """给整批图写图注：写的是「这张是什么」，和修图、排版都不相干。"""
        if self._running:
            return
        if not self.files:
            QMessageBox.information(self, APP_NAME, "先选几张图片，再给它们写图注。")
            return
        dlg = self.make_caption_dialog()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.captions = dlg.values()
        self._items_key = None
        self.rebuild_list(select_row=self._sel_row)
        if self.captions and not self.caption_check.isChecked():
            self.statusBar().showMessage(
                f"已写 {len(self.captions)} 条图注；勾上「图下方显示图注」才会印到版面上",
                8000)
        else:
            self.statusBar().showMessage(
                f"已写 {len(self.captions)} 条图注（留空的用文件名）", 6000)

    def _apply_edit(self, dlg: EditDialog) -> None:
        """把编辑窗口的结果落到主窗体状态：单张覆盖或整批修复、列表标记。

        PDF 输出（质量 / 体积 / 目录）不在编辑窗口里，所以这里不碰它们。
        """
        rows = self.selected_rows()
        row = rows[0] if rows else max(0, min(self._sel_row, len(self.files) - 1))
        key = str(self.files[row]) if self.files else ""
        repair = dlg.repair_values()
        if dlg.apply_to_all():
            self.batch_values = dict(repair)
            # 整批已按这组参数统一，留着单张覆盖会让「作用于其它图片」落空
            self.tunes.clear()
        else:
            if repair == self._batch_repair():
                self.tunes.pop(key, None)     # 与整批一致 = 跟随整批
            else:
                self.tunes[key] = dict(repair)

        self.results.pop(key, None)
        self.rebuild_list(select_row=row)
        if dlg.saved:
            dirs = {str(Path(r.dst).parent) for r in dlg.saved}
            where = next(iter(dirs)) if len(dirs) == 1 else f"{len(dirs)} 个 out 目录"
            self.statusBar().showMessage(
                f"已保存 {len(dlg.saved)} 张处理后的图到 {where}", 10000)

    def _batch_repair(self) -> dict:
        v = self.batch_values
        return {
            "geometry": bool(v["crop"] or v["deskew"] or v["perspective"]),
            "crop": bool(v["crop"]), "deskew": bool(v["deskew"]),
            "perspective": bool(v["perspective"]),
            "shadow": v["shadow"], "color_mode": v["color_mode"],
        }

    def current_items(self):
        """按列表顺序拿到 ImageInfo（文件读不出来就跳过），并挂上单张覆盖。

        出片和成品预览都从这里取，保证两者看到的张数、顺序、每张的修复参数
        完全一致。
        """
        if not self.files:
            return [], []
        items, skipped = collect_images([str(p) for p in self.files],
                                        sort_by_exif=False)
        by_path = {str(i.path): i for i in items}
        ordered = [by_path[str(p)] for p in self.files if str(p) in by_path]
        for info in ordered:
            tune = self.tunes.get(str(info.path))
            if tune:
                # 自由变换已取消：旧单张设置里残留的旋转 / 拉伸一律忽略，
                # 免得界面上看不见、出片却偷偷变形
                tune = {k: v for k, v in tune.items()
                        if k not in ("rotate", "scale_x", "scale_y")} or None
            info.tune = dict(tune) if tune else None
            place = self.places.get(str(info.path))
            info.place = dict(place) if place else None
            info.caption = self.captions.get(str(info.path), "")
        return ordered, skipped

    def preview_items(self):
        """成品预览用的 ImageInfo：只在清单 / 顺序 / 单独设置变了才重读。

        参数滑块拖一下就要重排一页，每次都去读一遍文件头会把界面拖慢。
        """
        key = (tuple(str(p) for p in self.files),
               tuple(sorted((k, repr(v)) for k, v in self.tunes.items())),
               tuple(sorted((k, repr(sorted(v.items()))) for k, v in self.places.items())),
               tuple(sorted(self.captions.items())))
        if key != self._items_key:
            items, _skipped = self.current_items()
            self._items_key = key
            self._items = items
        return self._items

    def _sheets(self) -> list:
        """所有还活着的预览：主窗体右栏那个 + （如果有）页面编辑窗口里那个。

        改参数、改位置之后两边都要重排，否则关掉编辑窗口会看到一份旧的排版。
        """
        out = []
        sheet = getattr(self, "sheet", None)
        if sheet is not None:
            out.append(sheet)
        editor = getattr(self, "_editor", None)
        if editor is not None:
            out.append(editor.sheet)
        return out

    def _refresh_sheet(self, *, keep_view: bool = False) -> None:
        """把「成品预览」对齐到当前清单与参数；运行中不抢 CPU。

        keep_view 用在刚手动挪完位置：画面里已经是新位置，清空只会闪一下白。
        """
        if not hasattr(self, "sheet") or self._running:
            return
        # 标注按页存，页数变了也要让预览知道：先灌数据再排页，画布一次就位
        for sheet in self._sheets():
            sheet.set_annotations(self.annotations)
            sheet.set_document(self.preview_items(), self.options(),
                               keep_view=keep_view)

    # ------------------------------------------------------------ 撤销 / 重做
    def _snapshot(self) -> dict:
        """当前这一版的可编辑状态：纸上所有标注 + 每张图被挪到的位置。

        存全量而不是存「动作」：一份快照就是几个小字典，几十条标注也只有几
        KB。反过来写一套 add/remove/move/style 的逆操作，漏一种就崩一次，
        而且「涂黑先挪后改色再裁文字」这种组合根本没法倒着推。
        """
        return {"annos": copy.deepcopy(self.annotations),
                "places": copy.deepcopy(self.places)}

    def push_undo(self, tag: str = "") -> None:
        """在改动发生**之前**调，把「改之前那一版」压进栈。

        tag 用来合并同类连续操作：一直点字号会让 valueChanged 连发十几次，
        每次都算一步的话，用户得按十几次撤销才回到原样。同一个 tag 在
        UNDO_COALESCE 秒内只压第一次。
        """
        now = time.monotonic()
        if tag and tag == self._undo_tag and now - self._undo_at <= self.UNDO_COALESCE:
            self._undo_at = now
            return
        self._undo.append(self._snapshot())
        # 新动作把「重做」这条路踩断了——标准的撤销语义
        self._redo.clear()
        self._undo_tag, self._undo_at = tag, now
        self._sync_undo()

    def undo(self) -> None:
        if not self._undo:
            return
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())
        self._announce("已撤销")

    def redo(self) -> None:
        if not self._redo:
            return
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())
        self._announce("已重做")

    def _restore(self, snap: dict) -> None:
        """把状态换回快照那一版，再让两边预览都跟上。"""
        places = copy.deepcopy(snap.get("places") or {})
        moved = places != self.places
        self.annotations = copy.deepcopy(snap.get("annos") or [])
        self.places = places
        self._items_key = None           # 位置变了，缓存的 items 作废
        for sheet in self._sheets():
            # restore 而不是 set：整份换过之后旧的选中下标可能指着别的一条
            sheet.restore_annotations(self.annotations)
        if moved:
            for row in range(len(self.files)):
                self._update_item_text(row)
            self._refresh_sheet(keep_view=True)
        if self._editor is not None:
            # 撤销也是改动，关窗时右栏得跟着刷一次
            self._editor.annotations_changed = True
        self._undo_tag = ""              # 别把下一步操作并进刚撤销的那一步
        self._sync_undo()

    def _sync_undo(self) -> None:
        can_undo, can_redo = bool(self._undo), bool(self._redo)
        for sheet in self._sheets():
            sheet.set_undo_state(can_undo, can_redo)

    def _announce(self, what: str) -> None:
        text = annotate.summary(self.annotations)
        self.statusBar().showMessage(
            f"{what} · 页面标注：{text}" if text else what, 4000)

    def _reset_undo(self) -> None:
        """换了一批图：旧快照里的位置是按文件路径记的，已经对不上了。"""
        self._undo.clear()
        self._redo.clear()
        self._undo_tag = ""
        self._sync_undo()

    # ------------------------------------------------------------ 页面标注
    def on_anno_added(self, anno) -> None:
        self.annotations.append(dict(anno))
        self.sheet.set_annotations(self.annotations)
        self._update_anno_hint()

    def on_anno_updated(self, index: int, anno) -> None:
        if 0 <= index < len(self.annotations):
            self.annotations[index] = dict(anno)
        self.sheet.set_annotations(self.annotations)
        self._update_anno_hint()

    def on_anno_removed(self, index: int) -> None:
        if 0 <= index < len(self.annotations):
            self.annotations.pop(index)
        self.sheet.set_annotations(self.annotations)
        self._update_anno_hint()

    def _update_anno_hint(self) -> None:
        """状态栏一句话说清现在有几处标注——不占版面，但随时能确认没白画。"""
        text = annotate.summary(self.annotations)
        if text:
            self.statusBar().showMessage(f"页面标注：{text}", 4000)

    # ------------------------------------------------------------ 手动排版
    def on_place_changed(self, row: int, rect) -> None:
        """成品预览里拖动 / 缩放了一张图：记下位置，再按真参数重排这一页。

        位置只存在内存里（这个工具没有工程文件），但会随 PDF 一起写进
        report.json，事后能查到每张图是按什么坐标落的。
        """
        if self._running or not (0 <= row < len(self.files)):
            return
        key = str(self.files[row])
        if not rect:
            if self.places.pop(key, None) is None:
                return
        else:
            self.places[key] = {k: round(float(v), 5) for k, v in dict(rect).items()}
        self._items_key = None      # 位置变了，预览要拿到带新位置的 ImageInfo
        self._update_item_text(row)
        self._refresh_sheet(keep_view=True)

    def on_reset_page(self, rows) -> None:
        """把这一页的手动位置全部清掉，退回按拼版自动落格。"""
        changed = 0
        for row in list(rows or []):
            if 0 <= row < len(self.files):
                key = str(self.files[row])
                if self.places.pop(key, None) is not None:
                    changed += 1
                    self._update_item_text(row)
        if changed:
            self._items_key = None
            self._refresh_sheet(keep_view=True)
            self.statusBar().showMessage(f"本页 {changed} 张已复位成自动排布", 5000)

    # ------------------------------------------------------------ 运行
    def start(self) -> None:
        if not self.files or self._running:
            return
        ordered, skipped = self.current_items()
        if not ordered:
            QMessageBox.warning(self, APP_NAME, "这些图片一张都打不开，请检查文件是否损坏。")
            return
        if skipped:
            self.statusBar().showMessage("已跳过：" + "；".join(skipped[:3]), 8000)

        self.results.clear()
        self.report = None
        self._running = True
        self._set_params_enabled(False)
        for row in range(len(self.files)):
            self._update_item_text(row)
        self.progress.setRange(0, len(ordered))
        self.progress.setValue(0)
        self._refresh_buttons()
        self.statusBar().showMessage("正在生成…")

        out_dir = self.out_edit.text().strip() or None
        self.worker = Worker(ordered, out_dir, self.options(), self,
                             annotations=self.annotations)
        self.worker.progressed.connect(self.on_progress)
        self.worker.finished_with.connect(self.on_finished)
        self.worker.start()

    def cancel(self) -> None:
        if self.worker is not None and self._running:
            self.worker.cancel()
            self.btn_cancel.setEnabled(False)
            self.statusBar().showMessage("正在取消…不会留下半截 PDF")

    def on_progress(self, done: int, total: int, item) -> None:
        self.progress.setValue(done)
        row = self._path_to_row.get(item.src)
        if row is None:
            return
        short = (f"✓ 已处理 {item.dpi:.0f}dpi" if item.status != "failed"
                 else "✗ 失败")
        self.results[item.src] = short
        self._update_item_text(row)

    def on_finished(self, report, net_summary: str) -> None:
        self._running = False
        self.worker = None
        self.report = report
        self._set_params_enabled(True)
        if self._closing:
            return
        self._refresh_buttons()
        if report is None:
            self.statusBar().showMessage(f"生成失败：{net_summary}")
            QMessageBox.critical(self, APP_NAME, f"生成失败：{net_summary}")
            return
        self.statusBar().showMessage(net_summary)
        for item in report.items:
            row = self._path_to_row.get(item.src)
            if row is None:
                continue
            self.results[item.src] = (f"✓ 已处理 {item.dpi:.0f}dpi"
                                     if item.status != "failed" else "✗ 失败")
            self._update_item_text(row)
        self.progress.setValue(self.progress.maximum())
        if report.cancelled:
            # 取消前 on_progress 已经把若干张写成 ✓，进度条也刚被置满；
            # 这里必须全部抹掉，否则看上去像「生成完了」却没有 PDF。
            self.results.clear()
            self.progress.setValue(0)
            self.rebuild_list()
            self.statusBar().showMessage("已取消：没有生成 PDF（原图未改动）。")
            QMessageBox.information(
                self, APP_NAME, "已取消：没有生成 PDF（原图未改动）。\n"
                "重新点「开始生成 PDF」会整批重做一遍。")
            return
        if report.pdf_path is None:
            QMessageBox.warning(self, APP_NAME, report.summary or "没有生成 PDF。")
            return
        self.open_result()
        QMessageBox.information(self, APP_NAME, report.summary)

    def open_result(self) -> None:
        if self.report is None or self.report.pdf_path is None:
            QMessageBox.information(self, APP_NAME, "还没有生成 PDF。")
            return
        folder = self.report.out_dir or self.report.pdf_path.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def show_about(self) -> None:
        AboutDialog(self).exec()

    def show_usage(self) -> None:
        UsageDialog(self).exec()

    def closeEvent(self, event):  # noqa: N802 — Qt 命名
        self._closing = True
        self.sheet.shutdown()
        # 非模态「页面编辑」窗口不会走 done()，它那条常驻 SheetRenderer
        # 必须在这里先停，否则子窗口随主窗口析构时线程还在跑 → 进程 abort
        editor = getattr(self, "_editor", None)
        if editor is not None:
            try:
                editor.sheet.shutdown()
            except (AttributeError, RuntimeError):
                pass
            editor.close()
            self._editor = None
        # 编辑窗口里若正在即时预览或存图，线程要一并收干净
        for dlg in self.findChildren(EditDialog):
            if dlg._save_worker is not None and dlg._save_worker.isRunning():
                dlg._save_worker.cancel()
                dlg._save_worker.wait(30000)
            dlg.view.shutdown()
            dlg.close()
        if self.worker is not None and self._running:
            try:
                self.worker.finished_with.disconnect(self.on_finished)
                self.worker.progressed.disconnect(self.on_progress)
            except (TypeError, RuntimeError):
                pass
            self.worker.cancel()
            self.worker.wait(30000)
            if self.worker.isRunning():
                # 30 秒还没收尾：托住线程，等 finished 再释放，绝不提前析构
                w = self.worker
                _ORPHAN_THREADS.add(w)
                def _release_batch(a=w):
                    _ORPHAN_THREADS.discard(a)
                    a.deleteLater()
                w.finished.connect(_release_batch)
            else:
                self.worker = None
            # 信号已断开，on_finished 不会再来复位状态；这里必须自己收干净，
            # 否则窗口关了但 _running 还挂着 True，后续任何引用都会读到假状态
            self._running = False
        super().closeEvent(event)


# ---------------------------------------------------------------- 出厂自检


def run_selftest(target: Path | None = None) -> int:
    """打包后现场造样张，走完 增强 → 编码 → 组装 → 复称体积。

    窗口版 exe 没有控制台，结果同时写日志文件。开发机上 import cv2 永远成功，
    但 PyInstaller 可能漏掉 DLL；不现场跑一遍，用户双击只会看到窗口一闪。
    """
    import shutil
    import tempfile

    import numpy as np

    from core.converter import convert_batch, effective_options, preview_page
    from core.presets import PAPER_AUTO, ConvertOptions

    lines: list[str] = []
    failed = 0

    def log(msg: str) -> None:
        lines.append(msg)
        try:
            print(msg, flush=True)
        except (OSError, ValueError):
            pass

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failed
        if not cond:
            failed += 1
        log(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"（{detail}）" if detail else ""))

    tmp = Path(tempfile.mkdtemp(prefix="i2p-selftest-"))
    try:
        import hashlib

        import pymupdf
        from PIL import Image as PILImage, ImageDraw

        from core import imaging
        from core.about import APP_VERSION
        from core.converter import ImageInfo

        log(f"{APP_NAME} {APP_VERSION} 打包自检")
        log(f"  可执行文件：{sys.executable}")
        log(f"  frozen     ：{getattr(sys, 'frozen', False)}")

        for label, fn in (("OpenCV", lambda: __import__("cv2")),
                          ("numpy", lambda: __import__("numpy")),
                          ("PyMuPDF", lambda: __import__("pymupdf")),
                          ("Pillow", lambda: __import__("PIL.Image"))):
            try:
                mod = fn()
                check(f"包内 {label} 可用", True, getattr(mod, "__version__", "ok"))
            except Exception as exc:  # noqa: BLE001
                check(f"包内 {label} 可用", False, f"{type(exc).__name__}: {exc}")

        heif_ok = imaging.ensure_heif()
        log(f"  HEIC 支持  ：{'是' if heif_ok else '否（会给出明确提示）'}")
        if heif_ok:
            try:
                heic_sample = tmp / "样张.heic"
                PILImage.new("RGB", (240, 320), (235, 236, 238)).save(heic_sample)
                got = imaging.open_image(heic_sample)
                check("HEIC 能写能读（解码链完整）", got.size == (240, 320),
                      f"{got.size[0]}×{got.size[1]}")
            except Exception as exc:  # noqa: BLE001
                check("HEIC 能写能读（解码链完整）", False,
                      f"{type(exc).__name__}: {exc}")

        # 扫描件四档滤镜：半明半暗的纸最容易暴露「窗口太小把暗侧笔画洗白」
        probe = np.full((800, 600, 3), 230, np.uint8)
        probe[:, 300:] = 150
        probe[200:210, 100:400] = 30
        try:
            from core.presets import COLOR_NAMES
            check("包内画面效果五档齐全",
                  list(COLOR_NAMES) == ["color", "scan", "enhance", "gray", "bw"],
                  str(list(COLOR_NAMES)))
            check("包内拍照修复档：暗图不白化（防呆）",
                  np.array_equal(
                      imaging.doc_filter(np.full((300, 400, 3), 80, np.uint8),
                                         "scan"),
                      imaging.doc_filter(np.full((300, 400, 3), 80, np.uint8),
                                         "enhance")))
            bw = imaging.doc_filter(probe, "bw")[..., 0]
            check("包内黑白滤镜是纯两级", set(np.unique(bw).tolist()) <= {0, 255},
                  str(np.unique(bw).tolist()[:6]))
            check("包内黑白滤镜暗侧笔画不丢",
                  bw[100, 100] == 255 and bw[205, 100] == 0
                  and bw[100, 350] == 255 and bw[205, 350] == 0)
            check("包内增强滤镜可用",
                  imaging.doc_filter(probe, "enhance").shape == probe.shape)
        except Exception as exc:  # noqa: BLE001
            check("包内扫描件滤镜可用", False, f"{type(exc).__name__}: {exc}")

        # 现场造一张带阴影的「票据」，不依赖任何外部素材
        im = PILImage.new("RGB", (900, 1270), (250, 250, 248))
        d = ImageDraw.Draw(im)
        d.rectangle((50, 60, 850, 150), fill=(40, 40, 40))
        for r in range(14):
            d.rectangle((50, 220 + r * 55, 650 - r * 25, 245 + r * 55), fill=(70, 70, 70))
        arr = np.array(im).astype(np.float32)
        grad = np.linspace(0.6, 1.0, arr.shape[1], dtype=np.float32)[None, :, None]
        sample = tmp / "样张.jpg"
        PILImage.fromarray(np.clip(arr * grad, 0, 255).astype(np.uint8)).save(
            sample, "JPEG", quality=92)

        before = hashlib.sha256(sample.read_bytes()).hexdigest()
        info = ImageInfo(path=sample, bytes=sample.stat().st_size,
                         width=900, height=1270)
        base_opt = ConvertOptions(dpi=150)
        with NetworkAudit() as audit:
            rep = convert_batch([info], out_dir=tmp / "out", options=base_opt)
        check("增强 + 编码 + 组装全程无异常", rep.pdf_path is not None,
              rep.summary or "无 summary")
        if rep.pdf_path is not None:
            check("产出 PDF 且体积 > 0", rep.pdf_bytes > 1024, f"{rep.pdf_bytes} 字节")
            check("页数为 1", rep.pages == 1, str(rep.pages))
            check("原图字节未变",
                  hashlib.sha256(sample.read_bytes()).hexdigest() == before)
            check("逐张报告写明了 DPI 与处理链",
                  bool(rep.items[0].dpi) and bool(rep.items[0].geometry),
                  f"{rep.items[0].dpi}dpi / {rep.items[0].geometry}")
            check("每个 PDF 都有同名 .report.json",
                  rep.pdf_path.with_suffix(".report.json").exists())
            doc = pymupdf.open(rep.pdf_path)
            rect = doc[0].rect
            doc.close()
            check("成品 PDF 能被重新打开，页面是 A4",
                  abs(rect.width - 595.28) < 1.5 and abs(rect.height - 841.89) < 1.5,
                  f"{rect.width:.0f}×{rect.height:.0f}pt")

        try:
            src, page, pv, (pw, ph, cell) = preview_page(info, base_opt)
            check("包内预览能算出落纸图且与成品同尺寸",
                  page.size == tuple(rep.items[0].out_px) and pv.dpi > 0,
                  f"{page.size[0]}×{page.size[1]} / {pv.dpi:.0f}dpi")
            check("包内预览的整页落位框在 A4 内",
                  0 <= cell[0] < cell[2] <= pw and 0 <= cell[1] < cell[3] <= ph,
                  f"page={pw:.0f}×{ph:.0f}pt")
        except Exception as exc:  # noqa: BLE001
            check("包内预览能算出落纸图且与成品同尺寸", False,
                  f"{type(exc).__name__}: {exc}")

        try:
            auto = ConvertOptions(dpi=150, paper=PAPER_AUTO, geometry=False,
                                  shadow="off", margin_mm=0)
            _s, _p, _r, (aw, ah, _c) = preview_page(info, auto)
            check("自适应纸张：页面比例跟着图片走",
                  abs(aw / ah - 900 / 1270) < 0.02,
                  f"{aw:.0f}×{ah:.0f}pt / {aw / ah:.3f}")
            norm = auto.normalized()
            check("自适应纸张强制一页一张",
                  auto.copy(per_page=4, layout="grid").normalized().per_page == 1
                  and norm.per_page == 1)
        except Exception as exc:  # noqa: BLE001
            check("自适应纸张：页面比例跟着图片走", False,
                  f"{type(exc).__name__}: {exc}")

        try:
            off = ConvertOptions(dpi=150, geometry=False, shadow="off").normalized()
            check("关掉总开关后三个细分项都关",
                  not (off.crop or off.deskew or off.perspective))
            only_crop = ConvertOptions(dpi=150, crop=True, deskew=False,
                                       perspective=False, shadow="off").normalized()
            check("可以只开裁边、关掉纠偏与透视",
                  only_crop.crop and not only_crop.deskew and not only_crop.perspective)
            res = imaging.enhance(imaging.open_image(sample),
                                  geometry=only_crop.geometry,
                                  perspective=only_crop.perspective,
                                  crop=only_crop.crop, deskew=only_crop.deskew,
                                  shadow="off")
            check("只开裁边时不会误报纠偏/透视",
                  not res.perspective and res.angle == 0.0, str(res.steps))
        except Exception as exc:  # noqa: BLE001
            check("可以只开裁边、关掉纠偏与透视", False,
                  f"{type(exc).__name__}: {exc}")

        # v1.3.0：「编辑这张」写下的逐张参数，打包版也必须能走通
        try:
            tuned = ImageInfo(path=sample, bytes=sample.stat().st_size,
                              width=900, height=1270,
                              tune={"shadow": "strong", "geometry": False})
            eff = effective_options(tuned, base_opt)
            check("逐张修复覆盖在打包版生效",
                  eff.shadow == "strong" and base_opt.shadow == "off"
                  and not eff.geometry)
            _s, _p, pv2, _box = preview_page(tuned, base_opt)
            check("逐张修复覆盖能走进预览计算", pv2.shadow == "强", str(pv2.shadow))
        except Exception as exc:  # noqa: BLE001
            check("逐张修复覆盖在打包版生效", False,
                  f"{type(exc).__name__}: {exc}")

        check("全程 0 次联网", audit.attempts == 0, str(audit.attempts))
        log(f"  输出      ：{getattr(rep, 'pdf_path', None)}")
    except Exception as exc:  # noqa: BLE001 — 自检自己也不能把进程带崩
        check("自检过程未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return _write_selftest_log(lines, target, failed)


def _write_selftest_log(lines: list[str], out: Path | None, failed: int) -> int:
    lines.append("")
    lines.append("结论：" + ("全部通过" if failed == 0 else f"{failed} 项未通过"))
    text = "\n".join(lines)
    target = Path(out) if out else Path.home() / "图片转PDF工具-selftest.log"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError:
        pass
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass
    return 0 if failed == 0 else 1


def flush_streams() -> None:
    """退出前把缓冲输出落盘，窗口版 exe（console=False）下必须能安全调用。

    PyInstaller 的 windowed 模式没有控制台，sys.stdout / sys.stderr 是 None；
    解释器收尾时流也可能已关闭。直接 .flush() 会抛
    「'NoneType' object has no attribute 'flush'」，被 bootloader 弹成
    「Unhandled exception in script」——关窗口时才出现，伪装成崩溃。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.flush()
        except (OSError, ValueError, AttributeError):
            pass


def main() -> int:
    set_windows_app_id()
    app = QApplication(sys.argv)
    configure_app(app)
    win = MainWindow()
    win.show()
    rc = app.exec()
    # 显式清场再硬退出：解释器收尾时 GC 销毁 Qt 对象的顺序不可控，一旦
    # widget 晚于 QApplication 被析构就触发 fail-fast（0xC0000409，实测
    # 整进程静默崩溃）。此时事件循环已结束、该保存的都已落盘，跳过
    # Python 终结阶段是安全的。
    win.deleteLater()
    del win
    app.processEvents()
    del app
    gc.collect()
    flush_streams()
    os._exit(rc)
    return rc  # 不可达；保留返回值签名给调用方


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--selftest" in argv:
        i = argv.index("--selftest")
        target = None
        if len(argv) > i + 1 and not argv[i + 1].startswith("-"):
            target = Path(argv[i + 1])
        sys.exit(run_selftest(target))
    if "--version" in argv or "-V" in argv:
        # 窗口版 exe 没有控制台，print 用户看不到；同时把版本落到用户目录。
        from core.about import VERSION_TEXT

        try:
            print(VERSION_TEXT, flush=True)
        except (OSError, ValueError):
            pass
        try:
            (Path.home() / "图片转PDF工具-版本.txt").write_text(
                VERSION_TEXT, encoding="utf-8")
        except OSError:
            pass
        sys.exit(0)
    unknown = [a for a in argv if a.startswith("--")
               and a not in ("--selftest", "--version")]
    if unknown:
        # 不认识的参数以前会被忽略、悄悄开个空窗口挂在那里；宁可明确报错退出。
        try:
            print("不认识的命令行参数：" + " ".join(unknown) + "\n"
                  "可用参数：--selftest [日志路径] / --version", flush=True)
        except (OSError, ValueError):
            pass
        sys.exit(2)
    sys.exit(main())
