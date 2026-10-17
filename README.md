# forge

A terminal utility for triaging, converting and shelving audiobook files (m4b
and mp3): from a freshly downloaded file to a book filed in Audiobookshelf.

Attended runs **decide**; unattended runs **execute**. The decision is recorded
as a tag inside the audio file itself, so it survives restarts, folder moves and
rebuilds of this tool, and can be inspected or corrected in MP3Tag or `ffprobe`.

An untagged file is never processed. Nothing happens because forge guessed.

## The disposition tag

Stored as `----:com.apple.iTunes:FORGE_DISPOSITION` (MP4) or
`TXXX:FORGE_DISPOSITION` (ID3).

| Value | Meaning | Output | Original |
|---|---|---|---|
| *(absent)* | not yet triaged | — | untouched |
| `downcode` | re-encode with the standard rules | output dir | → archive |
| `drama` | re-encode, keeping stereo for a full-cast production | output dir | → archive |
| `remux` | repackage as m4b without re-encoding the audio | output dir | → archive |
| `as-is` | already correct; relocate only | output dir | consumed |
| `hold` | looked at, deliberately left alone | — | untouched |
| `needs-attention` | forge tried and could not proceed | — | untouched |
| `done` | written onto outputs so they are never reprocessed | — | — |

Any value forge does not recognise is treated as `hold`. A tag written by a
future version can never make an older version do something destructive.

Disposition records **intent only**. Bitrate and channels come from the rules at
conversion time, so retuning the rules applies to every already-triaged book.

## Install

Everything comes from distribution packages. There is no virtual environment,
so an unattended run has nothing to activate.

```
sudo apt install ffmpeg python3-mutagen python3-requests python3-rich python3-textual
```

`libfdk_aac` is preferred and `aac` is the fallback; forge probes for what your
ffmpeg build actually provides rather than assuming.

Then copy the sample configuration and edit the paths:

```
mkdir -p ~/.config/forge
cp config.sample.ini ~/.config/forge/config.ini
```

`output_dir` and `archive_dir` must not live inside `source_dir`. forge refuses
to start otherwise: a run that can discover its own outputs will convert them
again.

## Use

```
forge triage [DIR]           interactive triage. Reads, edits and tags. Never converts.
forge run [PATHS...]         the executor. No paths means everything tagged.
  -n, --dry-run              show what would happen, change nothing
  -d, --disposition LIST     restrict to given dispositions
  --limit N                  stop after N books
forge list                   show the books in the source directory
forge tags show|set PATHS    non-interactive tag inspection and editing
forge disposition PATHS      read, or set with -s
forge lookup TITLE|--asin    query Audible; --apply FILE writes the result
forge history                recent change groups and their ids
  PATH                       one file, field by field
  --stats                    database summary
forge undo GROUP_ID          put that group's previous tag values back
  -n, --dry-run              show what would be restored, change nothing
```

### Undoing a tag change

Every tag write forge makes — in triage, from an Audible lookup, from
`forge tags set` — is recorded as a **change group**. `forge history` lists them
newest first, and the number in brackets is what `forge undo` takes:

```
$ forge history
[7] 2026-10-12 13:39  ASIN 1528866738  (1 field(s))   Ironclads.m4b
[6] 2026-10-11 18:56  ASIN 1528866738  (10 field(s))  Ironclads.m4b
[5] 2026-10-11 18:51  ASIN B0GY23SNTX  (11 field(s))  1453.m4b

$ forge history "/path/to/Ironclads.m4b"      # field by field
$ forge undo 6 -n                             # preview
$ forge undo 6                                # asks before writing
```

`forge undo` with no id lists the ids rather than undoing anything. One edit is
one group however many files it touched, so undoing a folder book's tagging
restores every track in one step. Cover art is recorded but not restorable — the
database stores a hash and a size, not the image.

### Triage

| key | action |
|---|---|
| `space` | set the disposition (every marked book, if any are marked) |
| `x` | clear the disposition (likewise) |
| `m` | mark the selected book and move down |
| `M` | mark every book the filter is showing, or unmark them |
| `U` | clear every mark |
| `e` | edit tags |
| `a` | Audible lookup |
| `d` | check this book against the Audiobookshelf library |
| `D` | check every book that has an ASIN |
| `/` | search title, author, series or ASIN |
| `f` | cycle filters (all / untriaged / attention / blocked / ready / duplicate / new-author) |
| `s` | sort the list |
| `r` | rescan |
| `q` | quit (waits for anything still running) |

