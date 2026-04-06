#!/usr/bin/env python3
"""Smart cleanup CLI for Arch Linux.

Make executable with: chmod +x cleanup.py
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    print("Python 3.11+ is required for tomllib.", file=sys.stderr)
    raise


GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BOLD = "\033[1m"
RESET = "\033[0m"

HOME = Path.home()
DEFAULT_CONFIG_PATH = HOME / ".config" / "cleanup" / "config.toml"
DEFAULT_LOG_PATH = HOME / ".local" / "share" / "cleanup" / "cleanup.log"
DEPENDENCIES = ["fzf", "paccache", "journalctl", "fdupes", "dust"]
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
JUNK_SUFFIXES = (".orig", ".bak", "~")


@dataclass
class FileEntry:
    path: Path
    size: int
    last_access: datetime
    reason: str


@dataclass
class CleanupStats:
    freed: int = 0
    skipped: int = 0
    errors: int = 0


@dataclass
class Context:
    config: dict
    config_path: Path
    log_path: Path
    dry_run: bool
    yes: bool
    quiet: bool
    stats: CleanupStats = field(default_factory=CleanupStats)
    missing_bins: set[str] = field(default_factory=set)
    noatime_home: bool = False
    sudo_retry_all: bool = False
    home_scan_cache: tuple[list[Path], list[Path]] | None = None

    def print(self, message: str = "") -> None:
        if not self.quiet:
            print(message)


def default_config_text() -> str:
    return """[general]
dry_run = false
log_path = "~/.local/share/cleanup/cleanup.log"

[cache]
enabled = true
exclude = ["mozilla", "chromium", "thumbnails"]

[old_files]
enabled = true
scan_dirs = ["~/Downloads", "~/tmp"]
days_unused = 90
min_size_kb = 100
exclude_ext = [".py", ".sh", ".md", ".toml", ".json"]

