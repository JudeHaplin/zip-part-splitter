# -*- coding: utf-8 -*-
"""
zippack_core —— 大文件夹按体积上限顺序分包压缩的核心逻辑（纯标准库，可单独当 CLI 用）

设计要点
  * 顺序：默认按名称前面的数字做“自然排序”（1、2、10 → 1,2,10 而不是 1,10,2）
  * 切分：默认“子文件夹不拆分”，一个子文件夹整体进同一个包；只有单个子文件夹本身就超
          过上限时才独占一包，保证照片不被切碎
  * 散落文件：直接躺在源文件夹最外层、不属于任何子文件夹的文件（比如报价表 xlsx），
          默认并入第一部分（可改为单独打包 / 忽略）
  * 压缩：jpg/png/mp4 等已压缩格式走“仅存储”，比强压缩快几十倍且体积几乎不变；
          文档类走 deflate
"""
from __future__ import annotations

import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Iterable

MB = 1024 * 1024

# 已经被压缩过的格式：再压没收益，直接存储，速度快很多
ALREADY_COMPRESSED = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".jfif",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v", ".3gp",
    ".mp3", ".aac", ".m4a", ".wav", ".flac", ".ogg", ".amr",
    ".zip", ".rar", ".7z", ".gz", ".bz2", ".xz",
    ".docx", ".xlsx", ".pptx", ".pdf", ".apk", ".jar",
}

SORT_MODES = {
    "auto": "按序号前缀自然排序（推荐）",
    "name": "按名称排序",
    "mtime": "按修改时间排序",
    "none": "不排序（按系统返回顺序）",
}

SPLIT_MODES = {
    "dir": "子文件夹不拆分（推荐）",
    "file": "严格按体积拆分（可能切碎文件夹）",
}

LOOSE_MODES = {
    "first": "并入第一部分",
    "separate": "单独打一个包",
    "skip": "忽略不打包",
}

COMPRESS_MODES = {
    "auto": "自动（图片视频仅存储，文档压缩）",
    "store": "仅存储（最快，体积≈原大小）",
    "normal": "标准压缩",
    "max": "最大压缩（最慢）",
}


# ----------------------------------------------------------------------------- 工具函数
def natural_key(name: str):
    """'10、xx' 排在 '9、xx' 之后。数字段按数值比较，其余按小写字符串。"""
    parts = re.split(r"(\d+)", name)
    key = []
    for i, p in enumerate(parts):
        if i % 2 == 1:
            key.append((0, int(p), ""))
        elif p:
            key.append((1, 0, p.lower()))
    return key


def leading_number(name: str):
    m = re.match(r"\s*(\d+)", name)
    return int(m.group(1)) if m else None


def dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit in ("B", "KB") else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "包"


# ----------------------------------------------------------------------------- 数据结构
@dataclass
class Options:
    src: str = ""
    out: str = ""
    limit_mb: float = 900.0
    sort_mode: str = "auto"
    split_mode: str = "dir"
    loose_mode: str = "first"
    label: str = "部分"              # 部分 / 批 / 卷 / PART
    prefix: str = ""                 # 默认取源文件夹名
    include_range: bool = True       # 文件名里带 01-22 这样的序号范围
    compress: str = "auto"
    include_hidden: bool = False
    keep_empty_dirs: bool = True
    make_manifest: bool = True       # 输出目录生成总清单 txt
    manifest_inside: bool = False    # 每个包里再放一份本包清单
    overwrite: bool = True
    open_when_done: bool = False

    @property
    def limit_bytes(self) -> int:
        return int(self.limit_mb * MB)

    @property
    def eff_prefix(self) -> str:
        if self.prefix.strip():
            return self.prefix.strip()
        return os.path.basename(os.path.normpath(self.src)) or "分包"


@dataclass
class Entry:
    """一个待打包的文件（rel 为 zip 内相对路径，用 / 分隔）"""
    rel: str
    path: str
    size: int
    group: str = ""      # 所属子文件夹名（散落文件为空）


@dataclass
class Unit:
    """切分的最小单位：默认是一个子文件夹"""
    name: str
    path: str
    size: int
    num: int | None = None
    is_loose: bool = False
    entries: list = field(default_factory=list)
    empty_dirs: list = field(default_factory=list)

    @property
    def sort_key(self):
        return self.num if self.num is not None else 10 ** 9


@dataclass
class Part:
    index: int
    units: list
    entries: list
    size: int
    filename: str = ""
    extra_dirs: list = field(default_factory=list)

    @property
    def counted_units(self) -> list:
        """真正带了文件的单位（空文件夹不算，避免序号范围写错）"""
        return [u for u in self.units if u.entries]

    @property
    def range_text(self) -> str:
        nums = [u.num for u in self.counted_units if u.num is not None]
        if not nums:
            return "%02d" % self.index
        return "%02d-%02d" % (min(nums), max(nums))