The list shows **disposition** and **readiness** in separate columns, because
the state worth noticing is a disposition set on a book that cannot be converted
— forge will defer that book rather than obey blindly. Readiness is computed on
every refresh, so it can never go stale against the file.

The bottom row carries two things: `Last action:` on the left, the result of
whatever you did most recently, and an activity indicator on the right. It reads
a dim `[Idle]` when nothing is running, and an accented `[⠙ Scanning directory]`,
`[⠹ Writing tags]`, `[⠸ Writing disposition]` or `[⠼ Saving tag snapshot]` while
work is in flight. The spinner animates, which is the part that distinguishes
slow work from a wedged interface — scanning a large directory and writing tags
into a multi-gigabyte container both take long enough to look like a hang.

### Setting a disposition on several books at once

`m` marks the selected book and moves down, so marking a run of them is one
keystroke each. `M` marks everything the current filter is showing — or unmarks
it, if it is all marked already — and `U` clears every mark. Marked rows carry a
`✓` in the `sel` column.

With anything marked, `space` and `x` apply to the marked books rather than to
the one under the cursor. The disposition dialog says so (`Set disposition — 7
marked books`) and, because the choice is one keystroke away from an accidental
`M`, a batch write stops to confirm and names what it is about to touch. One
marked book is not a batch: it writes straight away, like `space` always has.

A few details chosen deliberately:

* **Marks are keys, not rows**, so they survive a re-sort, a filter change and a
  rescan. A mark on a book that has since left the directory is dropped.
* **A filter does not narrow a batch.** If marks are hidden by the current
  filter they are still written to, and the status line says how many are out of
  sight, because a mark is an explicit instruction.
* **One bad file does not stop the run.** The books that could be tagged are,
  the failures keep their marks so they can be retried, and the status line
  names the first failure.
* **A completed batch clears the marks it acted on**, since a stale mark is what
  would make the *next* `space` write the wrong thing.

### Ordering the list

`s` opens the sort dialog. A number or the arrow keys picks the column, `r`
reverses the direction, `enter` applies it and `escape` leaves the list alone.
The dialog opens on whatever the list is sorted by now, so reversing an existing
sort is `s`, `r`, `enter`.

| order | by |
|---|---|
| `triage` | needs attention first, then by name — how the list opens |
| `title` | album or title, falling back to the file name |
| `author` | author, then title within an author |
| `series` | series, then number within a series |
| `name` | the name on disk |
| `disposition` | untriaged first, then by what forge will do |
| `readiness` | books that cannot be converted first |
| `duplicate` | library duplicates first; unchecked books last |
| `bitrate` | encoded bitrate, lowest first |
| `duration` | shortest first |

Two rules hold in every order. **Blanks sink**: a book with no author, no
series, or no bitrate because it could not be probed goes to the bottom in both
directions, so reversing never promotes the books carrying the least
information. **Ties break by file name**, so the list does not shuffle between
refreshes. The order survives a filter change and a rescan, and the cursor stays
on the book it was on.

### One author, however Amazon spelled them

Amazon's contributor records do not insist on one spelling, so the same author
arrives as `J.N. Chaney`, `J. N. Chaney`, `JN Chaney` and `J N Chaney` —
sometimes two of those from one API call. Audiobookshelf indexes the author
*tag*, so each spelling becomes its own shelf there, and the library tree grows
a folder per spelling to match. One author in this library had 31 folders and
245 books.

What makes it tractable is that **Audible gives every contributor an ASIN, and
that ASIN does not vary**:

```
tag "JN Chaney"      → B00W2ZAK7E
tag "J. N. Chaney"   → B00W2ZAK7E
tag "J.N. Chaney"    → B00W2ZAK7E
```