[categories]
pacman = true
pip = true
npm = true
cargo = true
logs = true
trash = true
junk = true
symlinks = true
empty = true
dupes = true
"""


def deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def ensure_config(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_config_text(), encoding="utf-8")


def load_config(path: Path) -> dict:
    ensure_config(path)
    defaults = tomllib.loads(default_config_text())
    with path.open("rb") as fh:
        loaded = tomllib.load(fh)
    return deep_merge(defaults, loaded)


def expand_path(raw: str | Path) -> Path:
    return Path(raw).expanduser()


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(size, 0))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def parse_human_size(text: str) -> int:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?)(?:I?B)?\b", text, re.IGNORECASE)
    if not match:
        return 0
    value = float(match.group(1))
    unit = (match.group(2).upper() or "B") + "B"
    scale = {
        "B": 1,
        "KB": 1024,
        "MB": 1024**2,
        "GB": 1024**3,
        "TB": 1024**4,
        "PB": 1024**5,
    }
    return int(value * scale.get(unit, 1))


def measure_journal_size(ctx: Context) -> int:
    if "journalctl" in ctx.missing_bins:
        return 0
    usage = run_command(ctx, ["journalctl", "--disk-usage"], reason="journal usage")
    if usage.returncode != 0:
        return 0
    return parse_human_size(usage.stdout)


def estimate_paccache_reclaimable(ctx: Context, cache_dir: Path) -> int:
    if "paccache" in ctx.missing_bins:
        return 0
    proc = run_command(
        ctx,
        ["paccache", "-dvvzk2"],
        reason="paccache dry-run estimate",
    )
    if proc.returncode != 0:
        return 0

    total = 0
    for raw in proc.stdout.split("\0"):
        candidate = raw.strip()
        if not candidate or candidate.startswith("==>"):
            continue
        path = Path(candidate)
        if not path.is_absolute():
            path = cache_dir / candidate
        total += path_size(path)
    return total


def fmt_date(ts: datetime) -> str:
    return ts.astimezone().strftime("%Y-%m-%d")


def shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def safe_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except OSError:
        return None


def path_size(path: Path) -> int:
    st = safe_lstat(path)
    if st is None:
        return 0
    if stat.S_ISLNK(st.st_mode):
        return 0
    if stat.S_ISREG(st.st_mode):
        return st.st_size
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size
    total = 0
    for root, dirs, files in os.walk(path, topdown=True, onerror=None, followlinks=False):
        dirs[:] = [name for name in dirs if not Path(root, name).is_symlink()]
        for name in files:
            child = Path(root, name)
            st_child = safe_lstat(child)
            if st_child and stat.S_ISREG(st_child.st_mode):
                total += st_child.st_size
    return total


def file_count(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return 1
    total = 0
    for _root, _dirs, files in os.walk(path):
        total += len(files)
    return total


def log_action(ctx: Context, path: Path, size: int, reason: str) -> None:
    ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().astimezone().isoformat()}\t{path}\t{size}\t{reason}\n"
    with ctx.log_path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def should_retry_with_sudo(ctx: Context, path: Path) -> bool:
    if shutil.which("sudo") is None:
        return False
    if ctx.sudo_retry_all:
        return True
    approved = prompt_yes_no(
        ctx,
        f"  sudo required for {path}. Retry with sudo?",
        default=False,
        force_prompt=True,
    )
    if approved and prompt_yes_no(ctx, "  Reuse sudo automatically for later permission errors?", default=True, force_prompt=True):
        ctx.sudo_retry_all = True
    return approved


def sudo_delete_path(ctx: Context, path: Path, reason: str, size: int) -> tuple[int, bool]:
    if ctx.dry_run:
        shown = " ".join(shell_quote(part) for part in ["sudo", "rm", "-rf", str(path)])
        ctx.print(f"{YELLOW}  - dry-run: {shown}{RESET}")
        return size, True
    proc = subprocess.run(
        ["sudo", "rm", "-rf", "--", str(path)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        ctx.print(f"{RED}  ! sudo delete failed for {path}{RESET}")
        if proc.stderr:
            ctx.print(proc.stderr.strip())
        ctx.stats.errors += 1
        return 0, False
    log_action(ctx, path, size, f"{reason}:sudo")
    return size, True


def sudo_truncate_file(ctx: Context, path: Path, reason: str, size: int) -> tuple[int, bool]:
    if ctx.dry_run:
        shown = " ".join(shell_quote(part) for part in ["sudo", "sh", "-c", f": > {shell_quote(str(path))}"])
        ctx.print(f"{YELLOW}  - dry-run: {shown}{RESET}")
        return size, True
    proc = subprocess.run(
        ["sudo", "sh", "-c", f": > {shell_quote(str(path))}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        ctx.print(f"{RED}  ! sudo truncate failed for {path}{RESET}")
        if proc.stderr:
            ctx.print(proc.stderr.strip())
        ctx.stats.errors += 1
        return 0, False
    log_action(ctx, path, size, f"{reason}:sudo")
    return size, True


def delete_path(ctx: Context, path: Path, reason: str) -> tuple[int, bool]:
    size = path_size(path)
    if ctx.dry_run:
        return size, True
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path)
        log_action(ctx, path, size, reason)
        return size, True
    except OSError as exc:
        if exc.errno in {1, 13} and should_retry_with_sudo(ctx, path):
            return sudo_delete_path(ctx, path, reason, size)
        ctx.print(f"{RED}  ! Failed to delete {path}: {exc}{RESET}")
        ctx.stats.errors += 1
        return 0, False


def truncate_file(ctx: Context, path: Path, reason: str) -> tuple[int, bool]:
    size = path_size(path)
    if ctx.dry_run:
        return size, True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8"):
            pass
        log_action(ctx, path, size, reason)
        return size, True
    except OSError as exc:
        if exc.errno in {1, 13} and should_retry_with_sudo(ctx, path):
            return sudo_truncate_file(ctx, path, reason, size)
        ctx.print(f"{RED}  ! Failed to truncate {path}: {exc}{RESET}")
        ctx.stats.errors += 1
        return 0, False


def prompt_yes_no(ctx: Context, prompt: str, default: bool = True, force_prompt: bool = False) -> bool:
    if ctx.yes and not force_prompt:
        return True
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        reply = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not reply:
        return default
    return reply in {"y", "yes"}


def print_section(ctx: Context, title: str) -> None:
    ctx.print(f"{CYAN}{title}{RESET}")


def print_category_summary(ctx: Context, name: str, size: int, note: str = "") -> None:
    extra = f" {note}" if note else ""
    ctx.print(f"  [{name}] {BOLD}{human_size(size)}{RESET}{extra}")


def skip_zero_size(ctx: Context, name: str, size: int) -> bool:
    if size > 0:
        return False
    ctx.print(f"{YELLOW}  - Skipped {name}: nothing to free{RESET}")
    return True


def run_command(
    ctx: Context,
    command: list[str],
    reason: str,
    use_sudo: bool = False,
    capture: bool = True,
    check: bool = False,
    destructive: bool = False,
) -> subprocess.CompletedProcess[str]:
    full = ["sudo"] + command if use_sudo else command
    if ctx.dry_run and destructive:
        shown = " ".join(shell_quote(part) for part in full)
        ctx.print(f"{YELLOW}  - dry-run: {shown}{RESET}")
        return subprocess.CompletedProcess(full, 0, "", "")
    try:
        proc = subprocess.run(
            full,
            check=check,
            capture_output=capture,
            text=True,
        )
        if proc.returncode != 0 and not check:
            ctx.print(f"{RED}  ! Command failed ({reason}): {' '.join(full)}{RESET}")
            if proc.stderr:
                ctx.print(proc.stderr.strip())
            ctx.stats.errors += 1
        return proc
    except FileNotFoundError as exc:
        ctx.print(f"{RED}  ! Missing command for {reason}: {exc.filename}{RESET}")
        ctx.stats.errors += 1
        return subprocess.CompletedProcess(full, 127, "", str(exc))


def check_dependencies(ctx: Context) -> None:
    missing = {name for name in DEPENDENCIES if shutil.which(name) is None}
    ctx.missing_bins = missing
    if missing:
        ctx.print(
            f"{YELLOW}Warning:{RESET} missing external tools: {', '.join(sorted(missing))}. "
            "Some cleanup phases will be skipped."
        )


def print_log_tail(log_path: Path, lines: int = 20) -> None:
    if not log_path.exists():
        print(f"No log file at {log_path}")
        return
    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in content[-lines:]:
        print(line)


def is_junk_name(name: str) -> bool:
    return name in JUNK_NAMES or name.endswith(JUNK_SUFFIXES)


def scan_home_for_junk_and_symlinks(ctx: Context) -> tuple[list[Path], list[Path]]:
    if ctx.home_scan_cache is not None:
        return ctx.home_scan_cache

    junk_matches: list[Path] = []
    broken_matches: list[Path] = []

    def walk_dir(root: str) -> None:
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if entry.name == ".git" and entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        if entry.is_symlink() and not os.path.exists(entry.path):
                            broken_matches.append(Path(entry.path))
                            continue
                        if entry.is_file(follow_symlinks=False):
                            if is_junk_name(entry.name):
                                junk_matches.append(Path(entry.path))
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            walk_dir(entry.path)
                    except OSError:
                        continue
        except OSError:
            return

    walk_dir(str(HOME))

    ctx.home_scan_cache = (junk_matches, broken_matches)
    return ctx.home_scan_cache


def scan_empty_dirs() -> list[Path]:
    bases = [HOME / "Downloads", HOME / "tmp", HOME / "Desktop"]
    results: list[Path] = []
    for base in bases:
        if not base.exists():
            continue
        for root, dirs, files in os.walk(base, topdown=False):
            if not dirs and not files:
                results.append(Path(root))
    return results


def parse_fdupes_output(output: str) -> list[list[Path]]:
    groups: list[list[Path]] = []
    current: list[Path] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            if len(current) > 1:
                groups.append(current)
            current = []
            continue
        current.append(Path(line))
    if len(current) > 1:
        groups.append(current)
    return groups


def get_duplicates(ctx: Context) -> list[FileEntry]:
    if "fdupes" in ctx.missing_bins:
        return []
    roots = [HOME / "Downloads", HOME / "Documents"]
    existing = [str(path) for path in roots if path.exists()]
    if not existing:
        return []
    proc = run_command(ctx, ["fdupes", "-r", *existing], reason="duplicates scan")
    if proc.returncode != 0:
        return []
    entries: list[FileEntry] = []
    for group in parse_fdupes_output(proc.stdout):
        ranked = sorted(
            group,
            key=lambda path: safe_lstat(path).st_mtime if safe_lstat(path) else 0,
            reverse=True,
        )
        for duplicate in ranked[1:]:
            st = safe_lstat(duplicate)
            if not st or not stat.S_ISREG(st.st_mode):
                continue
            entries.append(
                FileEntry(
                    path=duplicate,
                    size=st.st_size,
                    last_access=datetime.fromtimestamp(max(st.st_atime, st.st_mtime), tz=timezone.utc),
                    reason="duplicate",
                )
            )
    return entries


def summarize_and_apply_paths(
    ctx: Context,
    name: str,
    paths: Iterable[Path],
    reason: str,
    prompt_note: str = "",
    confirm_default: bool = True,
    force_prompt: bool = False,
) -> int:
    items = [path for path in paths if path.exists() or path.is_symlink()]
    total = sum(path_size(path) for path in items)
    print_category_summary(ctx, name, total, prompt_note)
    if not items:
        if total == 0:
            ctx.print(f"{YELLOW}  - Skipped {name}: nothing to free{RESET}")
        return 0
    if skip_zero_size(ctx, name, total):
        return 0
    if not prompt_yes_no(ctx, f"  [{name}] clean?", default=confirm_default, force_prompt=force_prompt):
        ctx.stats.skipped += total
        ctx.print(f"{YELLOW}  - Skipped {name}{RESET}")
        return 0
    freed = 0
    for item in items:
        size, ok = delete_path(ctx, item, reason)
        if ok:
            freed += size
    ctx.stats.freed += freed
    label = "Would free" if ctx.dry_run else "Freed"
    ctx.print(f"{GREEN}  ✓ {label} {human_size(freed)}{RESET}")
    return freed


def clean_static(ctx: Context) -> None:
    print_section(ctx, "== Phase 1: Static cleaning ==")
    config = ctx.config
    cache_cfg = config.get("cache", {})
    categories = config.get("categories", {})

    if cache_cfg.get("enabled", True):
        cache_dir = HOME / ".cache"
        excluded = set(cache_cfg.get("exclude", []))
        targets = [path for path in cache_dir.iterdir()] if cache_dir.exists() else []
        cache_targets = [path for path in targets if path.name not in excluded]
        freed = summarize_and_apply_paths(ctx, "cache", cache_targets, "static:cache")
        recent = HOME / ".local" / "share" / "recently-used.xbel"
        if recent.exists():
            size = path_size(recent)
            print_category_summary(ctx, "cache.recent", size, "(truncate xbel)")
            if skip_zero_size(ctx, "cache.recent", size):
                pass
            elif prompt_yes_no(ctx, "  [cache.recent] clean?"):
                freed_recent, ok = truncate_file(ctx, recent, "static:recently-used")
                if ok:
                    ctx.stats.freed += freed_recent
                    label = "Would free" if ctx.dry_run else "Freed"
                    ctx.print(f"{GREEN}  ✓ {label} {human_size(freed_recent)}{RESET}")
            else:
                ctx.stats.skipped += size
                ctx.print(f"{YELLOW}  - Skipped cache.recent{RESET}")

    if categories.get("pacman", True):
        pacman_cache = Path("/var/cache/pacman/pkg")
        total_cache_size = path_size(pacman_cache) if pacman_cache.exists() else 0
        reclaimable_size = estimate_paccache_reclaimable(ctx, pacman_cache)
        note = f"(paccache -rk2, cache total {human_size(total_cache_size)})"
        print_category_summary(ctx, "pacman", reclaimable_size, note)
        if skip_zero_size(ctx, "pacman", reclaimable_size):
            pass
        elif prompt_yes_no(ctx, "  [pacman] clean?"):
            before_size = path_size(pacman_cache)
            proc = run_command(
                ctx,
                ["paccache", "-rk2"],
                reason="pacman cache cleanup",
                use_sudo=True,
                destructive=True,
            )
            if proc.returncode == 0:
                freed = reclaimable_size if ctx.dry_run else max(0, before_size - path_size(pacman_cache))
                ctx.stats.freed += freed
                label = "Would free up to" if ctx.dry_run else "Freed"
                ctx.print(f"{GREEN}  ✓ {label} {human_size(freed)}{RESET}")
        else:
            ctx.stats.skipped += reclaimable_size
            ctx.print(f"{YELLOW}  - Skipped pacman cache{RESET}")

        orphans = run_command(ctx, ["pacman", "-Qdtq"], reason="pacman orphan scan")
        orphan_list = [line.strip() for line in orphans.stdout.splitlines() if line.strip()]
        if orphan_list:
            note = f"({len(orphan_list)} packages)"
            print_category_summary(ctx, "pacman.orphans", 0, note)
            if skip_zero_size(ctx, "pacman.orphans", 0):
                pass
            elif prompt_yes_no(ctx, "  [pacman.orphans] remove packages?", default=False, force_prompt=True):
                run_command(
                    ctx,
                    ["pacman", "-Rs", "--noconfirm", *orphan_list],
                    reason="remove pacman orphans",
                    use_sudo=True,
                    destructive=True,
                )
            else:
                ctx.print(f"{YELLOW}  - Skipped pacman orphans{RESET}")

    if categories.get("pip", True):
        summarize_and_apply_paths(
            ctx,
            "pip/uv",
            [HOME / ".cache" / "pip", HOME / ".cache" / "uv"],
            "static:pip-uv",
        )

    if categories.get("npm", True):
        summarize_and_apply_paths(
            ctx,
            "npm/yarn/pnpm",
            [HOME / ".npm" / "_cacache", HOME / ".cache" / "yarn", HOME / ".local" / "share" / "pnpm" / "store"],
            "static:node-caches",
        )

    if categories.get("cargo", True):
        summarize_and_apply_paths(
            ctx,
            "cargo",
            [HOME / ".cargo" / "registry" / "cache", HOME / ".cargo" / "registry" / "src"],
            "static:cargo",
        )

    if categories.get("logs", True):
        old_logs: list[Path] = []
        share_dir = HOME / ".local" / "share"
        cutoff = datetime.now().timestamp() - (30 * 24 * 60 * 60)
        if share_dir.exists():
            for candidate in share_dir.glob("*.log"):
                st = safe_lstat(candidate)
                if st and st.st_mtime < cutoff:
                    old_logs.append(candidate)
        journal_size = measure_journal_size(ctx)
        old_logs_size = sum(path_size(path) for path in old_logs)
        reclaimable_estimate = old_logs_size
        journal_note = (
            "journal empty"
            if journal_size == 0
            else f"journal current {human_size(journal_size)}, reclaim unknown"
        )
        note = f"(old *.log {human_size(old_logs_size)}; {journal_note})"
        print_category_summary(ctx, "logs", reclaimable_estimate, note)
        should_offer_logs = old_logs_size > 0 or journal_size > 0
        if not should_offer_logs:
            ctx.print(f"{YELLOW}  - Skipped logs: nothing to free{RESET}")
        elif prompt_yes_no(ctx, "  [logs] clean?"):
            freed = 0
            if "journalctl" not in ctx.missing_bins and journal_size > 0:
                before_journal_size = journal_size
                proc = run_command(
                    ctx,
                    ["journalctl", "--vacuum-time=7d"],
                    reason="journal cleanup",
                    destructive=True,
                )
                if proc.returncode == 0:
                    if ctx.dry_run:
                        freed += 0
                    else:
                        freed += max(0, before_journal_size - measure_journal_size(ctx))
            for item in old_logs:
                size, ok = delete_path(ctx, item, "static:old-log")
                if ok:
                    freed += size
            ctx.stats.freed += freed
            label = "Would free" if ctx.dry_run else "Freed"
            ctx.print(f"{GREEN}  ✓ {label} {human_size(freed)}{RESET}")
        else:
            ctx.stats.skipped += reclaimable_estimate
            ctx.print(f"{YELLOW}  - Skipped logs{RESET}")

    if categories.get("trash", True):
        trash = HOME / ".local" / "share" / "Trash"
        total = path_size(trash)
        note = f"({file_count(trash)} files)"
        print_category_summary(ctx, "trash", total, note)
        if skip_zero_size(ctx, "trash", total):
            pass
        elif prompt_yes_no(ctx, "  [trash] empty trash?"):
            freed = 0
            for name in ("files", "info"):
                target = trash / name
                size, ok = delete_path(ctx, target, "static:trash")
                if ok:
                    freed += size
            if not ctx.dry_run:
                (trash / "files").mkdir(parents=True, exist_ok=True)
                (trash / "info").mkdir(parents=True, exist_ok=True)
            ctx.stats.freed += freed
            label = "Would free" if ctx.dry_run else "Freed"
            ctx.print(f"{GREEN}  ✓ {label} {human_size(freed)}{RESET}")
        else:
            ctx.stats.skipped += total
            if total:
                ctx.print(f"{YELLOW}  - Skipped trash{RESET}")

    junk_files: list[Path] = []
    broken: list[Path] = []
    if categories.get("junk", True) or categories.get("symlinks", True):
        junk_files, broken = scan_home_for_junk_and_symlinks(ctx)

    if categories.get("junk", True):
        total = sum(path_size(path) for path in junk_files)
        print_category_summary(ctx, "junk_files", total, f"({len(junk_files)} matches)")
        if skip_zero_size(ctx, "junk_files", total):
            pass
        elif prompt_yes_no(ctx, "  [junk_files] clean?"):
            freed = 0
            for item in junk_files:
                size, ok = delete_path(ctx, item, "static:junk")
                if ok:
                    freed += size
            ctx.stats.freed += freed
            label = "Would free" if ctx.dry_run else "Freed"
            ctx.print(f"{GREEN}  ✓ {label} {human_size(freed)}{RESET}")
        else:
            ctx.stats.skipped += total
            ctx.print(f"{YELLOW}  - Skipped junk files{RESET}")

    if categories.get("symlinks", True):
        total = sum(path_size(path) for path in broken)
        print_category_summary(ctx, "broken_symlinks", total, f"({len(broken)} links)")
        if broken and total > 0:
            for item in broken:
                ctx.print(f"    {item}")
        if skip_zero_size(ctx, "broken_symlinks", total):
            pass
        elif broken and prompt_yes_no(ctx, "  [broken_symlinks] delete listed links?", default=False):
            freed = 0
            for item in broken:
                size, ok = delete_path(ctx, item, "static:broken-symlink")
                if ok:
                    freed += size
            ctx.stats.freed += freed
            label = "Would free" if ctx.dry_run else "Freed"
            ctx.print(f"{GREEN}  ✓ {label} {human_size(freed)}{RESET}")
        else:
            ctx.stats.skipped += total
            if broken:
                ctx.print(f"{YELLOW}  - Skipped broken symlinks{RESET}")

    if categories.get("empty", True):
        empties = scan_empty_dirs()
        total = len(empties)
        print_category_summary(ctx, "empty_dirs", 0, f"({total} dirs)")
        for item in empties:
            if ctx.dry_run:
                continue
            else:
                try:
                    item.rmdir()
                    log_action(ctx, item, 0, "static:empty-dir")
                except OSError as exc:
                    if exc.errno in {1, 13} and should_retry_with_sudo(ctx, item):
                        sudo_delete_path(ctx, item, "static:empty-dir", 0)
                    else:
                        ctx.print(f"{RED}  ! Failed to remove empty dir {item}: {exc}{RESET}")
                        ctx.stats.errors += 1
        if empties:
            ctx.print(f"{GREEN}  ✓ Removed {len(empties)} empty directories{RESET}")

    if categories.get("dupes", True):
        duplicates = get_duplicates(ctx)
        total = sum(entry.size for entry in duplicates)
        print_category_summary(ctx, "duplicates", total, f"({len(duplicates)} candidates)")
        if skip_zero_size(ctx, "duplicates", total):
            pass
        elif duplicates and prompt_yes_no(ctx, "  [duplicates] send to fzf review?"):
            fzf_review(ctx, duplicates)
        else:
            ctx.stats.skipped += total
            if duplicates:
                ctx.print(f"{YELLOW}  - Skipped duplicates review{RESET}")


def home_noatime_detected() -> bool:
    fstab = Path("/etc/fstab")
    if not fstab.exists():
        return False
    try:
        lines = fstab.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False
    candidates: list[tuple[str, str]] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        mountpoint, options = parts[1], parts[3]
        candidates.append((mountpoint, options))
    home_str = str(HOME)
    matched = False
    best_len = -1
    for mountpoint, options in candidates:
        if home_str == mountpoint or home_str.startswith(mountpoint.rstrip("/") + "/"):
            if len(mountpoint) > best_len:
                matched = "noatime" in options.split(",")
                best_len = len(mountpoint)
    return matched


def collect_old_files(ctx: Context, scan_dirs: list[Path]) -> list[FileEntry]:
    old_cfg = ctx.config.get("old_files", {})
    days_unused = int(old_cfg.get("days_unused", 90))
    min_size = int(old_cfg.get("min_size_kb", 100)) * 1024
    exclude_ext = {str(ext).lower() for ext in old_cfg.get("exclude_ext", [])}
    now_ts = datetime.now().timestamp()
    threshold = days_unused * 24 * 60 * 60
    results: list[FileEntry] = []
    use_mtime_only = ctx.noatime_home
    for base in scan_dirs:
        if not base.exists():
            continue
        for root, dirs, files in os.walk(base, topdown=True, onerror=None):
            dirs[:] = [name for name in dirs if name != ".git"]
            for name in files:
                path = Path(root) / name
                st = safe_lstat(path)
                if not st or not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_size <= min_size:
                    continue
                if path.suffix.lower() in exclude_ext:
                    continue
                last_seen_ts = st.st_mtime if use_mtime_only else max(st.st_atime, st.st_mtime)
                if now_ts - last_seen_ts <= threshold:
                    continue
                results.append(
                    FileEntry(
                        path=path,
                        size=st.st_size,
                        last_access=datetime.fromtimestamp(last_seen_ts, tz=timezone.utc),
                        reason="old_file",
                    )
                )
    results.sort(key=lambda item: item.size, reverse=True)
    return results


def print_old_files_table(ctx: Context, entries: list[FileEntry]) -> None:
    ctx.print(f"{'SIZE':>10}  {'LAST ACCESS':<12}  PATH")
    for entry in entries:
        ctx.print(f"{human_size(entry.size):>10}  {fmt_date(entry.last_access):<12}  {entry.path}")


def analyze_old_files(ctx: Context, scan_override: list[Path] | None = None) -> list[FileEntry]:
    print_section(ctx, "== Phase 2: Old file analysis ==")
    old_cfg = ctx.config.get("old_files", {})
    if not old_cfg.get("enabled", True):
        ctx.print(f"{YELLOW}old_files disabled in config{RESET}")
        return []
    ctx.noatime_home = home_noatime_detected()
    if ctx.noatime_home:
        ctx.print(f"{YELLOW}⚠ noatime detected — using mtime only{RESET}")
    scan_dirs = scan_override or [expand_path(raw) for raw in old_cfg.get("scan_dirs", [])]
    entries = collect_old_files(ctx, scan_dirs)
    if not entries:
        ctx.print(f"{GREEN}No old files matched the current filters.{RESET}")
        return []
    print_old_files_table(ctx, entries)
    if prompt_yes_no(ctx, "Send to fzf for review?"):
        fzf_review(ctx, entries)
    else:
        skipped = sum(entry.size for entry in entries)
        ctx.stats.skipped += skipped
        ctx.print(f"{YELLOW}Skipped {len(entries)} old files ({human_size(skipped)}){RESET}")
    return entries


def parse_fzf_selection(stdout: str, lookup: dict[str, FileEntry]) -> list[FileEntry]:
    selected: list[FileEntry] = []
    for line in stdout.splitlines():
        if not line.strip() or "\t" not in line:
            continue
        key = line.split("\t", 1)[0]
        entry = lookup.get(key)
        if entry is not None:
            selected.append(entry)
    return selected


def fzf_review(ctx: Context, file_list: list[FileEntry]) -> int:
    if not file_list:
        ctx.print(f"{YELLOW}No files to review.{RESET}")
        return 0
    if "fzf" in ctx.missing_bins:
        ctx.print(f"{YELLOW}fzf is missing; interactive review skipped.{RESET}")
        skipped = sum(item.size for item in file_list)
        ctx.stats.skipped += skipped
        return 0

    lookup: dict[str, FileEntry] = {}
    lines: list[str] = []
    for item in file_list:
        key = str(item.path)
        lookup[key] = item
        display = f"[{human_size(item.size)}]  [{fmt_date(item.last_access)}]  [{item.reason}]  {item.path}"
        lines.append(f"{key}\t{display}")

    try:
        proc = subprocess.run(
            [
                "fzf",
                "--multi",
                "--delimiter=\t",
                "--with-nth=2..",
                "--preview",
                "file {1} ; du -sh {1}",
                "--preview-window=right:40%",
                "--header=TAB=select  ENTER=delete selected  ESC=skip",
                "--bind=ctrl-a:select-all",
            ],
            input="\n".join(lines) + "\n",
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        ctx.print(f"{RED}Failed to launch fzf: {exc}{RESET}")
        ctx.stats.errors += 1
        return 0

    if proc.returncode != 0:
        ctx.print(f"{YELLOW}fzf review cancelled.{RESET}")
        return 0

    selected = parse_fzf_selection(proc.stdout, lookup)
    if not selected:
        ctx.print(f"{YELLOW}No files selected.{RESET}")
        return 0

    total = sum(item.size for item in selected)
    ctx.print("")
    ctx.print("Selected files:")
    for item in selected:
        ctx.print(f"  {item.path} ({human_size(item.size)})")
    if not prompt_yes_no(ctx, f"Delete {len(selected)} files ({human_size(total)})?", default=False):
        ctx.stats.skipped += total
        ctx.print(f"{YELLOW}Skipped {len(selected)} selected files{RESET}")
        return 0

    freed = 0
    for item in selected:
        size, ok = delete_path(ctx, item.path, item.reason)
        if ok:
            freed += size
    ctx.stats.freed += freed
    label = "Would free" if ctx.dry_run else "Freed"
    ctx.print(f"{GREEN}✓ {label} {human_size(freed)}{RESET}")
    return freed


def manual_interactive_entries(paths: list[Path]) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for base in paths:
        if not base.exists():
            continue
        if base.is_file():
            st = safe_lstat(base)
            if st:
                entries.append(
                    FileEntry(
                        path=base,
                        size=st.st_size,
                        last_access=datetime.fromtimestamp(max(st.st_atime, st.st_mtime), tz=timezone.utc),
                        reason="manual",
                    )
                )
            continue
        for root, _dirs, files in os.walk(base):
            for name in files:
                path = Path(root) / name
                st = safe_lstat(path)
                if not st or not stat.S_ISREG(st.st_mode):
                    continue
                entries.append(
                    FileEntry(
                        path=path,
                        size=st.st_size,
                        last_access=datetime.fromtimestamp(max(st.st_atime, st.st_mtime), tz=timezone.utc),
                        reason="manual",
                    )
                )
    entries.sort(key=lambda item: item.size, reverse=True)
    return entries


def print_final_summary(ctx: Context) -> None:
    log_display = str(ctx.log_path).replace(str(HOME), "~")
    width = max(29, len(log_display) + 10)
    inner = width - 2
    freed_label = "Would free" if ctx.dry_run else "Freed"

    def box_line(content: str) -> str:
        return f"│{content:<{inner}}│"

    lines = [
        "┌" + "─" * inner + "┐",
        box_line("  cleanup complete"),
        box_line(f"  {freed_label}:     {human_size(ctx.stats.freed)}"),
        box_line(f"  Skipped:   {human_size(ctx.stats.skipped)}"),
        box_line(f"  Errors:    {ctx.stats.errors}"),
        box_line(f"  Log:  {log_display}"),
        "└" + "─" * inner + "┘",
    ]
    for line in lines:
        print(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cleanup", description="Smart cleanup CLI")
    parser.add_argument("paths", nargs="*", help="Override old-file scan dirs or manual interactive targets")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Show what would be deleted")
    parser.add_argument("-y", "--yes", action="store_true", help="Auto-confirm prompts except pacman orphans")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only print summary at end")
    parser.add_argument("--config", type=Path, help="Custom config path")
    parser.add_argument("--log", action="store_true", help="Print last 20 log entries and exit")
    parser.add_argument("--static", action="store_true", dest="phase_static", help="Run Phase 1 only")
    parser.add_argument("--old", action="store_true", dest="phase_old", help="Run Phase 2 only")
    parser.add_argument("--interactive", action="store_true", help="Run Phase 3 only")
    return parser


def selected_phases(args: argparse.Namespace) -> tuple[bool, bool, bool]:
    if args.phase_static or args.phase_old or args.interactive:
        return args.phase_static, args.phase_old, args.interactive
    return True, True, False


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config_path = expand_path(args.config or DEFAULT_CONFIG_PATH)
    config = load_config(config_path)
    dry_run = args.dry_run or bool(config.get("general", {}).get("dry_run", False))
    log_path = expand_path(config.get("general", {}).get("log_path", str(DEFAULT_LOG_PATH)))

    if args.log:
        print_log_tail(log_path)
        return 0

    ctx = Context(
        config=config,
        config_path=config_path,
        log_path=log_path,
        dry_run=dry_run,
        yes=args.yes,
        quiet=args.quiet,
    )
    check_dependencies(ctx)

    phase_static, phase_old, phase_interactive = selected_phases(args)
    override_paths = [expand_path(raw) for raw in args.paths]

    if phase_static:
        clean_static(ctx)
    if phase_old:
        analyze_old_files(ctx, scan_override=override_paths or None)
    if phase_interactive:
        targets = override_paths or [HOME / "Downloads"]
        print_section(ctx, "== Phase 3: Interactive review ==")
        fzf_review(ctx, manual_interactive_entries(targets))

    print_final_summary(ctx)
    return 0 if ctx.stats.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
