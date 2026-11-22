"""命令行入口：不装 PySide6 也能完整使用、验证核心能力。

v1.2.0 起默认就是「通用」，与界面一致；--preset 只是把一组推荐值填进来。

用法：
  python cli.py 图片或文件夹                       # 通用默认，结果在第一张图旁的 out
  python cli.py ./票据 --preset receipt --target 20   # 一键填票据值 + 整批压到 20MB
  python cli.py ./证件 --preset idcard             # 300dpi、正反面左右并排
  python cli.py ./书页 --paper 自适应               # 页面跟着图片比例走
  python cli.py ./作业 --color bw --no-perspective  # 黑白 1 位，只关透视拉正
  python cli.py ./书页 --color enhance --shadow light  # 扫描件增强 + 轻度去阴影
  python cli.py ./票据 --per-page 4 --shadow light
  python cli.py --version                          # 版本、公司、有损口径
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.about import VERSION_TEXT
from core.compliance import NetworkAudit
from core.converter import collect_images, convert_batch
from core.presets import COLOR_NAMES, PRESETS, ConvertOptions


def _mb(n: int) -> str:
    return f"{n / 1048576:.2f} MB"


def _parse_layout(text: str):
    table = {
        "1": (1, "single"), "single": (1, "single"),
        "2": (2, "col"), "col": (2, "col"), "v": (2, "col"),
        "row": (2, "row"), "h": (2, "row"),
        "4": (4, "grid"), "grid": (4, "grid"),
    }
    if text.lower() not in table:
        raise argparse.ArgumentTypeError("拼版只能是 1 / 2 / 4 / col / row / grid")
    return table[text.lower()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="图片转PDF工具",
        description="把照片、截图、扫描图拼成一份能直接发出去的 PDF（全程本地，不联网）")
    p.add_argument("inputs", nargs="*", help="图片文件或文件夹（可多个）")
    p.add_argument("--preset", choices=list(PRESETS), default="general",
                   help="一键填推荐值（默认 general 通用）：receipt 票据 / "
                        "idcard 证件 / homework 文档；填完仍可用下面的参数覆盖")
    p.add_argument("--target", type=float, metavar="MB",
                   help="目标体积上限（MB）；程序按实测体积自动降 DPI 与质量")
    p.add_argument("--dpi", type=int, help="覆盖预设的 DPI")
    p.add_argument("--paper", help="纸张：A4 / A5 / B5 / Letter / 自适应")
    p.add_argument("--margin", type=float, metavar="MM", help="页边距（毫米）")
    p.add_argument("--per-page", type=_parse_layout, metavar="N",
                   help="一页几张：1 / 2 / 4，也可写 col（上下）、row（左右）、grid")
    p.add_argument("--shadow", choices=["off", "light", "standard", "strong"],
                   help="去阴影强度")
    p.add_argument("--color", choices=list(COLOR_NAMES),
                   help="画面效果：color 彩色 / scan 彩色扫描件（拍照修复）/ "
                        "enhance 增强扫描件 / gray 灰度 / bw 黑白 1 位无损")
    p.add_argument("--quality", type=int, help="JPEG 质量 40-95")
    p.add_argument("--floor", type=int, choices=[150, 96],
                   help="最低画质地板：150 打印可用 / 96 只给人看")
    p.add_argument("--no-geometry", action="store_true", help="一次关掉裁边 / 纠偏 / 透视拉正")
    p.add_argument("--no-crop", action="store_true", help="只关裁边（其余矫正保留）")
    p.add_argument("--no-deskew", action="store_true", help="只关纠偏（其余矫正保留）")
    p.add_argument("--no-perspective", action="store_true", help="只关透视拉正（其余矫正保留）")
    p.add_argument("--max-angle", type=float, metavar="DEG",
                   help="纠偏最大允许角度（0.5-30，默认 15）")
    p.add_argument("--orientation", choices=["auto", "portrait", "landscape"],
                   help="页面方向：auto 自动 / portrait 始终竖版 / landscape 始终横版")
    p.add_argument("--no-landscape", action="store_true",
                   help="横图不自动转横版（等价于 orientation=auto 时关掉自动横版）")
    p.add_argument("--out", help="PDF 输出目录（默认第一张图旁边的 out）")
    p.add_argument("--name", help="PDF 文件名（默认 图片转PDF-日期-时间.pdf）")
    p.add_argument("--json", action="store_true", help="把完整报告以 JSON 打到标准输出")
    p.add_argument("--version", action="version", version=VERSION_TEXT)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.inputs:
        build_parser().print_help()
        return 2

    items, skipped = collect_images(args.inputs, sort_by_exif=True)
    for s in skipped:
        print(f"[跳过] {s}")
    if not items:
        print("没有可转换的图片。")
        return 1

    opt = ConvertOptions.from_preset(args.preset)
    overrides = {}
    if args.dpi:
        overrides["dpi"] = args.dpi
    if args.paper:
        overrides["paper"] = args.paper
    if args.margin is not None:
        overrides["margin_mm"] = args.margin
    if args.per_page:
        overrides["per_page"], overrides["layout"] = args.per_page
    if args.shadow:
        overrides["shadow"] = args.shadow
    if args.color:
        overrides["color_mode"] = args.color
    if args.quality:
        overrides["quality"] = args.quality
    if args.floor:
        overrides["floor_dpi"] = args.floor
    if args.no_geometry:
        overrides["geometry"] = False
    if args.no_crop:
        overrides["crop"] = False
    if args.no_deskew:
        overrides["deskew"] = False
    if args.no_perspective:
        overrides["perspective"] = False
    if args.max_angle is not None:
        overrides["max_angle"] = args.max_angle
    if args.orientation:
        overrides["orientation"] = args.orientation
    if args.no_landscape:
        overrides["auto_landscape"] = False
    if args.target:
        overrides["target_bytes"] = int(args.target * 1048576)
    if overrides:
        opt = ConvertOptions.from_preset(args.preset, **overrides)

    opt = opt.normalized()   # 回显实际生效值：--dpi 9999 会被钳到 600，别骗人
    print(f"{len(items)} 张图片 → {opt.paper} {opt.dpi}dpi，"
          f"{'一页' + str(opt.per_page) + '张' if opt.per_page > 1 else '一页一张'}，"
          f"去阴影 {opt.shadow}，{opt.color_mode}")
    if opt.target_bytes:
        print(f"目标体积 ≤ {_mb(opt.target_bytes)}")

    def on_progress(done, total, item):
        name = Path(item.src).name
        flag = "OK " if item.status == "ok" else "ERR"
        extra = f"{item.dpi:.0f}dpi {item.geometry}"
        if item.note:
            extra += f" · {item.note}"
        print(f"  [{done}/{total}] {flag} {name} → {item.out_px[0]}×{item.out_px[1]} {extra}",
              flush=True)

    with NetworkAudit() as audit:
        report = convert_batch(items, out_dir=args.out, options=opt,
                               progress=on_progress, pdf_name=args.name,
                               skipped=skipped, network_audit=audit)
        report.network_attempts = audit.attempts
        summary = audit.summary()

    if args.json:
        print(json.dumps({
            "pdf": str(report.pdf_path) if report.pdf_path else None,
            "bytes": report.pdf_bytes,
            "pages": report.pages,
            "attempts": report.attempts,
            "summary": report.summary,
            "network_attempts": report.network_attempts,
            "items": [
                {"src": i.src, "src_px": i.src_px, "out_px": i.out_px, "dpi": i.dpi,
                 "geometry": i.geometry, "shadow": i.shadow, "status": i.status,
                 "note": i.note, "ms": i.ms}
                for i in report.items
            ],
        }, ensure_ascii=False, indent=2))
    else:
        print()
        print(report.summary)
        if report.pdf_path:
            print(f"PDF：{report.pdf_path}")
            print(f"报告：{report.pdf_path.parent / 'report.json'}")
        print(summary)
    if report.cancelled:
        return 1
    return 0 if report.pdf_path else 1


if __name__ == "__main__":
    sys.exit(main())