So identity is a lookup, not a guess. `forge/authors.py` keeps the answers in
its own SQLite file (`state_dir/authors.sqlite3`): an `author` table of
identities, an `author_alias` table mapping every spelling ever seen to one of
them, and an `author_asin` table holding every Audible contributor id that
turned out to be the same person — because Amazon issues several. It keeps
three contributor records for David Baldacci, so three lookups of his books
produce three ids for one author; they are folded onto one identity rather than
splitting his shelf three ways. `shelve` resolves each contributor through it, writes the canonical
spelling into the tag — so ABS indexes one author — and files the book under
that name, so the folder follows the tag rather than being fixed separately.

Three tiers, strongest first:

1. **The book has an ASIN**, so Audible names its contributors. Where it also
   gives a contributor ASIN, that is the identity. Where it does not — and for
   self-published authors it very often does not, returning a bare
   `{"name": "Rhaegar"}` — the identity is derived from the name itself and
   stored as `name:rhaegar`, which is weaker and says so. One call per book
   whose authors are not already known, and none at all for a book whose
   authors the registry has seen, so a second run is free.
2. **No ASIN**, but the spelling reduces to a key already in the alias table —
   casefolded, punctuation and spacing dropped — so `J N Chaney` resolves
   offline.
3. **Neither.** Nothing happens. The name is written exactly as it arrived and
   recorded as a candidate, because the alternative is guessing.

**An Audible contributor id is not proof of identity.** Amazon gives every
Great Courses lecturer the imprint's own id — one payload returns
`B06XPWVHFB` twice, once as `Constance Kassor` and once as `The Great
Courses` — and Steve Cavanagh shares Mark Dawson's. So a shared id is
believed only when the two names look like one person: one key a subsequence
of the other with a length ratio (a middle initial, an expanded first name,
an honorific, a typo), or the same tokens in a different order
(`Baldacci, David` is `David Baldacci`). That keeps the thirteen genuine
merges in this library and rejects the two wrong ones, which had been about to
erase a lecturer's name from her own author tag. A disputed id is not used as
an identity either: the author is keyed by name, and the id stays with
whoever already held it.

**The key deliberately does not drop initials.** `Brandon Morris` and
`Brandon Q. Morris` reduce to different keys and are never joined
automatically: they might be two people, and a rule clever enough to join them
is also clever enough to join two real authors who share a surname. Only an
author ASIN, or `forge authors --link`, settles that case — once, permanently.

Collaborations get one shelf per line-up. A folder that already names the same
people is reused whatever order it used, so nothing already shelved is renamed
for the sake of it; when a folder has to be created, the names are listed
**alphabetically** — by the whole name as written, first name first, with no
guessing at which part is a surname. Audible bills the same pair in a different
order from one book to the next, and without this that makes a second shelf for
the same two people.

The **tag keeps its billing order**. Only the folder name is sorted: the order
Audible lists authors in is information, and not forge's to rewrite.

Not every comma separates two people. `L. E. Modesitt, Jr.` is one author, and
reading that comma as a separator gave Audiobookshelf an author called `Jr.`
with 39 books in this library. A comma-separated part that is *only* a name
suffix — `Jr`, `Sr`, `II`, `III`, `IV`, `PhD`, `MD`, `Esq` and friends, an
explicit list rather than a guess — rejoins the name before it. A real
co-author after a suffix still splits correctly:
`Martin Luther King, Jr., Coretta Scott King` is two people.

Triage flags an author forge has never identified: the name shows **yellow** in
the `author` column, and the detail panel says `★ new author … this spelling
becomes a shelf in the library`. The spelling in that tag is about to become a
folder and an Audiobookshelf author, and triage is the last point at which
changing it is free — so a name forge cannot place is coloured rather than
silently accepted. `f` cycles to a `new-author` filter that narrows the list to
exactly those books.

Accepting an Audible match clears the flag, because that lookup is what settles
the question: the product carries Audible's contributor ASINs, so the author is
identified the moment you apply the tags rather than hours later when `shelve`
gets to it. The flag also clears when shelving identifies an author, or when you
do it yourself with `forge authors --link`. What stays flagged is an author
forge has no identity for — a hand-typed name, or a book with no ASIN — which is
the case worth a second look.

The colour is advisory: if the registry cannot be opened at all, the list
behaves exactly as it did before any of this existed.

When the registry knows the author but the tag spells them differently, the
panel says `⇒ shelves as J. N. Chaney` instead, since nothing needs checking —
that rewrite is the mechanism working.

