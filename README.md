# diskscan

A SpaceSniffer-style disk-usage indexer with an **agent-friendly** CLI.

`diskscan.py` walks a folder or drive once, stores the entire tree in a local
SQLite index, and then answers **small, bounded, aggregated** queries against
it. The design goal is *context frugality*: a scan can contain hundreds of
thousands of files, so the tool never prints the tree. You (or an AI assistant
like Claude Code) drill down one level at a time, find the bloat, and reclaim
it — safely and reversibly.

It has **no third-party dependencies** — just Python 3 and its standard library.

## Why

GUI tools like SpaceSniffer are great for humans but useless to an LLM driving a
terminal: dumping a 1M-file tree blows the context window. `diskscan` solves
that by keeping the full tree in SQLite and exposing only top-N, filtered, and
category-aggregated views — each one a handful of lines.

## Install

```sh
git clone https://github.com/fimilfaneea/SpaceSniffer.git
cd SpaceSniffer
python diskscan.py --help
```

Python 3.8+ is all you need.

## The workflow

```sh
# 1. Index a drive/folder once (re-run to refresh). Builds diskindex.db.
python diskscan.py scan C:\

# 2. See the biggest items at the root, then drill one level at a time.
python diskscan.py top
python diskscan.py top "C:\Users\me\AppData\Local"

# 3. Get a tiny ranked summary of regenerable "junk" candidates.
python diskscan.py junk
python diskscan.py junk --detail --category build

# 4. Write a deletion manifest (review it!).
python diskscan.py plan --category build,cache --out manifest.txt

# 5. Dry run — prints what would move, changes nothing.
python diskscan.py sweep --manifest manifest.txt

# 6. Apply — moves items to a timestamped quarantine folder with a restore log.
#    Reversible; nothing is hard-deleted.
python diskscan.py sweep --manifest manifest.txt --apply
```

## Commands

| Command | What it does |
|---|---|
| `scan PATH` | Walk a folder/drive and (re)build the SQLite index. |
| `top [PATH]` | Largest items directly inside `PATH` (one level only). |
| `find` | Filter files by `--ext`, `--bigger`, `--older`, `--under`. |
| `junk` | Apply junk heuristics; print a tiny ranked summary (`--detail` lists paths). |
| `plan` | Write a deletion manifest (paths + sizes) to a file. |
| `sweep` | Act on a manifest. Dry-run by default; `--apply` quarantines. |

Run `python diskscan.py <command> -h` for per-command options.

Targeted `find` examples:

```sh
python diskscan.py find --ext .mp4,.iso --bigger 1GB
python diskscan.py find --older 6mo --bigger 100MB --under "C:\Users\me\Downloads"
```

## How it's safe

- `sweep` is a **dry run** unless you pass `--apply`.
- `--apply` **moves** items into a quarantine folder and writes a `restore.log`
  (`quarantine_path \t original_path` per line). Nothing is hard-deleted — move
  entries back to undo.
- Deleting the quarantine folder is the **only irreversible step**, and it's
  always the user's call.

### `junk` is a starting point, not a verdict

The `junk` heuristic matches a fixed set of directory *names* (`node_modules`,
`dist`, `build`, `.venv`, `__pycache__`, caches, …). That means it can both miss
real bloat and flag things that aren't actually disposable:

- **It misses** large tool/package caches that aren't on the name list —
  `npm-cache`, `pip`, `ms-playwright`, `uv`, `go-build`, `node-gyp`,
  `calibre-cache`. Drill `AppData\Local` (or `~/.cache`) and add them yourself.
- **It false-positives** on name matches that belong to installed software:
  editor bundles (`...\Microsoft VS Code\...`), extensions
  (`.vscode\extensions\`, `.cursor\extensions\`), global CLI tools
  (`AppData\Roaming\npm\node_modules`, `nvm\<version>\node_modules`), and Python
  package *source* (`build`/`venv`/`dist` inside `site-packages`). Audit the
  manifest by location and strip these before `--apply`. `__pycache__` is the
  one always-safe match.

A manifest line is just `<size_bytes>\t<category>\t<path>`, so you can hand-edit
it freely — add cache dirs the heuristic missed, delete false positives.

## Windows notes

- Paths use backslashes and drive letters: `top "C:\Users\me"`.
- A quoted path ending in a backslash breaks most shells (`top "C:\"` →
  unbalanced quote). Omit the path to use the stored scan root, or drop the
  trailing `\`.
- Run the terminal **as Administrator** for a complete scan of system folders.
- Put the quarantine on the **same drive** you're cleaning (`--quarantine`) so
  `--apply` does a fast rename instead of a slow cross-drive copy. The space is
  only reclaimed once you delete the quarantine.
- OneDrive/cloud "online-only" files report logical size, not size-on-disk, so a
  folder may look bigger than what deleting it actually frees.

## License

[MIT](LICENSE) © 2026 Fimil Faneea
