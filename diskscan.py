#!/usr/bin/env python3
"""
diskscan - a SpaceSniffer-style disk usage index with an agent-friendly CLI.

The whole design goal is CONTEXT FRUGALITY: a scan can contain hundreds of
thousands of files, so we never print the tree. We store everything in a
SQLite index once, then answer small, bounded, aggregated queries against it.
Claude Code drives this by drilling down one level at a time.

Commands
  scan PATH            Walk a folder/drive and (re)build the index.
  top [PATH]           Largest items directly inside PATH (one level only).
  find                 Filter files by extension / size / age / location.
  junk                 Apply junk heuristics; print a tiny ranked summary.
  plan                 Write a deletion manifest (paths + sizes) to a file.
  sweep                Act on a manifest. Dry-run by default; --apply quarantines.

Run `diskscan.py <command> -h` for per-command options.
"""

import argparse
import os
import sqlite3
import sys
import time
import shutil
import datetime

DEFAULT_DB = "diskindex.db"
BATCH = 20000  # rows per executemany flush during scan

# Directory names commonly safe to reclaim. Conservative + clearly categorised.
# These are *candidates* for human review, never auto-deleted.
JUNK_DIRS = {
    "node_modules": "build", "target": "build", "dist": "build", "build": "build",
    ".next": "build", ".nuxt": "build", "__pycache__": "build", ".dart_tool": "build",
    ".gradle": "cache", ".nuget": "cache", ".cache": "cache", "Caches": "cache",
    ".pnpm-store": "cache", ".yarn-cache": "cache", ".terraform": "build",
    ".venv": "build", "venv": "build", "Pods": "build",
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024


def parse_size(s):
    """'1GB' '500mb' '10k' '2048' -> bytes."""
    s = s.strip().lower()
    mult = 1
    for suffix, m in (("tb", 1024**4), ("gb", 1024**3), ("mb", 1024**2),
                      ("kb", 1024), ("t", 1024**4), ("g", 1024**3),
                      ("m", 1024**2), ("k", 1024), ("b", 1)):
        if s.endswith(suffix):
            mult = m
            s = s[: -len(suffix)]
            break
    return int(float(s) * mult)


def parse_age(s):
    """'6mo' '2y' '30d' '4w' -> seconds."""
    s = s.strip().lower()
    table = (("mo", 30 * 86400), ("y", 365 * 86400), ("w", 7 * 86400),
             ("d", 86400), ("h", 3600))
    for suffix, m in table:
        if s.endswith(suffix):
            return float(s[: -len(suffix)]) * m
    return float(s) * 86400  # bare number = days


def win_long(path):
    """Prefix Windows paths so >260-char paths still work."""
    if os.name == "nt":
        p = os.path.abspath(path)
        if not p.startswith("\\\\?\\"):
            if p.startswith("\\\\"):
                return "\\\\?\\UNC\\" + p[2:]
            return "\\\\?\\" + p
    return path


def connect(db):
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def require_index(con):
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='files'")
    if not cur.fetchone():
        sys.exit("No index found. Run:  diskscan.py scan <PATH>  first.")


def get_root(con):
    row = con.execute("SELECT value FROM meta WHERE key='root'").fetchone()
    return row[0] if row else None

# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

def cmd_scan(args):
    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        sys.exit(f"Not a directory: {root}")

    if os.path.exists(args.db):
        os.remove(args.db)
    con = connect(args.db)
    con.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE files(
            path   TEXT PRIMARY KEY,
            parent TEXT,
            name   TEXT,
            ext    TEXT,
            is_dir INTEGER,
            size   INTEGER,        -- own size (file bytes; dir = 0)
            subtree INTEGER,       -- recursive size (dir); = size for files
            mtime  REAL,
            depth  INTEGER
        );
        """
    )

    sep = os.sep
    root_depth = root.rstrip(sep).count(sep)
    dir_total = {}     # path -> recursive size, popped as we ascend
    dir_rows = []
    file_batch = []
    n_files = n_dirs = n_err = 0
    t0 = time.time()

    # Progress ticker: one rewritable line on stderr, throttled to ~4/sec so it
    # never bottlenecks the walk. stderr keeps it out of stdout (which other
    # commands parse). Silenced with --quiet or when stderr isn't a terminal.
    show_progress = not args.quiet and sys.stderr.isatty()
    last_tick = [0.0]

    def tick(force=False):
        if not show_progress:
            return
        now = time.time()
        if not force and now - last_tick[0] < 0.25:
            return
        last_tick[0] = now
        rate = n_files / (now - t0) if now > t0 else 0
        sys.stderr.write(
            f"\rscanning… {n_files:,} files  {n_dirs:,} folders  "
            f"{n_err} errors  {rate:,.0f} files/s")
        sys.stderr.flush()

    def onerr(e):
        nonlocal n_err
        n_err += 1

    # topdown=False guarantees children are visited before their parent,
    # so a folder's subtree size is just its own files + children totals.
    for dirpath, dirnames, filenames in os.walk(win_long(root), topdown=False, onerror=onerr):
        real_dir = dirpath
        if os.name == "nt" and real_dir.startswith("\\\\?\\"):
            real_dir = real_dir[4:].replace("UNC\\", "\\\\", 1) if real_dir.startswith("\\\\?\\UNC\\") else real_dir[4:]

        direct = 0
        for fn in filenames:
            full = os.path.join(real_dir, fn)
            try:
                st = os.stat(win_long(full))
            except OSError:
                n_err += 1
                continue
            sz = st.st_size
            direct += sz
            ext = os.path.splitext(fn)[1].lower()
            file_batch.append((full, real_dir, fn, ext, 0, sz, sz, st.st_mtime,
                               full.rstrip(sep).count(sep) - root_depth))
            n_files += 1
            if len(file_batch) >= BATCH:
                con.executemany("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)", file_batch)
                file_batch.clear()
                tick()

        sub = 0
        for d in dirnames:
            sub += dir_total.pop(os.path.join(real_dir, d), 0)
        total = direct + sub
        dir_total[real_dir] = total
        name = os.path.basename(real_dir.rstrip(sep)) or real_dir
        try:
            mt = os.stat(win_long(real_dir)).st_mtime
        except OSError:
            mt = 0.0
        dir_rows.append((real_dir, os.path.dirname(real_dir.rstrip(sep)), name, "",
                         1, 0, total, mt, real_dir.rstrip(sep).count(sep) - root_depth))
        n_dirs += 1
        tick()

    if file_batch:
        con.executemany("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)", file_batch)
    con.executemany("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?,?)", dir_rows)

    con.executescript(
        """
        CREATE INDEX idx_parent  ON files(parent);
        CREATE INDEX idx_ext     ON files(ext);
        CREATE INDEX idx_size    ON files(size);
        CREATE INDEX idx_subtree ON files(subtree);
        CREATE INDEX idx_name    ON files(name);
        """
    )
    con.executemany("INSERT INTO meta VALUES(?,?)", [
        ("root", root),
        ("scanned_at", datetime.datetime.now().isoformat(timespec="seconds")),
        ("files", str(n_files)), ("dirs", str(n_dirs)), ("errors", str(n_err)),
    ])
    con.commit()
    con.close()

    if show_progress:
        sys.stderr.write("\r\033[K")  # clear the ticker line before the summary
        sys.stderr.flush()

    total_sz = dir_total.get(root, 0)
    print(f"Indexed {n_files:,} files, {n_dirs:,} folders "
          f"({human(total_sz)}) in {time.time()-t0:.1f}s. "
          f"{n_err} access errors. -> {args.db}")

# ---------------------------------------------------------------------------
# top  (one-level drill-down)
# ---------------------------------------------------------------------------

def cmd_top(args):
    con = connect(args.db)
    require_index(con)
    path = os.path.abspath(args.path) if args.path else get_root(con)
    where = "parent = ?"
    params = [path]
    if args.dirs_only:
        where += " AND is_dir = 1"
    elif args.files_only:
        where += " AND is_dir = 0"
    rows = con.execute(
        f"SELECT path, is_dir, subtree, size FROM files WHERE {where} "
        f"ORDER BY (CASE WHEN is_dir THEN subtree ELSE size END) DESC LIMIT ?",
        params + [args.limit],
    ).fetchall()
    if not rows:
        print(f"(nothing indexed directly under {path})")
        return
    print(f"# inside {path}")
    for p, is_dir, subtree, size in rows:
        eff = subtree if is_dir else size
        kind = "DIR " if is_dir else "file"
        print(f"{human(eff):>9}  {kind}  {p}")
    con.close()

# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------

def cmd_find(args):
    con = connect(args.db)
    require_index(con)
    where = ["is_dir = 0"]
    params = []
    if args.ext:
        exts = [e if e.startswith(".") else "." + e for e in args.ext.lower().split(",")]
        where.append("ext IN (%s)" % ",".join("?" * len(exts)))
        params += exts
    if args.bigger:
        where.append("size >= ?")
        params.append(parse_size(args.bigger))
    if args.older:
        where.append("mtime <= ?")
        params.append(time.time() - parse_age(args.older))
    if args.under:
        where.append("path LIKE ?")
        params.append(os.path.abspath(args.under).rstrip(os.sep) + os.sep + "%")
    rows = con.execute(
        "SELECT path, size, mtime FROM files WHERE %s ORDER BY size DESC LIMIT ?"
        % " AND ".join(where),
        params + [args.limit],
    ).fetchall()
    if not rows:
        print("(no matches)")
        return
    for p, size, mtime in rows:
        age = datetime.date.fromtimestamp(mtime).isoformat() if mtime else "????-??-??"
        print(f"{human(size):>9}  {age}  {p}")
    con.close()

# ---------------------------------------------------------------------------
# junk
# ---------------------------------------------------------------------------

def _junk_dir_matches(con, under):
    names = list(JUNK_DIRS)
    q = "SELECT path, name, subtree FROM files WHERE is_dir=1 AND name IN (%s)" % ",".join("?" * len(names))
    params = list(names)
    if under:
        q += " AND path LIKE ?"
        params.append(os.path.abspath(under).rstrip(os.sep) + os.sep + "%")
    rows = con.execute(q, params).fetchall()
    # Drop nested matches (e.g. node_modules inside node_modules) so sizes
    # aren't double-counted. Keep the shallowest, skip its descendants.
    rows.sort(key=lambda r: len(r[0]))
    kept = []
    kept_prefixes = []
    for path, name, subtree in rows:
        norm = path.rstrip(os.sep) + os.sep
        if any(norm.startswith(pref) for pref in kept_prefixes):
            continue
        kept.append((path, name, subtree, JUNK_DIRS[name]))
        kept_prefixes.append(norm)
    return kept


def _junk_file_matches(con, under):
    out = []
    base = ""
    if under:
        base = " AND path LIKE '%s'" % (os.path.abspath(under).rstrip(os.sep) + os.sep + "%").replace("'", "''")
    # old installers sitting in Downloads
    rows = con.execute(
        "SELECT path, size FROM files WHERE is_dir=0 AND size>=? "
        "AND (path LIKE '%%\\Downloads\\%%' OR path LIKE '%%/Downloads/%%') "
        "AND ext IN ('.iso','.dmg','.msi','.exe','.pkg','.appx','.zip')" + base,
        (50 * 1024**2,),
    ).fetchall()
    out += [(p, sz, "old-installer") for p, sz in rows]
    # large temp / log / dump files
    rows = con.execute(
        "SELECT path, size FROM files WHERE is_dir=0 AND size>=? "
        "AND ext IN ('.tmp','.log','.dmp','.cache','.bak')" + base,
        (10 * 1024**2,),
    ).fetchall()
    out += [(p, sz, "temp-logs") for p, sz in rows]
    return out


def cmd_junk(args):
    con = connect(args.db)
    require_index(con)
    dirs = _junk_dir_matches(con, args.under)
    files = _junk_file_matches(con, args.under)

    if args.detail:
        items = [(p, sz, cat) for p, n, sz, cat in dirs] + files
        if args.category:
            items = [i for i in items if i[2] == args.category]
        items.sort(key=lambda x: x[1], reverse=True)
        for p, sz, cat in items[: args.limit]:
            print(f"{human(sz):>9}  [{cat}]  {p}")
        return

    # summary: aggregate by (category, label)
    agg = {}
    for p, n, sz, cat in dirs:
        k = (cat, n)
        c, t = agg.get(k, (0, 0))
        agg[k] = (c + 1, t + sz)
    for p, sz, cat in files:
        k = (cat, cat)
        c, t = agg.get(k, (0, 0))
        agg[k] = (c + 1, t + sz)

    grand = sum(t for _, t in agg.values())
    print(f"# junk candidates (review before deleting) — reclaimable ~{human(grand)}")
    for (cat, label), (count, total) in sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True):
        print(f"{human(total):>9}  [{cat:<13}] {label} x{count}")
    print("# use:  junk --detail [--category <cat>]  to list paths")
    con.close()

# ---------------------------------------------------------------------------
# plan  (write a manifest)
# ---------------------------------------------------------------------------

def cmd_plan(args):
    con = connect(args.db)
    require_index(con)
    dirs = _junk_dir_matches(con, args.under)
    files = _junk_file_matches(con, args.under)
    items = [(p, sz, cat) for p, n, sz, cat in dirs] + files
    cats = set(args.category.split(",")) if args.category else None
    if cats:
        items = [i for i in items if i[2] in cats]
    items.sort(key=lambda x: x[1], reverse=True)

    total = sum(sz for _, sz, _ in items)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(f"# diskscan deletion manifest  {datetime.datetime.now().isoformat(timespec='seconds')}\n")
        f.write(f"# {len(items)} items, ~{human(total)}\n")
        for p, sz, cat in items:
            f.write(f"{sz}\t{cat}\t{p}\n")
    print(f"Wrote {len(items)} candidates (~{human(total)}) to {args.out}")
    print("Review it, then:  diskscan.py sweep --manifest %s        (dry run)" % args.out)
    print("                  diskscan.py sweep --manifest %s --apply (quarantine)" % args.out)
    con.close()

# ---------------------------------------------------------------------------
# sweep  (act on a manifest — safe by default)
# ---------------------------------------------------------------------------

def cmd_sweep(args):
    items = []
    with open(args.manifest, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                items.append((int(parts[0]), parts[1], parts[2]))

    total = sum(sz for sz, _, _ in items)
    if not args.apply:
        print(f"DRY RUN — would remove {len(items)} items (~{human(total)}). Nothing changed.")
        for sz, cat, p in items[:15]:
            print(f"  {human(sz):>9}  [{cat}]  {p}")
        if len(items) > 15:
            print(f"  ... and {len(items)-15} more")
        print("Re-run with --apply to move them to quarantine.")
        return

    quarantine = os.path.abspath(args.quarantine or
                                 "_quarantine_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(quarantine, exist_ok=True)
    log = os.path.join(quarantine, "restore.log")
    moved = freed = 0
    qnorm = quarantine.rstrip(os.sep) + os.sep
    with open(log, "w", encoding="utf-8") as lg:
        for sz, cat, p in items:
            if (p.rstrip(os.sep) + os.sep).startswith(qnorm):
                continue  # never eat our own quarantine
            if not os.path.exists(win_long(p)):
                continue
            dest = os.path.join(quarantine, p.replace(":", "").lstrip("\\/"))
            os.makedirs(os.path.dirname(win_long(dest)), exist_ok=True)
            try:
                shutil.move(win_long(p), win_long(dest))
                lg.write(f"{dest}\t{p}\n")
                moved += 1
                freed += sz
            except OSError as e:
                print(f"  skip (in use / locked): {p}  ({e})")
    print(f"Moved {moved} items (~{human(freed)}) to {quarantine}")
    print(f"Restore log: {log}  (move entries back to undo). Delete the folder when satisfied.")

# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog="diskscan", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help=f"index file (default {DEFAULT_DB})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="build the index")
    s.add_argument("path")
    s.add_argument("--quiet", action="store_true", help="suppress the progress ticker")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("top", help="largest items one level under PATH")
    s.add_argument("path", nargs="?")
    s.add_argument("-n", "--limit", type=int, default=25)
    s.add_argument("--dirs-only", action="store_true")
    s.add_argument("--files-only", action="store_true")
    s.set_defaults(func=cmd_top)

    s = sub.add_parser("find", help="filter files")
    s.add_argument("--ext", help="comma list, e.g. .iso,.mp4")
    s.add_argument("--bigger", help="e.g. 1GB")
    s.add_argument("--older", help="e.g. 6mo, 2y, 30d")
    s.add_argument("--under", help="limit to a subtree")
    s.add_argument("-n", "--limit", type=int, default=25)
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("junk", help="ranked junk candidates")
    s.add_argument("--under")
    s.add_argument("--detail", action="store_true", help="list paths instead of summary")
    s.add_argument("--category", help="filter detail to one category")
    s.add_argument("-n", "--limit", type=int, default=40)
    s.set_defaults(func=cmd_junk)

    s = sub.add_parser("plan", help="write a deletion manifest")
    s.add_argument("--under")
    s.add_argument("--category", help="comma list of categories to include")
    s.add_argument("--out", default="manifest.txt")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("sweep", help="act on a manifest (dry-run unless --apply)")
    s.add_argument("--manifest", required=True)
    s.add_argument("--apply", action="store_true")
    s.add_argument("--quarantine", help="quarantine folder (default timestamped)")
    s.set_defaults(func=cmd_sweep)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
