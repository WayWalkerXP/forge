---
name: forge
description: Working on forge, the audiobook triage/convert/shelve CLI in this repo - its safety invariants, naming templates, test workflow and textual/rich pitfalls. Load before editing forge/ or tests/.
---

# forge

A terminal utility that takes an audiobook from a freshly downloaded file to a
book filed in Audiobookshelf. Python 3.14, no virtual environment, no packaging:
distribution packages and the system interpreter only.

**The governing principle: attended runs decide, unattended runs execute.** The
decision is a tag written inside the audio file
(`----:com.apple.iTunes:FORGE_DISPOSITION` on MP4, `TXXX:FORGE_DISPOSITION` on
ID3), so it survives restarts, folder moves and rebuilds of the tool. An
untagged file is never processed. Nothing happens because forge guessed.

The pipeline runs one way, and the directories are validated at startup so no
stage can discover its own output:

```
source_dir  →  output_dir  →  library_dir
   (triage)     (converted)    (Audiobookshelf watches this)
      ↓
 archive_dir  (the untouched original, kept forever)
```

## Non-negotiable invariants

These are not style preferences. Each one exists because its absence cost
something real. Do not weaken any of them for speed, simplicity or convenience,
and do not offer a faster path that weakens one unless explicitly asked.

1. **Safety over speed, every time.** The user is protective of this library and
   has said so directly. The archive is a deliberate undo of last resort, not
   clutter.
2. **Nothing is ever deleted.** Originals move to the archive. The single
   exception is the source of an `as-is` relocation, and only after a
   byte-for-byte hash comparison of the copy.
3. **Nothing is ever overwritten.** Promotion uses an exclusive create; an
   existing output invalidates the plan; collisions take a ` (2)` … ` (999)`
   suffix. Verify by hash, never by size.
4. **The conversion transaction order is fixed:** convert to a temp file beside
   the final output → validate against the source → copy tags and cover →
   promote → archive the original → stamp `done`. The source is not touched
   until validation and tag-copying have both succeeded, so a failed run is
   always safe to run again.
5. **Tag writes are staged.** Copy the file, mutate the copy, re-open it to
   prove it still parses, then replace the original. mutagen rewrites a whole
   MP4 container when the tag block outgrows its padding; a failure part-way
   through leaves an unplayable file.
6. **Unrecognised disposition values are treated as `hold`.** A tag written by a
   future version must never make an older version do something destructive.
7. **Shelving never guesses.** A duplicate is set aside, not shelved. A library
   that cannot be reached *blocks* the book rather than being read as "not a
   duplicate".
8. **The `_` substitution in library names is load-bearing.** The live library
   was built by `absmove`, whose sanitiser turned every illegal character into
   `_` (`Space: 1969` is on disk as `Space_ 1969`). Anything that normalises
   differently files the same author under a second folder and splits books
   Audiobookshelf currently shows as one. Parity with absmove was verified
   against 600 real books, 0 mismatches. `naming.library_component` is matched
   to what is on disk and is not a matter of taste.
9. **Config validation is strict and happens once, at startup.** A
   misconfigured layout is the one mistake that can quietly destroy the library.
   Fail before anything moves, never half-way through.
10. **Folder books need unanimity.** A disposition is written to every track; if
    the tracks disagree the book is conflicted and is never processed.

## Orientation

Before changing anything, read in this order:

1. `README.md` — the user-facing contract, 662 lines. The sections that matter
   most are *The disposition tag*, *Naming*, *The encoding rules*, and *What
   protects your library*.
2. `config.sample.ini` — every setting, with the reasoning in comments.
3. The module docstring of whatever you are about to edit. They are long and
   they carry the reasoning; the file is not self-evident without them.

## Who owns what

```
forge/
  cli.py          subcommands, _HANDLERS    probe.py     ffprobe → AudioInfo
  config.py       INI loading, validation   ffmpeg.py    subprocess, progress, codecs
  disposition.py  the tag and its meaning   rules.py     the encoding decisions
  scan.py         book discovery            plan.py      source + intent → a plan
  convert.py      the executor              validate.py  output verification
  archive.py      promote, archive, move    paths.py     character rules, collisions
  naming.py       ALL naming: templates, sanitising, tag values
  tags.py         mutagen read/write        audible.py   catalogue lookup
  authors.py      author identity registry  titles.py    marketing-phrase stripping
  history.py      change log and undo       settle.py    wait for files to settle
  control.py      interruption handling     lock.py      single-instance lock
  render.py       terminal output           library.py   Audiobookshelf, read-only
  shelve.py       into the library          backfill.py  re-file the existing library
  sorting.py      list orders               errors.py    the exception hierarchy
  tui/            the triage interface (app.py, screens.py, markup.py, + authors/titles apps)
```

