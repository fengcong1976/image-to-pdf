# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本（onedir）。

用法：pyinstaller --noconfirm --clean "build.spec"（或直接双击 build_exe.bat）
产物：dist\\图片转PDF工具\\图片转PDF工具.exe（整个文件夹拷走即可免安装运行）

与「图片文件压缩」那份 spec 的区别，全部围绕 OpenCV：
1. **必须保留 numpy / cv2**：图像压缩那份把 numpy 排除掉了，这里 numpy 是
   核心依赖（OpenCV 与去阴影都靠它），排除掉 exe 会直接起不来。
2. **排除 opencv_videoio_ffmpeg*.dll**：本项目只处理静态图、不解视频，
   这个 DLL 实测 29.4MB；它只被 cv2.VideoCapture 用到，静态图链路一行不碰。
3. 去掉 Qt 自带翻译与 opengl32sw.dll：界面文案全是自写中文、没装 QTranslator；
   QWidget 走光栅渲染，软件 OpenGL 兜底用不上。
4. 不启用 UPX：压缩版 exe 容易被杀软误报，宁可体积大一点。
"""

import re
from pathlib import Path

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('resources', 'resources')],  # 图标：程序内 app_icon() 按相对路径找
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'pydoc', 'doctest', 'pdb',
        'yaml', 'psutil', 'charset_normalizer',
        'requests', 'urllib3', 'setuptools', 'pkg_resources',
        'pytest', 'IPython', 'matplotlib', 'pandas', 'scipy',
        # pymupdf 2.0 自带版面分析（onnxruntime + 模型 49MB），本项目只做
        # 「插图 + 存 PDF」，一行都用不到，排除后省 87MB。
        'onnxruntime', 'pymupdf_layout', 'pymupdf4llm', 'pymupdf.mupdf_devel',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# 视频编解码 DLL 用不上：静态图不碰 VideoCapture，省 29MB。
_VIDEOIO = re.compile(r'^opencv_videoio_ffmpeg.*\.dll$', re.IGNORECASE)
a.binaries = [b for b in a.binaries if not _VIDEOIO.match(Path(b[0]).name)]

# 版面分析那套东西（onnxruntime 38MB + pymupdf/layout 49MB）不属于本项目的功能，
# 但 pymupdf 的 hook 会把它们整份拖进来；mupdf-devel 是编译用的头文件与静态库。
_DROP = ('onnxruntime', 'pymupdf/layout', 'pymupdf\\layout', 'mupdf-devel',
         'pymupdf4llm', 'pymupdf_layout')


def _keep(entry) -> bool:
    name = str(entry[0]).replace('\\', '/').lower()
    return not any(k in name for k in _DROP)


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]
a.binaries = [b for b in a.binaries if 'opengl32sw' not in b[0].lower()]
a.datas = [d for d in a.datas
           if 'translations' not in d[0].replace('\\', '/').lower()
           and 'qt6/qml' not in d[0].replace('\\', '/').lower()]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='图片转PDF工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 窗口程序：不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='tools/pyinstaller_version.txt',
    icon=['resources/app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='图片转PDF工具',
)
