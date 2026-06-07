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

## Safety rules
- Never call `sweep --apply` without the user approving the manifest first.
- Treat `junk` output as *candidates*, not verdicts. Things like `target/`, `dist/`,
  `.venv` are usually regenerable, but confirm with the user before sweeping.
- After `--apply`, tell the user the quarantine path and that deleting that folder
  is the final, irreversible step — that's their call, not yours.

## Windows notes
- Paths use backslashes and drive letters: `top "C:\Users\me"`.
- Run the terminal as Administrator for a complete scan of system folders.
- OneDrive/cloud "online-only" files report logical size, not size-on-disk, so a
  folder may look bigger than what it actually frees. Flag this if it matters.