Boundaries worth respecting:

- **`naming.py` is the single owner of what a book is called.** Both `forge run`
  and `forge shelve` name files from the same two config templates,
  `shelf_template` and `filename_template`. `shelve`, `plan` and `backfill`
  render them; they do not invent names. `paths.py` keeps only the character
  rules (`INVALID_CHARS`, `WINDOWS_RESERVED`), the collision suffix, and the
  temp/archive path helpers.
- **`plan.py` is pure.** Building a plan touches nothing on disk beyond reading.
  That is what makes `--dry-run` trustworthy. Do not reach for a database or a
  network call from it.
- **`errors.py` encodes blast radius.** `BookError` and its subclasses are
  per-book failures that must not stop a run; anything deriving straight from
  `ForgeError` aborts. Choose the base class deliberately — it decides whether
  one bad book skips or the whole nightly pass dies.
- **`render.py` has two renderers behind one Protocol.** `RichRenderer` for a
  terminal, `PlainRenderer` for `cron.log`. Add a method to one and you must add
  it to the other.

## Methodologies

### Changing how a book is named

1. Read `forge/naming.py`'s module docstring first — the three optional-part
   rules (empty bracketed run, glue beside an empty tag, empty shelf component)
   are specified there and are easy to break.
2. Decide which of the two failure modes you are in, and do not blur them:
   - A bad **template** (illegal literal character, unknown tag, unclosed `<`,
     could only ever render empty) is a `ConfigError`, fatal at startup, exit 3,
     nothing moved. Never sanitise a template silently.
   - An illegal character in a **tag value** is normalised to `_` silently.
     Erroring would refuse most of the library; warning would fire on nearly
     every book. This is settled — do not relitigate it.
   - A valid template that names nothing *this book* has is `naming.EmptyName`, a
     `BookError`: skip the one book with a reason, because the fix is the tags.
3. Add new tags to `TEMPLATE_TAGS` and to the `tag_values` dict together.
   `config.sample.ini`, the README tag list and `forge naming` all derive from
   `TEMPLATE_TAGS`, so they stay correct for free.
4. Test the missing-value combinations, not just the full shape. Most books have
   no series and plenty have no ASIN.
5. If library-visible names change, say so: already-shelved books are untouched
   and `forge backfill` is the tool that re-files them.

### Adding or changing a config setting

1. Add the field to the `Config` dataclass with a default that preserves current
   behaviour for an install whose INI predates the setting.
2. Parse it in `load()` with that same default as the `fallback`.
3. Validate it in `validate()`, before any directory is created, with a message
   naming the `[section] key` the user has to edit.
4. Document it in `config.sample.ini` with the reasoning, not just the syntax.
5. Add the override to `tests/helpers.write_config` only if a test needs it, and
   keep it omitted by default so the rest of the suite exercises the defaults.

### Adding a CLI subcommand

1. `build_parser()` for the parser, a `cmd_<name>` function, and an entry in
   `_HANDLERS`. All three, or the command silently does not exist.
2. Pass `parents=[shared]` so `-c/--config`, `--plain` and `--no-color` work.
   Note the known trap: subparsers sharing options via `parents` parse second
   and overwrite what the main parser stored — this is how a test run once
   reached the real library.
3. Import heavyweight modules *inside* the function, as the existing commands
   do, so `forge --help` stays fast.
4. Exit codes are a contract with `bin/forge-cron`: `0` ok, `1` failures, `2`
   usage, `3` configuration, `4` already locked, `130` interrupted.

### Touching the TUI

textual fails *silently* here more often than it raises. Before writing widget
code, read the list below and the guard tests it refers to in
`tests/test_tui.py` (`ScreenConstruction`, `ScreenLayout`,
`ModalMessageIsolation`, `WorkerIsolation`, `ActivityIndicator`).

- Check every attribute and method name against the base class MRO. `self.region`
  and `self.visible` are read-only properties; a method named `_render` shadows
  `Widget._render()` and breaks layout for the whole app with an error naming
  neither the widget nor the method.
- Scope every `@on` handler with a selector. Unscoped, it also receives messages
  bubbling out of modal screens, and an app-level widget lookup then raises
  `NoMatches` and takes the app down.