@dataclass
class BuildResult:
    ok: bool
    message: str = ""
    parts: list = field(default_factory=list)   # [(filename, files, bytes)]
    skipped: list = field(default_factory=list)
    manifest: str = ""


# ----------------------------------------------------------------------------- 扫描
def scan(opts: Options,
         progress: Callable[[str], None] | None = None) -> tuple[list, list, int]:
    """返回 (子文件夹单位, 散落文件条目, 总字节数)"""
    src = opts.src
    say = progress or (lambda *_: None)
    subdirs, loose = [], []
    total = 0

    names = sorted(os.listdir(src))
    for name in names:
        full = os.path.join(src, name)
        if os.path.isdir(full):
            if not opts.include_hidden and name.startswith("."):
                continue
            say("扫描 %s" % name)
            size = 0
            nfile = 0
            unit = Unit(name=name, path=full, size=0, num=leading_number(name))
            for root, dirs, files in os.walk(full):
                if not opts.include_hidden:
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                if not files and not dirs:
                    unit.empty_dirs.append(os.path.relpath(root, src).replace(os.sep, "/"))
                for f in files:
                    if not opts.include_hidden and f.startswith("."):
                        continue
                    try:
                        size += os.path.getsize(os.path.join(root, f))
                        nfile += 1
                    except OSError:
                        pass
            unit.size = size
            subdirs.append(unit)
            total += size
            say("  %s  %d 个文件 %s" % (name, nfile, human(size)))
        elif os.path.isfile(full):
            if not opts.include_hidden and name.startswith("."):
                continue
            try:
                sz = os.path.getsize(full)
            except OSError:
                continue
            loose.append(Entry(rel=name, path=full, size=sz))
            total += sz

    return subdirs, loose, total


def _iter_entries(unit: Unit, src: str, include_hidden: bool) -> Iterable[Entry]:
    for root, dirs, files in os.walk(unit.path):
        if not include_hidden:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in sorted(files):
            if not include_hidden and f.startswith("."):
                continue
            fp = os.path.join(root, f)
            yield Entry(rel=os.path.relpath(fp, src).replace(os.sep, "/"),
                        path=fp, size=os.path.getsize(fp), group=unit.name)


# ----------------------------------------------------------------------------- 排序
def sort_units(units: list, mode: str) -> list:
    if mode == "auto":
        return sorted(units, key=lambda u: (u.sort_key, natural_key(u.name)))
    if mode == "name":
        return sorted(units, key=lambda u: natural_key(u.name))
    if mode == "mtime":
        def mt(u):
            try:
                return os.path.getmtime(u.path)
            except OSError:
                return 0
        return sorted(units, key=mt)
    return list(units)


# ----------------------------------------------------------------------------- 切分
def part_tag(opts: "Options", idx: int) -> str:
    """第1部分 / 第1批 / 第1卷 / PART1"""
    label = (opts.label or "部分").strip()
    if label.isascii():
        return "%s%d" % (label.upper(), idx)
    return "第%d%s" % (idx, label)


def make_filename(opts: Options, idx: int, part: Part) -> str:
    tag = part_tag(opts, idx)
    if opts.include_range:
        base = "%s_%s_%s" % (opts.eff_prefix, tag, part.range_text)
    else:
        base = "%s_%s" % (opts.eff_prefix, tag)
    return safe_filename(base) + ".zip"


