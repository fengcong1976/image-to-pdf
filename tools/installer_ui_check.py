"""用真实安装向导走一遍：把安装、运行、卸载整条链验完。

不静默：静默安装看不到完成页，「安装完不出现查阅 README 勾选」这条要求就没法验。
步骤：
  1. 启动安装包 → 点「下一步」/「安装」走到完成页 → 枚举控件文字并截图，断言无 readme 项；
  2. 点「完成」，确认装到的目录里 exe 与 _internal 都在；
  3. 用装好的 exe 跑一次 --selftest，证明这份安装能真跑（不是只有文件）；
  4. 再真启动它、正常关窗口，断言没有「Unhandled exception in script」弹窗
     （旧流程用 taskkill 强杀，测不到退出路径上的 flush 崩溃）；
  5. 静默卸载，断言安装目录已清空、开始菜单快捷方式不残留。

跑法：python tools/installer_ui_check.py "dist\\安装包\\图片转PDF工具-1.3.0-安装包.exe"
产物：_ui_shots/installer-01/02/03-*.png
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

user32 = ctypes.windll.user32
ROOT = Path(__file__).resolve().parents[1]
SHOT = ROOT / "_ui_shots"
SHOT.mkdir(exist_ok=True)

APP = "图片转PDF工具"
VK_RETURN = 0x0D
BM_CLICK = 0x00F5


def local_appdata() -> Path:
    """取 shell 认定的 LOCALAPPDATA，而不是环境变量。

    Inno Setup 的 {autopf} 走 Windows 已知文件夹；某些沙箱 / 重定向环境里
    os.environ["LOCALAPPDATA"] 会指向另一个位置（实测 C:\\Users\\... 与
    D:\\Users\\... 不一致），照环境变量推断安装目录会把「装好了」误判成失败。
    """
    try:
        buf = ctypes.create_unicode_buffer(260)
        rc = ctypes.windll.shell32.SHGetFolderPathW(None, 0x001C, None, 0, buf)
        if rc == 0 and buf.value:
            return Path(buf.value)
    except (AttributeError, OSError):
        pass
    return Path(os.environ["LOCALAPPDATA"])


INSTALL_DIR = local_appdata() / "Programs" / APP

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"（{detail}）" if detail else ""))
    if not cond:
        fails.append(name)


def top_windows(title_part: str) -> list[int]:
    """找标题里带关键字的顶层窗口（Inno 向导的类是 TWizardForm）。"""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title_part in buf.value:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


def children_text(hwnd: int, visible_only: bool = True) -> list[tuple[str, str]]:
    """列出子控件的 (类名, 文字)。只取可见的：向导用隐藏控件占位，
    不筛掉的话「下一步」在完成页也还在列表里，就永远判断不出完成。"""
    out: list[tuple[str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        if visible_only and not user32.IsWindowVisible(h):
            return True
        cn = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, cn, 64)
        n = user32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        text = buf.value.strip()
        if text:
            out.append((cn.value, text))
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return out


def find_button(hwnd: int, keywords: tuple[str, ...]) -> int:
    hit = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        if not user32.IsWindowVisible(h):
            return True
        cn = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, cn, 64)
        if cn.value != "TNewButton":
            return True
        n = user32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        if any(k in buf.value for k in keywords):
            hit.append(h)
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return hit[0] if hit else 0


def rect_of(hwnd: int):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def error_dialog() -> str:
    """找安装过程中的报错弹窗文字。

    安装失败时弹的是标题「安装」的独立对话框，标题里没有软件名，
    `top_windows(APP)` 找不到它，旧版脚本会一直点到超时才报错。
    典型例子：错误 448「无法遍历路径，因为它包含不受信任的装入点」。
    """
    hits: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value.strip() not in ("安装", "安装程序", "错误"):
            return True
        blob = " ".join(t for _c, t in children_text(hwnd))
        if "错误" in blob or "无法创建目录" in blob or "448" in blob:
            hits.append(blob)
        return True

    user32.EnumWindows(cb, 0)
    return hits[0] if hits else ""


def walk_wizard(setup: Path) -> int:
    """走完向导并停在完成页，返回窗口句柄；失败返回 0。"""
    print(f"启动安装向导：{setup.name}")
    # /DIR 固定目标目录：不受「记住上次安装路径」影响，测试才可重复。
    proc = subprocess.Popen([str(setup), "/DIR=" + str(INSTALL_DIR)])
    time.sleep(2.0)

    hwnd = 0
    for _ in range(40):
        wins = top_windows(APP)
        if wins:
            hwnd = wins[0]
            break
        time.sleep(0.25)
    if not hwnd:
        print("FAIL 没找到安装向导窗口")
        fails.append("向导窗口")
        return 0
    print(f"窗口句柄 {hwnd}，标题 ok")

    shots = 0
    for _ in range(180):  # 180 × 1.5s ≈ 4.5 分钟，够装完 255MB
        err = error_dialog()
        if err:
            print(f"FAIL 安装弹出错误：{err[:220]}")
            fails.append("安装报错弹窗")
            subprocess.run(["taskkill", "/IM", setup.name, "/F"], capture_output=True)
            return 0
        if not user32.IsWindow(hwnd):
            print("FAIL 安装向导提前消失")
            fails.append("向导提前消失")
            return 0
        if find_button(hwnd, ("完成",)):
            return hwnd
        if shots < 3:
            ImageGrab.grab(bbox=rect_of(hwnd)).save(
                SHOT / f"installer-01-第{shots + 1}页.png")
            shots += 1
        next_btn = find_button(hwnd, ("下一步", "安装(&I)", "安装"))
        if next_btn:
            user32.SendMessageW(next_btn, BM_CLICK, 0, 0)
        time.sleep(1.5)

    print("FAIL 没走到完成页（可能卡在某一页）")
    fails.append("完成页")
    return 0


def close_without_crash(exe: Path) -> tuple[bool, str]:
    """真启动装好的 exe、正常关窗口，确认不是以「Unhandled exception」收场。

    窗口版 exe（console=False）没有控制台，退出时对 None 调 flush 会被
    PyInstaller 弹成错误对话框并把进程挂住。旧流程用 taskkill 强杀主程序，
    永远走不到那段退出代码，这个 bug 就漏过去了——必须真关一次窗口。
    """
    before = set(top_windows(APP))
    proc = subprocess.Popen([str(exe)])
    hwnd = 0
    for _ in range(60):
        hits = [h for h in top_windows(APP) if h not in before]
        if hits:
            hwnd = hits[0]
            break
        time.sleep(0.5)
    if not hwnd:
        proc.kill()
        return False, "没找到主窗口"
    time.sleep(1.5)
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    try:
        rc = proc.wait(timeout=18)
    except subprocess.TimeoutExpired:
        rc = None
    errs = top_windows("Unhandled exception")
    for h in errs:
        user32.PostMessageW(h, 0x0010, 0, 0)
    if proc.poll() is None:
        proc.kill()
    return rc == 0 and not errs, f"退出码 {rc} / 错误弹窗 {len(errs)} 个"


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python tools/installer_ui_check.py <安装包路径>")
        return 2
    setup = Path(sys.argv[1]).resolve()
    if not setup.is_file():
        print(f"找不到安装包：{setup}")
        return 2

    hwnd = walk_wizard(setup)
    if not hwnd:
        return 1

    time.sleep(0.8)
    texts = children_text(hwnd)
    print("\n完成页控件：")
    for cn, t in texts:
        print(f"  [{cn}] {t}")
    ImageGrab.grab(bbox=rect_of(hwnd)).save(SHOT / "installer-02-完成页.png")

    blob = " ".join(t for _, t in texts).lower()
    bad = [k for k in ("readme", "查阅", "自述") if k in blob]
    check("完成页没有「查阅 README」勾选项", not bad, str(bad) if bad else "")

    # 点「完成」；[Run] 里默认勾了「立即运行」，主界面会被带起来，稍后统一收掉
    btn = find_button(hwnd, ("完成",))
    if btn:
        user32.SendMessageW(btn, BM_CLICK, 0, 0)
    time.sleep(4.0)
    subprocess.run(["taskkill", "/IM", f"{APP}.exe", "/F"], capture_output=True)

    exe = INSTALL_DIR / f"{APP}.exe"
    check("装到了每用户程序目录（shell 认定的 LOCALAPPDATA\\Programs）",
          exe.is_file(), str(INSTALL_DIR))
    check("_internal 一并装上（免装运行库）",
          (INSTALL_DIR / "_internal").is_dir() and
          (INSTALL_DIR / "_internal" / "python313.dll").is_file(),
          "_internal/python313.dll")
    check("开始菜单快捷方式已建",
          any(Path(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu",
                   "Programs", APP).glob("*.lnk")),
          "开始菜单")

    # 装好的这份能不能真跑：跑一次打包自检
    if exe.is_file():
        log = ROOT / "_installed_selftest.log"
        subprocess.run([str(exe), "--selftest", str(log)],
                       capture_output=True, timeout=180)
        body = ""
        if log.is_file():
            raw = log.read_bytes()
            for enc in ("utf-8", "gbk", "utf-16"):
                try:
                    body = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
        check("装好的 exe 自检全过", "结论：全部通过" in body,
              "看 _installed_selftest.log" if body else "无日志")
        ok, detail = close_without_crash(exe)
        check("装好的 exe 正常关窗口不弹「Unhandled exception」", ok, detail)

    # 卸载：静默跑，断言目录清空
    unins = INSTALL_DIR / "unins000.exe"
    if unins.is_file():
        subprocess.run([str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                       capture_output=True, timeout=180)
        time.sleep(3.0)
    check("卸载后安装目录已清空", not exe.exists(), str(exe))
    leftovers = [p.name for p in INSTALL_DIR.glob("*")] if INSTALL_DIR.exists() else []
    check("卸载后不留残文件", not leftovers, str(leftovers[:5]))
    start_menu = Path(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu",
                      "Programs", APP)
    check("开始菜单目录已移除", not start_menu.exists(), str(start_menu))

    print(f"\n合计 {len(fails)} 项未通过" if fails else "\n结论：全部通过")
    print(f"截图：{SHOT / 'installer-02-完成页.png'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