- `Horizontal`/`Vertical` default to `height: 1fr`. Assert
  `box.content_region.contains_region(widget.region)`, not `size.height > 0` — a
  size assertion passes while the widget sits off screen.
- Escape every string you did not write before it reaches markup.
  `forge/tui/markup.py: literal()` for textual (its own `markup.escape` is not
  enough: the parser rejects `[2026]`, which escape leaves alone), and
  `render._plain()` for rich. **rich does not raise on an unknown tag, it
  removes it** — `[Dramatized Adaptation]` vanishes from a table cell with no
  error at all.
- Give every `@work` an explicit, distinct `group=`. All of an App's workers
  share the App node, so `exclusive=True` in the default group means a rescan
  cancels an in-flight tag write.

## Running things

```bash
python3 -m unittest discover -s tests -t .          # whole suite, ~165s, 791 tests
python3 -m unittest tests.test_naming -q            # one module, sub-second
```

- **No virtual environment, ever.** `sudo apt install ffmpeg python3-mutagen
  python3-requests python3-rich python3-textual`. The tests themselves need only
  the standard library; audio fixtures are synthesised with ffmpeg and MP3
  fixtures are built from hand-written frame headers, since not every ffmpeg
  build ships an MP3 *encoder*. Gate anything needing the binaries with
  `@requires_ffmpeg`.
- **Run targeted modules while iterating, then the full suite before reporting
  done.** The TUI modules account for most of the 165 seconds; run them in the
  background rather than blocking on them.
- **Never run `forge run` or `forge shelve` against the real config.** Build a
  scratch config with `tests.helpers.write_config`, point `FORGE_CONFIG` at it,
  and pass `-n/--dry-run` first — then *read back the paths it printed* to prove
  where it is aimed. Do not assume `-c` took effect; that exact assumption
  started converting a book in the live library once.
- `forge naming` prints the current templates, the available tags and a worked
  example; give it a file and it names that book. Use it to check a template
  without running a pass.

## Code conventions

Match the surrounding code; it is consistent and deliberate.

- `from __future__ import annotations` at the top of every module, then stdlib,
  then `from . import x`, then `from .x import y`.
- Modern typing: `str | None`, `list[Path]`, `Sequence`, no `Optional`.
- Frozen dataclasses for anything carried between stages (`Plan`, `Book`,
  `ShelvePlan`, `Config`); `Enum`/`StrEnum` for closed sets of values.
- `LOGGER = logging.getLogger(__name__)` in every module.
- Module docstrings are long and explain *why the module exists* and what it
  refuses to do. Keep them current; they are the primary documentation.
- **Comments explain the failure that motivated the code**, concretely, usually
  with a real example from this library. Not "sanitise the name" but "a
  300-character subtitle produced a name no filesystem would accept and the
  move failed outright". Do not add comments that restate the code.
- Prose in code and comments uses ` - `, not an em dash, and `->` in examples.
- Tests: one module per source module, long sentence-shaped method names
  (`test_no_series_means_no_series_folder_and_no_part_prefix`), and a class or
  module docstring saying *why the test matters* when it guards something
  load-bearing. Verify a new guard test by reintroducing the bug and confirming
  it fails — the first TUI layout test written here passed with the bug present.

## Commits

Imperative subject line, no prefix, no conventional-commits. The body is prose
explaining the real-world problem the change solves, with concrete examples from
the live library and anything a test caught. End with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

Commit only when asked.

## Do not

- Do not add a dependency, a virtual environment, a `pyproject.toml`, or a
  packaging step.
- Do not introduce a second naming convention, or compute a path outside
  `naming.py`.
- Do not write to Audiobookshelf. `library.py` is read-only, by design.
- Do not relax the settle checks, the lock, the disk-space pre-flight or the
  validation tolerances (channels exactly, bitrate within tolerance, duration
  within 1% or 2 seconds, chapter count exactly) to make a run finish.
- Do not use `-map_metadata` and assume tags survive. ffmpeg's mp4 muxer writes
  only the atoms it knows, so every custom one — series, series-part, ASIN,
  subtitle, publisher, language, explicit — is lost. forge re-applies them with
  mutagen after validation, and writes series part both as `series-part` (which
  Audiobookshelf reads) and `SERIES_SEQUENCE` (which other tools read).
  Audiobookshelf **never** reads `SERIES_SEQUENCE`; this is confirmed from its
  own `prober.js`.