def plan(opts: Options, progress: Callable[[str], None] | None = None) -> tuple[list[Part], list[Entry]]:
    """生成分包方案。返回 (parts, 被忽略的散落文件)"""
    say = progress or (lambda *_: None)
    subdirs, loose, total = scan(opts, say)
    if not subdirs and not loose:
        raise ValueError("源文件夹是空的，没有可打包的内容")

    limit = opts.limit_bytes
    parts: list[Part] = []

    # 散落文件处理
    loose_skipped = []
    loose_entries = []
    if opts.loose_mode == "skip":
        loose_skipped = list(loose)
    elif opts.loose_mode == "separate" and loose:
        size = sum(e.size for e in loose)
        p = Part(index=0, units=[], entries=list(loose), size=size)
        parts.append(p)
    else:  # first
        loose_entries = list(loose)

    if opts.split_mode == "dir":
        units = sort_units(subdirs, opts.sort_mode)
        cur_units, cur_entries, cur_size = [], list(loose_entries), sum(e.size for e in loose_entries)
        for u in units:
            say("评估 %s" % u.name)
            u.entries = list(_iter_entries(u, opts.src, opts.include_hidden))
            if cur_entries and cur_size + u.size > limit:
                parts.append(Part(index=0, units=cur_units, entries=cur_entries, size=cur_size))
                cur_units, cur_entries, cur_size = [], [], 0
            cur_units.append(u)
            cur_entries.extend(u.entries)
            cur_size += u.size
        if cur_entries:
            parts.append(Part(index=0, units=cur_units, entries=cur_entries, size=cur_size))
    else:
        # 严格按体积：文件夹可以跨包，散落文件仍排最前
        stream: list[Entry] = list(loose_entries)
        for u in sort_units(subdirs, opts.sort_mode):
            say("评估 %s" % u.name)
            u.entries = list(_iter_entries(u, opts.src, opts.include_hidden))
            stream.extend(u.entries)
        cur_entries, cur_size, cur_units = [], 0, []
        seen = set()
        for e in stream:
            if cur_entries and cur_size + e.size > limit:
                parts.append(Part(index=0, units=list(cur_units), entries=cur_entries, size=cur_size))
                cur_entries, cur_size, cur_units = [], 0, []
            cur_entries.append(e)
            cur_size += e.size
            if e.group and e.group not in seen:
                cur_units.append(Unit(name=e.group, path="", size=0))
                seen.add(e.group)
        if cur_entries:
            parts.append(Part(index=0, units=list(cur_units), entries=cur_entries, size=cur_size))

    for i, p in enumerate(parts, 1):
        p.index = i
        p.filename = make_filename(opts, i, p)
    return parts, loose_skipped


def summarize(parts: list[Part], limit_bytes: int = 0) -> str:
    lines = []
    total = sum(p.size for p in parts)
    for p in parts:
        lines.append("第%d部分  %s  %d 个子文件夹 / %d 个文件"
                     % (p.index, human(p.size), len([u for u in p.units if u.name]), len(p.entries)))
    tail = "合计 %d 个包，原始体积 %s" % (len(parts), human(total))
    if limit_bytes:
        tail += "，上限 %s/包" % human(limit_bytes)
    lines.append(tail)
    return "\n".join(lines)


# ----------------------------------------------------------------------------- 打包
def _method_for(entry: Entry, opts: Options):
    if opts.compress == "store":
        return zipfile.ZIP_STORED, None
    if opts.compress == "normal":
        return zipfile.ZIP_DEFLATED, 6
    if opts.compress == "max":
        return zipfile.ZIP_DEFLATED, 9
    ext = os.path.splitext(entry.path)[1].lower()
    if ext in ALREADY_COMPRESSED:
        return zipfile.ZIP_STORED, None
    return zipfile.ZIP_DEFLATED, 6


ProgressCb = Callable[[dict], None]