`forge authors` opens a review interface over the registry — the frequency vote
is the best rule a machine has and it is wrong often enough to want checking.
It lists every identity with the spellings Amazon has used, how many books the
author has, and how many contributor ids they hold.

| key | action |
|---|---|
| `e` / `enter` | change the spelling; the author's other spellings are offered as numbered choices |
| `p` | pin or unpin the name |
| `u` | drop this row's unsaved change |
| `/` | search name, spelling or identity |
| `f` | cycle filters (all / recorded more than one way / pinned / unpinned / no Audible id / unsaved / unidentified) |
| `o` | sort (books, name, spellings, ids, pinned, recently changed) |
| `s` | save |
| `q` | quit |

**Nothing is written until you save.** Edits live in memory, the status bar
shows `[3 unsaved]`, `s` asks for confirmation, and quitting with unsaved work
offers save / discard / stay. Saving **pins** each name, so the next backfill's
frequency vote cannot overrule a choice you made on purpose.

Deliberately limited: no identity can be deleted, no key edited, and a blank
name is refused three times over — in the dialog, when the save is built, and by
the registry itself. The interface opens the registry and nothing else: no tags,
no files, no change log. Reviewing a name is not an instruction to go and
rewrite a library; `forge backfill` does that, when you ask it to.

The 198 spellings with no identity at all appear in their own read-only filter,
so junk in an author tag (`Full Cast`, `40k Books`) is visible. Linking one to
an identity stays a CLI job, since that is the one operation where a wrong
answer merges two real people.

Non-interactive callers are unaffected: a pipe, a cron job, or `--report` gets
the text summary.

| command | what it does |
|---|---|
| `forge authors` | the review interface (or the text report when not a terminal) |
| `forge authors --report` | the text report: what the registry knows, and which authors Amazon records more than one way |
| `forge authors "JN Chaney"` | the identity behind one spelling, its other spellings, and its Audible ids |
| `forge authors --unknown` | spellings it could not place |
| `forge authors --link "Brandon Morris" B01N7UFRX4` | declare a spelling to be a known author (an ASIN, or a `name:` identity) |
| `forge authors --rename B00W2ZAK7E "J. N. Chaney"` | change how an author is written |

`forge shelve --no-author-lookup` keeps the registry but asks Audible nothing;
`--no-canonical-authors` ignores the registry altogether and shelves under
whatever the tags say.

**Known rough edge.** The canonical spelling is whichever one was learned first,
which is effectively arbitrary — Amazon's own display name for an author varies
between products. It is stable (a later lookup never renames an author, because
that would make the library rename itself at random), and `--rename` fixes any
you care about. Choosing it by frequency across the whole library is a job for
the backfill pass, which has the whole library to count over.

### Quitting while something is running

`q` quits immediately when nothing is in flight. If a write is still going it
does **not** quit: the interface stays up, the indicator reads `[⠙ Finishing
writes before quitting]`, the status line says how many writes it is waiting
for, and forge exits by itself the moment they land. Keys other than `q` are
ignored while it waits, so a stray `r` cannot start new work and extend the
wait.

This is not a safety ritual — it is what was already happening, made visible. A
thread cannot be interrupted, so the old `q` tore the interface down and left
the *process* sitting there silently until the write finished, with no prompt
and no explanation. A ctrl+c in that window printed a traceback over a write
that was, in fact, still running. Waiting for read-only work (a scan, a library
check) is shown for the same reason: the process would wait regardless, so it
may as well say so.

### Is this one already in my library?

`d` asks Audiobookshelf whether the selected book is already shelved, and `D`
asks about every book in the list that has an ASIN. The answer appears in the
`dup` column — `dup` for a match, `new` for a book the library does not have,
`err` when the server could not be reached — and the `f` filter has a
`duplicate` setting that narrows the list to the matches. A match also opens a
dialog naming the shelved copy, because the moment worth interrupting is the
one just before you spend an hour converting a book you already own.

The match is on **ASIN only**. Titles collide across editions, abridgements and
re-releases, so a book with no ASIN is reported as unchecked rather than as new:
"I could not tell" and "you do not have this" are different answers, and only
one of them is safe to act on. Run the Audible lookup (`a`) first if you want a
book included.

