# Driving diskscan.py (for Claude Code)

This tool indexes a disk into SQLite and answers **small, bounded** queries.
Never try to read the whole tree — work top-down, one level at a time.

## The loop
1. `python diskscan.py scan C:\` — run once. Builds `diskindex.db`. (Re-run to refresh.)
2. `python diskscan.py top` — biggest items at the root (~25 lines). Pick the fattest.
3. `python diskscan.py top "C:\that\folder"` — drill one level. Repeat until you find the bloat.
4. `python diskscan.py junk` — ranked junk summary (one line per category, tiny).
5. `python diskscan.py junk --detail --category build` — list actual paths for one category.
6. `python diskscan.py plan --category build,cache --out manifest.txt` — write candidates to a file.
7. **Show the user the manifest summary and get explicit approval.**
8. `python diskscan.py sweep --manifest manifest.txt` — dry run (prints, changes nothing).
9. `python diskscan.py sweep --manifest manifest.txt --apply` — moves items to a quarantine
   folder with a restore log. This is reversible; nothing is hard-deleted.

## Context rules
- Prefer `top` / `junk` (aggregated) over `find` with no filters.
- Always keep `-n` small (default 25–40). Don't raise it to dump everything.
- Use `find --ext --bigger --older --under` to target, e.g.
  `find --ext .mp4,.iso --bigger 1GB` instead of listing a whole folder.
- One drill level per step. The DB holds the full tree; you only ever load a slice.

## Reading `junk` output (blind spots + false positives)
`junk` matches a fixed set of directory *names* (see `JUNK_DIRS`). That means it both
misses real bloat and flags things that aren't junk. Always sanity-check before `plan`.

- **It misses big tool/package caches** — these aren't in `JUNK_DIRS` but are large and
  safe (they re-download on demand). After `junk`, also drill `AppData\Local` for:
  `npm-cache`, `pip`, `ms-playwright`, `uv`, `go-build`, `node-gyp`, `calibre-cache`,
  and any `*-cache` dir. Add them to the manifest by hand (a manifest line is
  `<size_bytes>\t<category>\t<path>`; size is only used for reporting).
- **It produces name-based false positives** — a dir called `node_modules`/`dist`/
  `build`/`venv`/`target` is *not* junk when it's part of installed software:
  - App-bundled code: `...\Microsoft VS Code\...`, editor extensions under
    `.vscode\extensions\` and `.cursor\extensions\` — deleting breaks the app/extension.
  - Global CLI tools: `AppData\Roaming\npm\node_modules` and `nvm\<version>\node_modules`
    are globally-installed commands (e.g. `func`, `vercel`, `tsc`), not project deps.
  - Python *source*: `build`/`venv`/`dist` inside a Python install or any
    `site-packages\` is package source code (e.g. the `venv` module, pip's `build`
    folder) — deleting breaks pip/venv. `__pycache__` is the only always-safe match there.
  Audit the manifest by location and strip app-internal / `site-packages` paths before
  `--apply`. When you hand-filter, show the user what you excluded and why.

## Safety rules
- Never call `sweep --apply` without the user approving the manifest first.
- Treat `junk` output as *candidates*, not verdicts. Things like `target/`, `dist/`,
  `.venv` are usually regenerable, but confirm with the user before sweeping.
- After `--apply`, tell the user the quarantine path and that deleting that folder
  is the final, irreversible step — that's their call, not yours.
- Pick the quarantine drive deliberately. The default lands in the CWD; if that's a
  *different* drive than the one scanned, `--apply` does a slow cross-drive **copy**
  (and can fill that drive). Use `--quarantine` on the **same** drive being cleaned for
  a fast rename — but note the space isn't actually freed until the quarantine is deleted.

## Windows notes
- Paths use backslashes and drive letters: `top "C:\Users\me"`.
- Run the terminal as Administrator for a complete scan of system folders.
- OneDrive/cloud "online-only" files report logical size, not size-on-disk, so a
  folder may look bigger than what it actually frees. Flag this if it matters.
- A quoted path ending in a backslash breaks the shell (`top "C:\"` → unbalanced quote).
  Omit the path to use the stored root, or drop the trailing `\`.
- Deleting a folder that sits directly at a drive root (e.g. `C:\quarantine`) may be
  blocked as a protected path. Delete its *contents* instead — that frees the space; the
  empty folder is harmless and the user can remove it from Explorer.
