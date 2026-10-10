"""Filesystem migration safety checks; no real media libraries are touched."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


spec = importlib.util.spec_from_file_location(
    "reorganize_asia", Path(__file__).parents[1] / "scripts" / "reorganize_asia.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def movie(root, parent, number):
    directory = root / parent / number
    directory.mkdir(parents=True)
    (directory / f"{number}.strm").write_bytes(b"http://example.test/play/miab-576.m3u8\n")
    (directory / f"{number}.nfo").write_bytes(b"<movie><title>unchanged</title></movie>\n")
    (directory / "poster.jpg").write_bytes(b"poster bytes")
    (directory / "extrafanart").mkdir()
    (directory / "extrafanart" / "fanart1.jpg").write_bytes(b"nested image bytes")
    return directory


def hashes(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob("*") if p.is_file()}


def test_move_preserves_every_file_and_repeat_is_safe(tmp_path):
    root = tmp_path / "asia"
    source = movie(root, "黒島玲衣", "MIAB-576")
    before = hashes(source)
    already = movie(root, "VDD", "VDD-204")
    plan_file = tmp_path / "plan.json"
    data = migration.plan(root, plan_file)
    assert [a["status"] for a in data["actions"]].count("correct") == 1
    assert migration.apply(plan_file) == 0
    assert hashes(root / "MIAB" / "MIAB-576") == before
    assert already.is_dir()
    assert not source.parent.exists()
    assert migration.apply(plan_file) == 0
    assert migration.plan(root, tmp_path / "again.json")["actions"][0]["status"] == "correct"


def test_conflicting_targets_and_duplicate_sources_remain_intact(tmp_path):
    root = tmp_path / "asia"
    first = movie(root, "演员一", "ABP-001")
    second = movie(root, "演员二", "ABP-001")
    existing = movie(root, "演员一", "VDD-204")
    target = root / "VDD" / "VDD-204"
    target.mkdir(parents=True)  # Even an empty destination is a conflict.
    plan_file = tmp_path / "plan.json"
    data = migration.plan(root, plan_file)
    assert sum(a["status"] == "conflict" for a in data["actions"]) == 3
    assert migration.apply(plan_file) == 2
    assert all(p.is_dir() for p in (first, second, existing, target))


def test_changed_source_stops_whole_preflight(tmp_path):
    root = tmp_path / "asia"
    first = movie(root, "演员一", "ABP-001")
    second = movie(root, "演员二", "VDD-204")
    plan_file = tmp_path / "plan.json"
    migration.plan(root, plan_file)
    (second / "extra.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="发生变化"):
        migration.apply(plan_file)
    assert first.is_dir() and second.is_dir()
    assert not (root / "ABP").exists()


def test_resume_after_partial_execution(tmp_path, monkeypatch):
    root = tmp_path / "asia"
    first = movie(root, "演员一", "ABP-001-破解")
    second = movie(root, "演员二", "VDD-204")
    plan_file = tmp_path / "plan.json"
    migration.plan(root, plan_file)
    original = migration.rename_no_replace

    def interrupt(source, destination):
        if source == second:
            raise KeyboardInterrupt
        original(source, destination)

    monkeypatch.setattr(migration, "rename_no_replace", interrupt)
    with pytest.raises(KeyboardInterrupt):
        migration.apply(plan_file)
    assert not first.exists() and second.exists()
    monkeypatch.setattr(migration, "rename_no_replace", original)
    assert migration.apply(plan_file) == 0
    assert (root / "ABP" / first.name).is_dir()
    assert (root / "VDD" / second.name).is_dir()


def test_new_target_after_preview_and_no_replace(tmp_path):
    root = tmp_path / "asia"
    source = movie(root, "演员", "ABP-001")
    plan_file = tmp_path / "plan.json"
    migration.plan(root, plan_file)
    target = root / "ABP" / "ABP-001"
    target.mkdir(parents=True)
    with pytest.raises(ValueError, match="拒绝覆盖"):
        migration.apply(plan_file)
    with pytest.raises(OSError):
        migration.rename_no_replace(source, target)
    assert source.exists() and target.exists()


def test_special_layouts_and_non_strm_folders_are_skipped(tmp_path):
    root = tmp_path / "asia"
    specials = [movie(root, "2021", "FC2-PPV-1234567"),
                movie(root, "一本道", "070417-001"),
                movie(root, "HEYZO", "HEYZO-1234")]
    missing = root / "演员" / "MIAB-576"
    missing.mkdir(parents=True)
    data = migration.plan(root, tmp_path / "plan.json")
    assert not data["actions"] and len(data["skipped"]) == 4
    assert all(p.exists() for p in specials)


def test_symlinks_never_move_or_escape_root(tmp_path):
    root = tmp_path / "asia"
    source = movie(root, "演员", "ABP-001")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (source / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable on this host")
    data = migration.plan(root, tmp_path / "plan.json")
    assert not data["actions"]
    assert "软链接" in data["skipped"][0]["reason"]
    with pytest.raises(ValueError):
        migration.confined(root, "../outside")


def test_resume_rejects_unrelated_destination(tmp_path):
    root = tmp_path / "asia"
    source = movie(root, "演员", "ABP-001")
    plan_file = tmp_path / "plan.json"
    migration.plan(root, plan_file)
    migration.rename_no_replace(source, root / "演员" / "held")
    movie(root, "ABP", "ABP-001")
    with pytest.raises(ValueError, match="不能确认为本次移动成果"):
        migration.apply(plan_file)


def test_plan_and_journal_must_stay_outside_library(tmp_path):
    root = tmp_path / "asia"
    source = movie(root, "演员", "ABP-001")
    with pytest.raises(ValueError, match="媒体库之外"):
        migration.plan(root, source / "plan.json")
    assert not (source / "plan.json").exists()
    plan_file = tmp_path / "plan.json"
    migration.plan(root, plan_file)
    nested_plan = source / "plan.json"
    nested_plan.write_bytes(plan_file.read_bytes())
    with pytest.raises(ValueError, match="媒体库之外"):
        migration.apply(nested_plan)
    assert source.is_dir() and not (root / "ABP").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_windows_junctions_are_not_traversed_even_inside_library(tmp_path):
    root = tmp_path / "asia"
    source = movie(root, "演员", "ABP-001")
    target = root / "assets"
    target.mkdir()
    (target / "image.jpg").write_bytes(b"must stay unchanged")
    junction = source / "linked-assets"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], capture_output=True)
    if result.returncode:
        pytest.skip("Junction creation unavailable on this host")
    try:
        assert migration.is_link(junction)
        with pytest.raises(ValueError, match="目录联接"):
            migration.fingerprint(source)
        data = migration.plan(root, tmp_path / "plan.json")
        assert not data["actions"]
        assert any("目录联接" in item["reason"] for item in data["skipped"])
        with pytest.raises(ValueError):
            migration.confined(root, "演员/ABP-001/linked-assets")
        assert (target / "image.jpg").read_bytes() == b"must stay unchanged"
    finally:
        junction.rmdir()  # Remove only the junction, never its target directory.


def test_paths_outside_root_and_duplicate_plan_file_are_rejected(tmp_path):
    root = tmp_path / "asia"
    source = movie(root, "演员", "ABP-001")
    plan_file = tmp_path / "plan.json"
    data = migration.plan(root, plan_file)
    with pytest.raises(FileExistsError):
        migration.plan(root, plan_file)
    data["actions"][0]["destination"] = "../ABP-001"
    plan_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="两级相对路径"):
        migration.apply(plan_file)
    assert source.is_dir()