Nothing is written to Audiobookshelf, and nothing on disk changes: the check
reads the library and reports. The whole library index is fetched once per
session — a few seconds on a large server, with the activity indicator showing
`Indexing library` — and every check after that answers instantly.

This needs credentials, and they are optional: everything else in forge works
without them. Put them in `[audiobookshelf]` in the config, or keep them out of
the config file and in a `.env` instead:

```
AUDIOBOOKSHELF_URL = "http://audiobookshelf.lan:13378"
AUDIOBOOKSHELF_API_KEY = "…"
AUDIOBOOKSHELF_LIBRARY_ID = "…"     # optional; without it, every book library is indexed
```

forge looks for that file next to the config, then in `~/.config/forge/.env`,
then in the forge directory, and `FORGE_ENV=/path/to/file` overrides the search
entirely. It holds an API key, so `chmod 600` it. Values set in the config file
win over the environment, which wins over the `.env`.

### Running it on demand, or from cron

```
forge run                                  # everything tagged
forge run ~/audiobooks/one-book.m4b        # just this one
forge run -d downcode --limit 5            # the first five downcodes
forge run --dry-run                        # plan only
```

`bin/forge-cron` is the nightly pass: it converts, then shelves, with
timestamps and exit-code reporting for each stage.

```
30 2 * * * /path/to/forge/bin/forge-cron >> /path/to/forge/cron.log 2>&1
```

Pass `--no-shelve` to convert only, or `--shelve-only` to file books that are
already converted. Shelving runs even when a conversion failed: the books that
did convert should still reach the library, and a book that failed was left
where it was with its disposition intact.

Output is line-oriented and colour-free whenever stdout is not a terminal, so
`cron.log` stays readable.

Press `q` during an attended run to stop after the current book; `p` pauses.
Ctrl-C does the same, and a second Ctrl-C abandons the conversion in flight,
killing ffmpeg and removing its temporary file.

Exit codes: `0` success, `1` failures, `2` usage, `3` configuration, `4` already
locked, `130` interrupted.

### Shelving: into the Audiobookshelf library

The last step. `forge shelve` reads the output directory and files each
finished book where Audiobookshelf expects it:

```
<library>/<Author>/<Series>/<NN - Album>/<Album (year) [asin]>.m4b
```

```
forge shelve -n                            # compute every destination, change nothing
forge shelve                               # file them
forge shelve ~/converted/one-book.m4b      # just this one
forge shelve --target /other/library       # somewhere else, once
forge shelve --csv plan.csv -n             # the dry-run CSV, for a spreadsheet
```

For each book, in order:

1. **Tags are read.** No author, or no album and no title, and the book is
   skipped with the reason — a book with no author has no shelf to go on.
2. **The ASIN is checked against the library.** Already there, and the file is
   moved to a duplicates directory (`<source>/duplicates` by default) instead,
   so you still have it but Audiobookshelf never sees two. A book with no ASIN
   is shelved without the question being asked; a library that cannot be
   *reached* blocks the book rather than risking a duplicate.
3. **Two tags are tidied**, because Audiobookshelf reads them and gets them
   wrong otherwise: role credits come off the author (`Mary Smith -
   Translator` is not an author, and would otherwise get her own shelf, while
   `Tom Clancy aka Jim Jones` is two authors), and the track number is removed,
   since a single-file book carrying one shows up as track 1 of a set. Both
   edits go through forge's tag layer, so they appear in `forge history` and
   `forge undo` puts them back — in the library copy, because the change log
   follows the file when it moves.
4. **The file is moved**, never overwriting: a name already taken gets a `(2)`,
   and two books that compute the same name in one run get different ones.
   Within one filesystem the move is a relink — instant, whatever the size.
   Across filesystems it is copy, hash both ends, then remove the original.

The series part is zero-padded (`3` → `03`) so folders sort, and an illegal
character in a name becomes `_` (`Space: 1969` → `Space_ 1969`). That is not a
style choice: it is what is already on disk, and normalising differently would
file the same author under a second folder.

`library_dir` is optional and forge never creates it — a typo should fail
loudly rather than quietly start filling a second library beside the real one.
Nothing is ever written *to* Audiobookshelf: forge reads the library and moves
files, and the server picks them up on its own schedule.

## The encoding rules

