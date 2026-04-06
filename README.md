# cleanup

Smart Arch Linux cleanup CLI with static cleanup, old-file analysis, and interactive `fzf` review.

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![Platform Arch Linux](https://img.shields.io/badge/platform-Arch%20Linux-1793D1)
![License MIT](https://img.shields.io/badge/license-MIT-green)

`cleanup` is a practical terminal tool for reclaiming disk space without losing control. It combines fast category-based cleanup, age analysis for forgotten large files, and an interactive `fzf` review step before deletion.

## Why cleanup

- Clean common disk hogs quickly without memorizing package-manager or cache commands
- Review old or duplicate files before deleting them
- Keep risky actions visible with prompts, dry-run mode, and logging
- Work well on a normal Arch Linux user setup without requiring root by default

## Features

- Static cleanup for caches, logs, trash, junk files, broken symlinks, empty dirs, and more
- Old-file analysis based on `atime`/`mtime`, size thresholds, and extension excludes
- Interactive review with `fzf` before deleting old or duplicate files
- Dry-run mode for every destructive action
- Deletion log with timestamp, path, size, and reason
- Auto-generated config at `~/.config/cleanup/config.toml`

## Requirements

- Python 3.12+
- Arch Linux
- Optional external tools:
  - `fzf`
  - `paccache`
  - `journalctl`
  - `fdupes`
  - `dust`

Missing tools are detected at runtime and related features are skipped with a warning.

## Install

Clone the repo or copy the script, then make it executable:

```bash
chmod +x cleanup.py
```

Optional: place it in your `PATH` as `cleanup`.

## Usage

Quick examples:

```bash
# full run
./cleanup.py

# preview only
./cleanup.py --dry-run

# analyze old files in a custom directory
./cleanup.py --old ~/Projects
```

Run all default phases:

```bash
./cleanup.py
```

Dry run:

```bash
./cleanup.py --dry-run
```

Run only static cleanup:

```bash
./cleanup.py --static
```

Run only old-file analysis:

```bash
./cleanup.py --old
```

Run only interactive review:

```bash
./cleanup.py --interactive
```

Scan a specific directory in Phase 2:

```bash
./cleanup.py --old ~/Projects
```

Show recent log entries:

```bash
./cleanup.py --log
```

## CLI Options

```text
cleanup [OPTIONS] [PHASES] [PATHS...]

Options:
  -n, --dry-run       Show what would be deleted
  -y, --yes           Auto-confirm prompts except pacman orphans
  -q, --quiet         Only print summary at end
  --config PATH       Custom config file path
  --log               Print last 20 log entries and exit

Phase selectors:
  --static            Run Phase 1 only
  --old               Run Phase 2 only
  --interactive       Run Phase 3 only
```

## Config

On first run, `cleanup` creates:

```text
~/.config/cleanup/config.toml
```

Default config:

```toml
[general]
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
```

## Logging

All destructive actions are logged to:

```text
~/.local/share/cleanup/cleanup.log
```

Each entry includes:

- Timestamp
- Path
- Size
- Reason

## Notes

- No root is required by default
- `sudo` is used for `paccache` and can be requested on permission errors during deletion
- If `noatime` is detected for your home partition, old-file analysis falls back to `mtime`

## Roadmap

- [ ] Add optional `--sudo` mode for non-interactive privileged cleanup
- [ ] Add richer size estimation for `paccache` and journal cleanup results
- [ ] Add ignore/include patterns for manual interactive mode
- [ ] Add exportable report output (`json` or `csv`)
- [ ] Add shell completion for `bash`, `zsh`, and `fish`
- [ ] Add lightweight test fixtures for safe local validation

## License

MIT