def build(opts: Options,
          progress: ProgressCb | None = None,
          cancelled: Callable[[], bool] | None = None) -> BuildResult:
    """执行打包。progress 收到 dict：phase/part/parts/file_files/bytes..."""
    cb = progress or (lambda *_: None)
    is_cancelled = cancelled or (lambda: False)

    parts, loose_skipped = plan(opts, lambda m: cb({"phase": "scan", "message": m}))
    os.makedirs(opts.out, exist_ok=True)

    total_files = sum(len(p.entries) for p in parts)
    total_bytes = sum(p.size for p in parts) or 1
    done_files = 0
    done_bytes = 0
    results, skipped = [], []
    t0 = time.time()
    manifest_lines = ["%s 分包清单" % opts.eff_prefix,
                      "生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                      "源目录：%s" % opts.src,
                      "共 %d 个包，原始体积 %s" % (len(parts), human(total_bytes)), ""]

    for p in parts:
        if is_cancelled():
            return BuildResult(ok=False, message="已取消", parts=results)
        zpath = os.path.join(opts.out, p.filename)
        if os.path.exists(zpath) and not opts.overwrite:
            return BuildResult(ok=False, message="已存在同名文件：%s" % p.filename, parts=results)
        cb({"phase": "start", "part": p.index, "parts": len(parts),
            "filename": p.filename, "size": p.size,
            "message": "正在打包 %s（%s）" % (p.filename, human(p.size))})

        part_files = 0
        try:
            with zipfile.ZipFile(zpath, "w", allowZip64=True) as zf:
                if opts.manifest_inside:
                    head = ["%s（第%d部分，共%d部分）" % (p.filename, p.index, len(parts)),
                            "序号范围：%s" % p.range_text,
                            "本包含 %d 个文件，原始体积 %s" % (len(p.entries), human(p.size)),
                            ""]
                    for u in p.units:
                        if u.name:
                            head.append("  - %s" % u.name)
                    for e in p.entries:
                        if not e.group:
                            head.append("  - %s（最外层文件）" % e.rel)
                    try:
                        zf.writestr("本包清单.txt", "\r\n".join(head).encode("utf-8-sig"))
                    except Exception:
                        pass
                for e in p.entries:
                    if is_cancelled():
                        zf.close()
                        try:
                            os.remove(zpath)
                        except OSError:
                            pass
                        return BuildResult(ok=False, message="已取消", parts=results)
                    method, level = _method_for(e, opts)
                    try:
                        if level is None:
                            zf.write(e.path, e.rel, compress_type=method)
                        else:
                            zf.write(e.path, e.rel, compress_type=method, compresslevel=level)
                        part_files += 1
                        done_files += 1
                        done_bytes += e.size
                        cb({"phase": "file", "part": p.index, "parts": len(parts),
                            "done_files": done_files, "files": total_files,
                            "done_bytes": done_bytes, "bytes": total_bytes,
                            "current": e.rel})
                    except Exception as ex:      # 单个文件失败不中断整批
                        skipped.append("%s（%s）" % (e.rel, ex))
                if opts.keep_empty_dirs:
                    for u in p.units:
                        for d in u.empty_dirs:
                            try:
                                zf.writestr(self_dir_entry(d), b"")
                            except Exception:
                                pass
                            if not u.entries:
                                u.entries.append(Entry(rel=self_dir_entry(d), path="", size=0))
        except Exception as ex:
            return BuildResult(ok=False, message="打包 %s 失败：%s" % (p.filename, ex), parts=results)

        size = os.path.getsize(zpath)
        results.append((p.filename, part_files, size))
        manifest_lines.append("【%s】" % p.filename)
        manifest_lines.append("  %d 个文件，%s" % (part_files, human(size)))
        for u in p.units:
            if u.name:
                manifest_lines.append("    - %s" % u.name)
        if not p.units:
            for e in p.entries:
                manifest_lines.append("    - %s" % e.rel)
        manifest_lines.append("")
        cb({"phase": "part_done", "part": p.index, "parts": len(parts),
            "filename": p.filename, "size": size, "elapsed": time.time() - t0})

    manifest_path = ""
    if opts.make_manifest:
        manifest_path = os.path.join(opts.out, "%s_分包清单.txt" % safe_filename(opts.eff_prefix))
        with open(manifest_path, "w", encoding="utf-8-sig", newline="\r\n") as f:
            f.write("\r\n".join(manifest_lines))
    return BuildResult(ok=True, message="完成，用时 %.0f 秒" % (time.time() - t0),
                       parts=results, skipped=skipped, manifest=manifest_path)


def self_dir_entry(rel: str) -> str:
    rel = rel.replace(os.sep, "/")
    return rel if rel.endswith("/") else rel + "/"


# ----------------------------------------------------------------------------- CLI
def _cli(argv):
    import argparse
    ap = argparse.ArgumentParser(description="大文件夹按体积上限顺序分包压缩")
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=float, default=900, help="每个包上限 MB")
    ap.add_argument("--sort", default="auto", choices=list(SORT_MODES))
    ap.add_argument("--split", default="dir", choices=list(SPLIT_MODES))
    ap.add_argument("--loose", default="first", choices=list(LOOSE_MODES))
    ap.add_argument("--label", default="部分")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--compress", default="auto", choices=list(COMPRESS_MODES))
    ap.add_argument("--no-range", action="store_true")
    ap.add_argument("--no-manifest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    out = a.out or os.path.join(os.path.dirname(os.path.normpath(a.src)), "分包结果")
    opts = Options(src=a.src, out=out, limit_mb=a.limit, sort_mode=a.sort,
                   split_mode=a.split, loose_mode=a.loose, label=a.label,
                   prefix=a.prefix, compress=a.compress,
                   include_range=not a.no_range, make_manifest=not a.no_manifest)
    if a.dry_run:
        parts, skipped = plan(opts, print)
        for p in parts:
            print("第%d部分 %s  %s  %d 个文件  %s" % (p.index, p.filename, human(p.size), len(p.entries), p.range_text))
        print("共 %d 个包，原始 %s；忽略散落文件 %d 个" % (len(parts), human(sum(p.size for p in parts)), len(skipped)))
        return 0
    r = build(opts, lambda d: None)
    print(r.message)
    for fn, n, sz in r.parts:
        print("  %-50s %4d 个文件  %s" % (fn, n, human(sz)))
    if r.skipped:
        print("跳过 %d 个文件：" % len(r.skipped))
        for s in r.skipped[:10]:
            print("  " + s)
    return 0 if r.ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