Operating on the source bitrate bucketed to the nearest 8 kbps, since encoders
undershoot their nominal rate.

| source | result |
|---|---|
| mono, under 32 kbps | **declined** — re-encoding gains nothing |
| mono, 32–63 kbps | matched to the source rate, mono |
| mono, 64 kbps and up | 64 kbps mono |
| stereo, under 64 kbps | **declined** |
| stereo, 64–127 kbps | half rate, downmixed to mono |
| stereo, 128 kbps and up | 64 kbps mono |
| `drama`, stereo | 128 kbps if the source allows, else 64 — **stereo kept** |
| `drama`, mono | the standard rules |

`max_bitrate` caps ordinary narration. It deliberately does **not** cap `drama`:
that cap exists to shrink narration, and applying it would defeat the point of
marking a book as dramatised.

A declined book whose disposition said to convert becomes `needs-attention`,
with the reason recorded, and sorts to the top of the next triage session.

## What protects your library

Conversions are transactional, in this order:

1. convert to a temporary file beside the final output
2. validate it against the source
3. copy the source's tags and cover onto it
4. only then promote it to its final name
5. only then archive the original
6. finally, stamp the output `done`

The source is never touched until steps 2 and 3 both succeed, so a failed run is
always safe to simply run again.

- **Validation** compares the output against the source: channel count, bitrate
  within tolerance, duration within 1% or 2 seconds, and chapter count exactly.
  A truncated or chapter-stripped output is rejected and discarded.
- **Nothing is overwritten.** Promotion uses an exclusive create; an existing
  output stops the plan; archive collisions get a `(2)` suffix.
- **Nothing is deleted.** Originals move to the archive. The only deletion is
  the source of an `as-is` relocation, and only after a byte-for-byte hash
  comparison of the copy.
- **Tags survive the conversion.** ffmpeg's mp4 muxer writes only the atoms it
  knows about, so `-map_metadata` quietly loses every custom one — series,
  series-part, ASIN, subtitle, publisher, language, explicit. forge re-applies
  them with mutagen after validation, and writes series part both as
  `series-part` (which Audiobookshelf reads) and `SERIES_SEQUENCE` (which other
  tools read).
- **Tag writes are staged.** Every write copies the file, mutates the copy,
  re-opens it to prove it still parses, then replaces the original. mutagen
  rewrites a whole MP4 container when the tag block outgrows its padding, and a
  failure part-way through leaves an unplayable file.
- **Settle detection.** An unattended run waits for size and mtime to stop
  changing and for a minimum age to pass, so a download still in flight is never
  picked up. `*.tmp.*` names are skipped outright.
- **One run at a time**, via a lock file that records its owner's pid and can
  tell a live instance from debris left by a power cut.
- **Pre-flight disk space check** against the estimated output size.
- **Folder books need unanimity.** A disposition is written to every track; if
  the tracks disagree, the book is conflicted and never processed.
- **Shelving never guesses.** A duplicate is set aside rather than shelved, and
  a library that cannot be reached blocks the book instead of being read as "not
  a duplicate". Within a filesystem the move relinks rather than copying, so
  there is no window in which the book exists only as a partial copy; across
  filesystems it copies, hashes both ends, and only then removes the original.
  Both the move and the tag edits are recorded, so `forge history` can say where
  a book went.

## Development

```
python3 -m unittest discover -s tests -t .
```

Only the standard library is needed to run the tests; audio fixtures are
synthesised with ffmpeg, and MP3 fixtures are built from hand-written frame
headers, since not every ffmpeg build ships an MP3 *encoder*.

```
forge/
  cli.py          subcommands              probe.py     ffprobe → AudioInfo
  config.py       INI loading, validation  ffmpeg.py    subprocess, progress, codecs
  disposition.py  the tag and its meaning  rules.py     the encoding decisions
  scan.py         book discovery           plan.py      source + intent → a plan
  convert.py      the executor             validate.py  output verification
  archive.py      promote, archive, move   paths.py     filename construction
  tags.py         mutagen read/write       audible.py   catalogue lookup
  history.py      change log and undo      settle.py    wait for files to stop changing
  control.py      interruption handling    lock.py      single-instance lock
  render.py       terminal output          library.py   Audiobookshelf, read-only
  shelve.py       into the library         tui/         the triage interface
```
