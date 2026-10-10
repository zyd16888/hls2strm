#!/usr/bin/env python3
"""Move actor/number STRM directories to prefix/number without rescraping.

Standard library only. Run `plan` first, then `apply` on the same machine.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


NUMBER = re.compile(r"(?P<prefix>\d{0,4}[A-Za-z][A-Za-z0-9]{0,15})-\d{1,9}(?:-\w+)*")
FORMAT = "asia-directory-migration-v1"


def is_link(path: Path) -> bool:
    """Also reject Windows junctions and other reparse points on Python 3.9+."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def fingerprint(directory: Path) -> str:
    """Identify a tree across rename; reject links rather than follow them.

    This is a filesystem identity/metadata check, not a content checksum.
    Directory timestamps change when an empty parent is cleaned up.
    """
    entries = []
    for current, dirs, files in os.walk(directory, followlinks=False, onerror=raise_error):
        for name in [".", *sorted(dirs), *sorted(files)]:
            path = Path(current) / name
            if is_link(path):
                raise ValueError(f"含软链接或目录联接，需人工检查: {path}")
            stat = path.stat()
            if not path.is_file() and not path.is_dir():
                raise ValueError(f"含特殊文件，需人工检查: {path}")
            entries.append((path.relative_to(directory).as_posix(), stat.st_dev,
                            stat.st_ino, "dir" if path.is_dir() else "file",
                            stat.st_size if path.is_file() else 0,
                            stat.st_mtime_ns if path.is_file() else 0))
    data = json.dumps(sorted(entries), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def raise_error(error: OSError) -> None:
    raise error


def exists(path: Path) -> bool:
    return os.path.lexists(path)


def write_new(path: Path, data: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def check_root(path: str) -> Path:
    root = Path(os.path.abspath(path))
    if root == Path(root.anchor) or not root.is_dir():
        raise ValueError(f"必须指定现有库目录，不能是文件系统根目录: {root}")
    if root.resolve() != root:
        raise ValueError(f"库目录及其父目录不能经过软链接: {root}")
    return root


def plan(root: Path, output: Path) -> dict:
    root = check_root(str(root))
    if output.resolve().is_relative_to(root):
        raise ValueError("清单及其日志必须放在媒体库之外，避免改变待迁移目录")
    actions, skipped = [], []
    for parent in sorted(root.iterdir()):
        if is_link(parent):
            skipped.append({"path": parent.name, "reason": "软链接或目录联接"})
            continue
        if not parent.is_dir():
            skipped.append({"path": parent.name, "reason": "库根下的文件，非演员/番号结构"})
            continue
        for source in sorted(parent.iterdir()):
            relative = source.relative_to(root).as_posix()
            if is_link(source):
                skipped.append({"path": relative, "reason": "软链接或目录联接"})
                continue
            if not source.is_dir():
                skipped.append({"path": relative, "reason": "非影片目录"})
                continue
            match = NUMBER.fullmatch(source.name)
            if not match or match["prefix"].upper() in {"FC2", "FC2PPV", "HEYZO"}:
                skipped.append({"path": relative, "reason": "非普通番号；年份/厂商布局需单独处理"})
                continue
            if not any(p.is_file() and p.suffix.lower() == ".strm" for p in source.iterdir()):
                skipped.append({"path": relative, "reason": "目录内没有 STRM"})
                continue
            prefix = match["prefix"].upper()
            destination = root / prefix / source.name
            action = {"source": relative,
                      "destination": destination.relative_to(root).as_posix(),
                      "status": "move"}
            try:
                action["fingerprint"] = fingerprint(source)
            except (OSError, ValueError) as error:
                skipped.append({"path": relative, "reason": str(error)})
                continue
            if source == destination:
                action["status"] = "correct"
            elif exists(destination.parent) and (is_link(destination.parent)
                                                 or not destination.parent.is_dir()):
                action.update(status="conflict", reason="目标前缀不是普通目录")
            elif exists(destination):
                action.update(status="conflict", reason="目标已存在；不覆盖、不合并")
            actions.append(action)
    by_destination: dict[str, list[dict]] = {}
    for action in actions:
        by_destination.setdefault(action["destination"], []).append(action)
    for group in by_destination.values():
        if len(group) > 1:
            for action in group:
                if action["status"] != "correct":
                    action.update(status="conflict", reason="多处影片指向同一目标；需人工确认")
    data = {"format": FORMAT, "root": str(root), "actions": actions, "skipped": skipped}
    write_new(output, data)
    counts = {status: sum(a["status"] == status for a in actions)
              for status in ("move", "correct", "conflict")}
    print(f"待移动 {counts['move']}，位置正确 {counts['correct']}，冲突 {counts['conflict']}，跳过 {len(skipped)}")
    for action in actions:
        if action["status"] in {"move", "conflict"}:
            print(f"[{action['status']}] {action['source']} -> {action['destination']}"
                  + (f" ({action['reason']})" if "reason" in action else ""))
    for item in skipped:
        print(f"[skip] {item['path']}: {item['reason']}")
    print(f"清单已写入 {output}；尚未移动任何文件。")
    return data


def confined(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or len(path.parts) != 2 or any(p in {".", ".."} for p in path.parts):
        raise ValueError(f"清单必须使用库内两级相对路径: {relative}")
    result = root / path
    if result.resolve().is_relative_to(root) and not is_link(result.parent) and not is_link(result):
        return result
    raise ValueError(f"清单路径越界或经过软链接: {relative}")


def rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically refuse an existing destination, including an empty directory."""
    if sys.platform == "win32":
        os.rename(source, destination)  # Windows rename never replaces an existing destination.
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError("系统不支持 renameat2；为避免覆盖，已停止")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(destination))
    else:
        raise OSError("仅支持 Linux 和 Windows 的不覆盖移动")


def apply(plan_file: Path) -> int:
    data = json.loads(plan_file.read_text(encoding="utf-8"))
    if data.get("format") != FORMAT:
        raise ValueError("不支持的清单格式")
    root = check_root(data["root"])
    if plan_file.resolve().is_relative_to(root):
        raise ValueError("清单及其日志必须放在媒体库之外，避免改变待迁移目录")
    pending, completed = [], []
    # Preflight every planned move before changing the first directory.
    for action in data["actions"]:
        if action["status"] != "move":
            continue
        source = confined(root, action["source"])
        destination = confined(root, action["destination"])
        match = NUMBER.fullmatch(source.name)
        if not match or destination != root / match["prefix"].upper() / source.name or source == destination:
            raise ValueError(f"清单移动规则不合法: {action['source']}")
        if not exists(source):
            if destination.is_dir() and fingerprint(destination) == action["fingerprint"]:
                completed.append((source, destination))
                continue
            raise ValueError(f"原目录消失，目标不能确认为本次移动成果: {source}")
        if not source.is_dir() or fingerprint(source) != action["fingerprint"]:
            raise ValueError(f"原目录自预览后发生变化，请重新生成清单: {source}")
        if exists(destination):
            raise ValueError(f"预览后目标已出现，拒绝覆盖: {destination}")
        if exists(destination.parent) and not destination.parent.is_dir():
            raise ValueError(f"目标前缀不是目录: {destination.parent}")
        # Directory rename must stay on the same filesystem; never copy/delete.
        if source.stat().st_dev != root.stat().st_dev or (destination.parent.exists()
                and destination.parent.stat().st_dev != source.stat().st_dev):
            raise ValueError(f"跨文件系统移动不受支持: {source}")
        pending.append((source, destination, action))
    journal = plan_file.with_name(plan_file.name + ".journal.jsonl")
    if is_link(journal):
        raise ValueError(f"日志不能是软链接或目录联接: {journal}")
    with journal.open("a", encoding="utf-8", newline="\n") as stream:
        for source, destination, action in pending:
            # Recheck immediately before each move; MDCNG must be stopped.
            confined(root, action["source"])
            confined(root, action["destination"])
            if fingerprint(source) != action["fingerprint"]:
                raise ValueError(f"执行期间原目录发生变化，已停止: {source}")
            destination.parent.mkdir(exist_ok=True)
            event = {"source": str(source), "destination": str(destination)}
            for phase in ("before", "after"):
                stream.write(json.dumps(dict(event, phase=phase), ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                if phase == "before":
                    rename_no_replace(source, destination)
            completed.append((source, destination))
            print(f"[moved] {source.relative_to(root)} -> {destination.relative_to(root)}", flush=True)
    cleaned = 0
    for parent in sorted({source.parent for source, _ in completed}):
        try:
            parent.rmdir()  # Only the old parent, only when empty; no recursive deletion.
            cleaned += 1
        except OSError as error:
            if error.errno not in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT}:
                print(f"[cleanup skipped] {parent}: {error}")
    conflicts = sum(a["status"] == "conflict" for a in data["actions"])
    print(f"本次移动 {len(pending)}，此前已移动 {len(completed) - len(pending)}，清理空父目录 {cleaned}。")
    print(f"清单中的冲突 {conflicts}、跳过 {len(data['skipped'])} 未处理；日志: {journal}")
    return 2 if conflicts or data["skipped"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("plan", help="仅扫描并生成 JSON 清单，不移动文件")
    preview.add_argument("--root", required=True, help="如 /mnt/hls2strm_matched/asia")
    preview.add_argument("--out", required=True, type=Path, help="新清单路径，不覆盖已有清单")
    execute = commands.add_parser("apply", help="执行清单；中断后可重复执行同一清单")
    execute.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            plan(check_root(args.root), args.out)
            return 0
        return apply(args.plan)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"停止: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断；停止其他文件操作后，可用同一清单继续执行。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
