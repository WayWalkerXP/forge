"""The forge command line.

Subcommands:

``triage``
    The interactive TUI.  Reads, edits and tags; never converts.
``run``
    The executor.  Reads disposition tags and acts.  Takes explicit paths for
    converting one book on demand, or no paths at all for the cron entry point.
``shelve``
    The last step: files finished books into the Audiobookshelf library.
``tags``
    Non-interactive tag inspection and editing.
``disposition``
    Read or set the disposition tag without opening the TUI.
``lookup``
    An Audible query, printed.
``history`` / ``undo``
    The change log, and putting tag values back.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import time
from pathlib import Path

from . import __version__

LOGGER = logging.getLogger("forge")

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_LOCKED = 4
EXIT_INTERRUPTED = 130


def _global_options() -> argparse.ArgumentParser:
    """Options accepted both before and after the subcommand.

    Shared through ``parents`` so that ``forge --plain run`` and
    ``forge run --plain`` both work; requiring one particular position is the
    kind of papercut that makes a tool feel brittle.
    """

    # Every default is SUPPRESS so that an option left out simply does not
    # appear in the namespace.  With ordinary defaults, the subparser - which
    # parses second, through the same `parents` - overwrites what the main
    # parser already stored, so `forge -c other.ini run` silently fell back to
    # the default config and ran against the wrong library.  The defaults are
    # restored by _apply_global_defaults after parsing.
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "-c", "--config", type=Path, default=argparse.SUPPRESS, help="path to config.ini"
    )
    shared.add_argument(
        "-v", "--verbose", action="count", default=argparse.SUPPRESS,
        help="repeat for more detail",
    )
    shared.add_argument(
        "--plain", action="store_true", default=argparse.SUPPRESS,
        help="line-oriented output, no tables",
    )
    shared.add_argument(
        "--no-color", action="store_true", default=argparse.SUPPRESS, help="disable colour"
    )
    return shared


#: The global options and the values they take when not given anywhere.
GLOBAL_DEFAULTS = {"config": None, "verbose": 0, "plain": False, "no_color": False}


def _apply_global_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in the globals that were suppressed so nothing clobbered them."""

    for name, default in GLOBAL_DEFAULTS.items():
        if getattr(args, name, None) is None:
            setattr(args, name, default)
    return args


def build_parser() -> argparse.ArgumentParser:
    shared = _global_options()
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Triage, convert and shelve audiobook files.",
        parents=[shared],
    )
    parser.add_argument("--version", action="version", version=f"forge {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    triage = sub.add_parser("triage", help="interactive triage (TUI)", parents=[shared])
    triage.add_argument("directory", nargs="?", type=Path, help="override source_dir")
    triage.add_argument(
        "--log",
        type=Path,
        metavar="FILE",
        help="write a debug log here; a TUI cannot print diagnostics to the screen, "
        "so this is how an intermittent problem gets captured",
    )

    run = sub.add_parser("run", help="convert books according to their disposition tag", parents=[shared])
    run.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="specific books to process; omit to process everything tagged",
    )
    run.add_argument(
        "-n", "--dry-run", action="store_true", help="show what would happen, change nothing"
    )
    run.add_argument(
        "-d",
        "--disposition",
        help="only process these dispositions (comma separated)",
    )
    run.add_argument("--limit", type=int, help="stop after this many books")
    run.add_argument(
        "--no-settle",
        action="store_true",
        help="skip the wait for files to stop changing",
    )
    run.add_argument(
        "--keep-disposition",
        action="store_true",
        help="do not rewrite unprocessable books to needs-attention",
    )

    shelve = sub.add_parser(
        "shelve",
        help="file finished books into the Audiobookshelf library",
        description="Reads the output directory, checks each book's ASIN against the "
        "library, tidies the two tags Audiobookshelf reads wrongly, and files it "
        "under Author/Series/NN - Album. Nothing is ever overwritten.",
        parents=[shared],
    )
    shelve.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="specific files or directories; omit to shelve everything in the output directory",
    )
    shelve.add_argument(
        "-n", "--dry-run", action="store_true",
        help="compute every destination and change nothing",
    )
    shelve.add_argument("--source", type=Path, help="override output_dir")
    shelve.add_argument("--target", type=Path, help="override library_dir")
    shelve.add_argument(
        "--duplicates", type=Path, help="where to set duplicates aside (default: <source>/duplicates)"
    )
    shelve.add_argument("--limit", type=int, help="stop after this many books")
    shelve.add_argument(
        "--no-settle", action="store_true", help="skip the wait for files to stop changing"
    )
    shelve.add_argument(
        "--no-author-lookup",
        action="store_true",
        help="do not ask Audible who an unrecognised author is; the registry "
        "still answers from what it already knows",
    )
    shelve.add_argument(
        "--no-canonical-authors",
        action="store_true",
        help="shelve under the author spelling in the tags, without consulting "
        "the identity registry at all",
    )
    shelve.add_argument(
        "--no-duplicate-check",
        action="store_true",
        help="do not ask Audiobookshelf whether a book is already shelved",
    )
    shelve.add_argument(
        "--csv", type=Path, metavar="FILE", help="also write the plan to a CSV file"
    )

    listing = sub.add_parser("list", help="show the books in the source directory", parents=[shared])
    listing.add_argument("directory", nargs="?", type=Path)
    listing.add_argument("-d", "--disposition", help="filter by disposition")
    listing.add_argument("--ready", action="store_true", help="only books ready to convert")
    listing.add_argument("--blocked", action="store_true", help="only books that are not ready")

    tags_cmd = sub.add_parser("tags", help="read or write tags", parents=[shared])
    tags_sub = tags_cmd.add_subparsers(dest="tags_command", metavar="<action>")
    show = tags_sub.add_parser("show", help="print tags")
    show.add_argument("paths", nargs="+", type=Path)
    show.add_argument("--json", action="store_true")
    set_cmd = tags_sub.add_parser("set", help="write tags")
    set_cmd.add_argument("paths", nargs="+", type=Path)
    set_cmd.add_argument(
        "-s",
        "--set",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="repeatable; an empty value clears the field",
    )

    disp = sub.add_parser("disposition", help="read or set the disposition tag", parents=[shared])
    disp.add_argument("paths", nargs="+", type=Path)
    disp.add_argument(
        "-s", "--set", dest="value", help="downcode, drama, remux, as-is, hold, or clear"
    )

    lookup = sub.add_parser("lookup", help="query Audible", parents=[shared])
    lookup.add_argument("query", nargs="?", help="title to search for")
    lookup.add_argument("-a", "--author")
    lookup.add_argument("--asin", help="look up a known ASIN instead of searching")
    lookup.add_argument("--region", default="us")
    lookup.add_argument("--apply", type=Path, help="write the result to this file")

    hist = sub.add_parser(
        "history",
        help="show recorded tag changes",
        description="With no PATH, lists recent change groups and their ids, "
        "which is what `forge undo` takes.",
        parents=[shared],
    )
    hist.add_argument(
        "path", nargs="?", type=Path, help="one file, shown field by field"
    )
    hist.add_argument("--limit", type=int, default=20, help="how many groups to show")
    hist.add_argument("--stats", action="store_true", help="database statistics")
    hist.add_argument("--prune", type=int, metavar="DAYS", help="delete records older than DAYS")

    authors_cmd = sub.add_parser(
        "authors",
        help="inspect and correct the author identity registry",
        description="Amazon spells the same author several ways; Audible gives each "
        "one an ASIN that does not vary. This is where those answers are kept. With "
        "no arguments, reports what the registry knows and what it could not place.",
        parents=[shared],
    )
    authors_cmd.add_argument(
        "name",
        nargs="?",
        help="show what the registry makes of this author spelling",
    )
    authors_cmd.add_argument(
        "--link",
        nargs=2,
        metavar=("SPELLING", "IDENTITY"),
        help="declare that SPELLING is this author - the 'Brandon Morris' / "
        "'Brandon Q. Morris' case, which nothing can infer. IDENTITY is an "
        "Audible contributor ASIN, or the name:<key> form shown by "
        "`forge authors <name>` for an author Amazon has no ASIN for",
    )
    authors_cmd.add_argument(
        "--name",
        dest="canonical",
        metavar="NAME",
        help="with --link, the spelling this author should be written as",
    )
    authors_cmd.add_argument(
        "--rename",
        nargs=2,
        metavar=("IDENTITY", "NAME"),
        help="change the canonical spelling of a known author",
    )
    authors_cmd.add_argument(
        "--unknown", action="store_true", help="list only the spellings it could not place"
    )
    authors_cmd.add_argument(
        "--limit", type=int, default=25, help="how many rows to show"
    )

    undo = sub.add_parser(
        "undo",
        help="restore tag values from a change group",
        description="Group ids come from `forge history`. Run `forge undo` with no id "
        "to see them.",
        parents=[shared],
    )
    undo.add_argument(
        "group_id",
        nargs="?",
        type=int,
        help="from `forge history`; omit to list the ids on offer",
    )
    undo.add_argument("-n", "--dry-run", action="store_true")
    undo.add_argument("--limit", type=int, default=20, help="how many groups to list")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = _apply_global_defaults(
        parser.parse_args(argv if argv is not None else sys.argv[1:])
    )

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    _configure_logging(args.verbose)

    from .errors import ConfigError, DiskSpaceError, ForcedTermination, ForgeError, LockError

    try:
        handler = _HANDLERS[args.command]
        return handler(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except LockError as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_LOCKED
    except DiskSpaceError as exc:
        print(f"out of disk space: {exc}", file=sys.stderr)
        LOGGER.critical("stopping because the disk is full: %s", exc)
        return EXIT_FAILURES
    except (KeyboardInterrupt, ForcedTermination):
        print("\ninterrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except ForgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURES


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _load_config(args: argparse.Namespace):
    from . import config as config_mod

    cfg = config_mod.load(args.config)
    if cfg.log_file is not None:
        _add_file_logging(cfg.log_file)
    return cfg


def _add_debug_logging(path: Path) -> None:
    """Log everything to *path* at DEBUG.

    The triage interface owns the terminal, so a traceback has nowhere to go -
    textual swallows exceptions raised inside an action and the key simply
    appears to do nothing.  This is the only way to find out what happened.
    """

    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(funcName)s: %(message)s")
    )
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    logging.getLogger("forge").info("debug logging started -> %s", path)


def _add_file_logging(path: Path) -> None:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    # Always record detail to the file even when the console is quiet; the file
    # is what gets read after an unattended run went wrong.
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    # The renderer is the user-facing output and already reports every warning
    # in context.  Letting the logger echo them to the console as well doubles
    # every line in cron.log, so the console keeps only errors the renderer
    # would not otherwise mention.
    for existing in root.handlers:
        if existing is not handler:
            existing.setLevel(logging.ERROR)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_triage(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    root = args.directory or cfg.source_dir

    if getattr(args, "log", None):
        _add_debug_logging(args.log)

    try:
        from .tui.app import run_triage
    except ImportError as exc:
        print(
            "the triage interface needs textual, which is not installed.\n"
            "  install it with:  sudo apt install python3-textual\n"
            f"  (import failed: {exc})",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    return run_triage(cfg, root)


def cmd_run(args: argparse.Namespace) -> int:
    from . import convert, ffmpeg, plan as plan_mod, render, scan
    from .control import RunController
    from .disposition import Disposition
    from .lock import Lock

    cfg = _load_config(args)
    renderer = render.make_renderer(force_plain=args.plain, no_color=args.no_color)

    wanted: set[Disposition] | None = None
    if args.disposition:
        from .disposition import parse as parse_disposition

        wanted = {parse_disposition(part) for part in args.disposition.split(",") if part.strip()}

    started = time.monotonic()

    with Lock(cfg.lock_path):
        codec = ffmpeg.select_codec(cfg.preferred_codec, cfg.fallback_codec)
        LOGGER.info("using encoder %s", codec)

        books = _collect_books(args, cfg, renderer)
        if wanted is not None:
            books = [b for b in books if b.disposition in wanted]

        actionable = [b for b in books if b.disposition.is_actionable or b.disposition_conflict]
        if not actionable:
            renderer.info("nothing tagged for processing")
            return EXIT_OK

        if not args.no_settle and not args.dry_run:
            actionable = _settle(actionable, cfg, renderer)
            if not actionable:
                renderer.info("every candidate is still being written; try again later")
                return EXIT_OK

        if args.limit:
            actionable = actionable[: args.limit]

        plans = plan_mod.plan_many(actionable, cfg, codec=codec)

        if args.dry_run:
            renderer.plan_table(plans)

        results = []
        total = len(plans)
        interrupted = False

        with RunController(allow_keys=not args.dry_run) as controller:
            for index, item in enumerate(plans, 1):
                if not args.dry_run:
                    renderer.start_book(item, index, total)

                # Registered so a forced termination removes the partial file
                # rather than leaving it to be mistaken for a finished output.
                controller.register_temp(item.temp_path)
                try:
                    result = convert.execute(
                        item,
                        cfg,
                        on_stage=renderer.stage,
                        dry_run=args.dry_run,
                        mark_needs_attention=not args.keep_disposition,
                    )
                finally:
                    controller.unregister_temp(item.temp_path)

                results.append(result)
                if not args.dry_run:
                    renderer.finish_book(result)

                # Quit requests are honoured here, between transactions, so a
                # book is never left half-converted.
                if controller.should_stop(prompt=renderer.info):
                    interrupted = True
                    remaining = total - index
                    if remaining:
                        renderer.warn(f"stopped by request with {remaining} book(s) left")
                    break

        renderer.summary(results, time.monotonic() - started)

    failures = sum(1 for r in results if r.outcome is convert.Outcome.FAILED)
    if failures:
        return EXIT_FAILURES
    return EXIT_INTERRUPTED if interrupted else EXIT_OK


def _collect_books(args: argparse.Namespace, cfg, renderer) -> list:
    """Books named on the command line, or everything under source_dir."""

    from . import scan

    if not args.paths:
        result = scan.scan(cfg.source_dir)
        for error in result.errors:
            renderer.warn(error)
        return result.books

    books: list = []
    for raw in args.paths:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            renderer.error(f"not found: {path}")
            continue
        if path.is_dir():
            found = scan.scan(path)
            books.extend(found.books)
            continue
        # A single named file is treated as its own book even if it sits in a
        # directory alongside others, because the user pointed at it directly.
        books.append(
            scan._build_book(path, [path], is_folder=False, with_probe=True)
        )
    return books


def _settle(books: list, cfg, renderer) -> list:
    """Drop books whose files are still being written."""

    from .settle import partition_settled

    ready, pending = partition_settled(
        books, min_age=cfg.min_age_seconds, settle_seconds=cfg.settle_seconds
    )
    for book, reason in pending:
        renderer.warn(f"skipping {book.display_name}: {reason}")
    return ready


def cmd_shelve(args: argparse.Namespace) -> int:
    """File finished books into the library.

    Deliberately the same shape as :func:`cmd_run`: one lock, one settle check,
    one controller loop so an interruption lands between books, and the same
    renderer. The last step of the pipeline should not feel like a different
    program - it used to be one.
    """

    import csv as csv_mod

    from . import render, shelve as shelve_mod
    from .control import RunController
    from .errors import ConfigError
    from .lock import Lock

    cfg = _load_config(args)
    renderer = render.make_renderer(force_plain=args.plain, no_color=args.no_color)

    source = (args.source or cfg.output_dir).expanduser().resolve()
    library = (args.target or cfg.library_dir)
    if library is None:
        raise ConfigError(
            "no library directory: set [paths] library_dir in the config, or pass --target"
        )
    library = Path(library).expanduser().resolve()
    if not library.is_dir():
        raise ConfigError(f"the library directory does not exist: {library}")

    from . import config as config_mod

    cfg = config_mod.with_overrides(cfg, library_dir=library)
    duplicates = (
        args.duplicates.expanduser().resolve() if args.duplicates else cfg.duplicates_for(source)
    )

    started = time.monotonic()

    with Lock(cfg.lock_path):
        paths, pending = _shelve_candidates(args, source, duplicates, renderer)
        for path, why in pending:
            renderer.warn(f"waiting on {path.name}: {why}")
        if not paths:
            renderer.info(f"nothing to shelve in {source}")
            return EXIT_OK

        if not args.no_settle and not args.dry_run:
            books = _settle([shelve_mod.as_book(path) for path in paths], cfg, renderer)
            paths = [book.path for book in books]
            if not paths:
                renderer.info("every candidate is still being written; try again later")
                return EXIT_OK

        if args.limit:
            paths = paths[: args.limit]

        client = None if args.no_duplicate_check else shelve_mod.make_client(cfg)
        renderer.info(
            f"shelving {len(paths)} book(s) from {source} into {library}"
            + ("  [dry run]" if args.dry_run else "")
        )

        # Identities first, because the canonical author name decides both the
        # tags that get written and the shelf they are written to.
        with _author_registry(cfg, enabled=not args.no_canonical_authors) as registry:
            if registry is not None:
                with _audible_for_identities(renderer, args) as audible:
                    outcome = shelve_mod.learn_identities(paths, registry, audible)
                renderer.info(f"author identities: {outcome.describe()}")
            plans = shelve_mod.plan_many(paths, cfg, client=client, registry=registry)

            if args.dry_run or args.csv:
                renderer.shelve_table(plans)
            if args.csv:
                _write_shelve_csv(args.csv, plans, csv_mod)
                renderer.info(f"plan written to {args.csv}")

            results: list = []
            total = len(plans)
            interrupted = False

            with RunController(allow_keys=not args.dry_run) as controller:
                for index, plan in enumerate(plans, 1):
                    result = shelve_mod.execute(
                        plan, cfg, duplicates_dir=duplicates, dry_run=args.dry_run
                    )
                    results.append(result)
                    if not args.dry_run:
                        renderer.shelve_book(result, index, total)

                    # Between books, never mid-move.
                    if controller.should_stop(prompt=renderer.info):
                        interrupted = True
                        remaining = total - index
                        if remaining:
                            renderer.warn(f"stopped by request with {remaining} book(s) left")
                        break

            renderer.shelve_summary(results, time.monotonic() - started)

        failures = sum(1 for r in results if not r.ok)
        if failures:
            return EXIT_FAILURES
        return EXIT_INTERRUPTED if interrupted else EXIT_OK


@contextlib.contextmanager
def _author_registry(cfg, *, enabled: bool):
    """The author identity registry, or None when it is switched off."""

    if not enabled:
        yield None
        return

    from . import authors as authors_mod

    with authors_mod.AuthorRegistry(cfg.authors_db_path) as registry:
        yield registry


@contextlib.contextmanager
def _audible_for_identities(renderer, args):
    """An Audible client for identity lookups, or None.

    None is a working configuration, not a failure: the registry still answers
    from what it already knows, and an unrecognised spelling is left alone.
    """

    if getattr(args, "no_author_lookup", False):
        yield None
        return

    from .audible import AudibleClient

    try:
        client = AudibleClient()
    except Exception as exc:  # pragma: no cover - construction is local
        renderer.warn(f"author lookups unavailable: {exc}")
        yield None
        return

    try:
        yield client
    finally:
        client.close()


def _shelve_candidates(
    args: argparse.Namespace, source: Path, duplicates: Path, renderer
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """The m4b files to consider: those named on the command line, or the tree."""

    from . import shelve as shelve_mod

    if not args.paths:
        return shelve_mod.find_books(source, exclude=[duplicates])

    paths: list[Path] = []
    pending: list[tuple[Path, str]] = []
    for raw in args.paths:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            renderer.error(f"not found: {path}")
            continue
        if path.is_dir():
            found, waiting = shelve_mod.find_books(path, exclude=[duplicates])
            paths.extend(found)
            pending.extend(waiting)
            continue
        if path.suffix.casefold() not in shelve_mod.SHELVEABLE_EXTENSIONS:
            renderer.error(f"not a finished book: {path.name}")
            continue
        paths.append(path)
    return paths, pending


def _write_shelve_csv(path: Path, plans: list, csv_mod) -> None:
    """The dry-run CSV absmove wrote, so existing habits keep working."""

    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv_mod.writer(handle)
        writer.writerow(["source", "target", "action", "reason"])
        for plan in plans:
            writer.writerow(
                [
                    str(plan.source),
                    str(plan.target or ""),
                    plan.status.value,
                    plan.reason or "; ".join(plan.fix_notes()),
                ]
            )


def cmd_list(args: argparse.Namespace) -> int:
    from . import render, scan
    from .disposition import parse as parse_disposition

    cfg = _load_config(args)
    renderer = render.make_renderer(force_plain=args.plain, no_color=args.no_color)

    result = scan.scan(args.directory or cfg.source_dir)
    for error in result.errors:
        renderer.warn(error)

    books = result.books
    if args.disposition:
        wanted = {parse_disposition(p) for p in args.disposition.split(",") if p.strip()}
        books = [b for b in books if b.disposition in wanted]
    if args.ready:
        books = [b for b in books if b.is_ready]
    if args.blocked:
        books = [b for b in books if not b.is_ready]

    if not books:
        renderer.info("no books matched")
        return EXIT_OK

    renderer.book_table(books)
    for path in result.skipped_in_progress:
        renderer.warn(f"skipped (still being written): {path.name}")
    return EXIT_OK


def cmd_tags(args: argparse.Namespace) -> int:
    from . import tags as tags_mod

    if args.tags_command == "show":
        return _tags_show(args, tags_mod)
    if args.tags_command == "set":
        return _tags_set(args, tags_mod)
    print("usage: forge tags show|set ...", file=sys.stderr)
    return EXIT_USAGE


def _tags_show(args: argparse.Namespace, tags_mod) -> int:
    import json

    from .errors import ForgeError

    payloads = []
    status = EXIT_OK
    for path in args.paths:
        try:
            meta = tags_mod.read_metadata(path)
        except ForgeError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            status = EXIT_FAILURES
            continue
        payloads.append(meta.to_dict())

    if args.json:
        print(json.dumps(payloads, indent=2, ensure_ascii=False))
        return status

    from . import disposition as disposition_mod

    for payload in payloads:
        print(payload["path"])
        try:
            print(f"  {'disposition':<16} {disposition_mod.read(payload['path']).value}")
        except ForgeError:
            pass
        for key, value in payload.items():
            if key == "path" or value in (None, [], ""):
                continue
            rendered = ", ".join(value) if isinstance(value, list) else value
            print(f"  {key:<16} {rendered}")
        print()
    return status


def _tags_set(args: argparse.Namespace, tags_mod) -> int:
    from .errors import ForgeError

    updates: dict[str, object] = {}
    for item in args.set:
        if "=" not in item:
            print(f"expected FIELD=VALUE, got {item!r}", file=sys.stderr)
            return EXIT_USAGE
        field, _, value = item.partition("=")
        updates[field.strip()] = value if value != "" else None

    if not updates:
        print("nothing to set; pass --set FIELD=VALUE", file=sys.stderr)
        return EXIT_USAGE

    status = EXIT_OK
    for path in args.paths:
        try:
            before = tags_mod.read_metadata(path)
            changes = tags_mod.diff_metadata(before, updates)
            if not changes:
                print(f"{path}: already up to date")
                continue
            tags_mod.write_metadata(path, updates)
            _record_history(args, path, changes, source="cli")
            for field, (old, new) in sorted(changes.items()):
                print(f"{path}: {field}: {old!r} -> {new!r}")
        except ForgeError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            status = EXIT_FAILURES
    return status


def cmd_disposition(args: argparse.Namespace) -> int:
    from . import disposition as disposition_mod
    from .disposition import Disposition
    from .errors import ForgeError

    if args.value is None:
        status = EXIT_OK
        for path in args.paths:
            try:
                print(f"{disposition_mod.read(path).value}\t{path}")
            except ForgeError as exc:
                print(f"{path}: {exc}", file=sys.stderr)
                status = EXIT_FAILURES
        return status

    raw = args.value.strip().casefold()
    if raw in ("clear", "none", "untriaged", ""):
        target = Disposition.UNTRIAGED
    else:
        target = disposition_mod.parse(raw)
        if target is Disposition.HOLD and raw != "hold":
            print(
                f"unknown disposition {args.value!r}; choose from "
                f"{', '.join(d.value for d in disposition_mod.ASSIGNABLE)}, or 'clear'",
                file=sys.stderr,
            )
            return EXIT_USAGE

    status = EXIT_OK
    for path in args.paths:
        try:
            disposition_mod.write(path, target)
            print(f"{path}: disposition = {target.value if target is not Disposition.UNTRIAGED else '(cleared)'}")
        except ForgeError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            status = EXIT_FAILURES
    return status


def cmd_lookup(args: argparse.Namespace) -> int:
    from . import audible as audible_mod
    from . import probe as probe_mod
    from . import tags as tags_mod
    from .errors import ForgeError

    with audible_mod.AudibleClient(region=args.region) as client:
        if args.asin:
            metadata = client.lookup(args.asin)
            _print_audible(metadata)
        else:
            if not args.query:
                print("pass a title to search for, or --asin", file=sys.stderr)
                return EXIT_USAGE

            source_seconds = None
            if args.apply:
                try:
                    source_seconds = probe_mod.probe(args.apply).duration_seconds
                except ForgeError:
                    source_seconds = None

            results = audible_mod.rank_by_runtime(
                client.search(title=args.query, author=args.author), source_seconds
            )
            if not results:
                print("no results")
                return EXIT_OK

            for index, item in enumerate(results, 1):
                delta = item.runtime_delta_minutes(source_seconds)
                gap = f"{delta:>4}m" if delta is not None else "   ?"
                runtime = f"{item.runtime_minutes // 60}h{item.runtime_minutes % 60:02d}m" if item.runtime_minutes else "   -  "
                print(
                    f"{index:>3}. {item.confidence(source_seconds):<7} {gap}  {item.asin}  "
                    f"{runtime}  {item.title[:44]:<46} {item.author[:26]}"
                )

            if not args.apply:
                return EXIT_OK

            choice = input("\napply which result? [number, or Enter to cancel] ").strip()
            if not choice:
                return EXIT_OK
            try:
                chosen = results[int(choice) - 1]
            except (ValueError, IndexError):
                print("not a listed number", file=sys.stderr)
                return EXIT_USAGE
            metadata = client.lookup(chosen.asin)
            _print_audible(metadata)

    if not args.apply:
        return EXIT_OK

    updates = metadata.to_updates()
    before = tags_mod.read_metadata(args.apply)
    changes = tags_mod.diff_metadata(before, updates)
    if not changes:
        print("\nlocal tags already match")
        return EXIT_OK

    print()
    for field, (old, new) in sorted(changes.items()):
        print(f"  {field:<16} {str(old)[:34]!r:36} -> {str(new)[:34]!r}")
    if input("\nwrite these tags? [y/N] ").strip().casefold() != "y":
        print("cancelled")
        return EXIT_OK

    tags_mod.write_metadata(args.apply, updates)
    _record_history(args, args.apply, changes, source="audible", description=f"ASIN {metadata.asin}")
    print(f"wrote {len(changes)} field(s) to {args.apply}")
    return EXIT_OK


def _print_audible(metadata) -> None:
    for name in (
        "asin", "title", "subtitle", "author", "narrator", "series",
        "series_sequence", "publisher", "copyright", "published_date", "language",
    ):
        value = getattr(metadata, name)
        if value:
            print(f"  {name:<16} {value}")
    if metadata.duration:
        print(f"  {'runtime':<16} {metadata.duration // 3600}h{metadata.duration % 3600 // 60:02d}m")
    if metadata.genres:
        print(f"  {'genres':<16} {', '.join(metadata.genres)}")
    if metadata.cover_url:
        print(f"  {'cover':<16} {metadata.cover_url}")
    if metadata.description:
        text = metadata.description.replace("\n", " ")
        print(f"  {'description':<16} {text[:140]}{'…' if len(text) > 140 else ''}")


def cmd_history(args: argparse.Namespace) -> int:
    from .history import History

    cfg = _load_config(args)
    with History(cfg.db_path) as store:
        if args.prune is not None:
            counts = store.prune(days_to_keep=args.prune)
            print(
                f"removed {counts['groups']} change group(s), {counts['changes']} change(s), "
                f"{counts['snapshots']} snapshot(s)"
            )
            return EXIT_OK

        if args.stats:
            stats = store.stats()
            from .render import human_bytes

            print(f"  database   {cfg.db_path}")
            print(f"  size       {human_bytes(stats['size_bytes'])}")
            print(f"  groups     {stats['change_group']}")
            print(f"  changes    {stats['change']}")
            print(f"  snapshots  {stats['snapshot']}")
            return EXIT_OK

        if args.path is None:
            # Without a path, list the most recent groups across everything:
            # `forge undo` needs a group id, and printing database statistics
            # here left no way to find one.
            print_recent_groups(store, limit=args.limit)
            return EXIT_OK

        groups = store.groups_for_file(args.path, limit=args.limit)
        if not groups:
            print(f"no recorded changes for {args.path}")
            return EXIT_OK
        unrestorable = False
        for group in groups:
            print(group.describe())
            for change in store.changes_in_group(group.id, file_path=args.path):
                mark = " " if change.restorable else "*"
                unrestorable = unrestorable or not change.restorable
                print(
                    f"   {mark} {change.field:<16} {_change_value(change.old_value):<34}"
                    f" -> {_change_value(change.new_value)}"
                )
        print()
        if unrestorable:
            print("* cannot be restored (image data or derived from the audio)")
        print("undo one of these with: forge undo GROUP_ID  (-n to preview)")
    return EXIT_OK


def _change_value(value: object, width: int = 32) -> str:
    """Render a recorded tag value for the change log.

    An absent value prints as a dash: ``repr(None)`` reads as the four-letter
    string "None", which is indistinguishable from a tag that really said that.
    """

    if value is None:
        return "—"
    text = str(value).replace("\n", " ")
    if len(text) > width:
        text = text[: width - 1] + "…"
    return repr(text)


def print_recent_groups(store, *, limit: int) -> None:
    """List recent change groups with the ids ``forge undo`` takes."""

    groups = store.recent_groups(limit=limit)
    if not groups:
        print("no recorded tag changes yet")
        print("forge records a group every time it writes tags, in triage or from lookup")
        return

    for group in groups:
        files = store.files_in_group(group.id)
        where = Path(files[0]).name if files else "—"
        if len(files) > 1:
            where += f" (+{len(files) - 1} more file(s))"
        print(f"{group.describe()}  {where}")

    print()
    print("forge history PATH   the changes to one file, field by field")
    print("forge undo GROUP_ID  put that group's previous values back (-n to preview)")


def cmd_undo(args: argparse.Namespace) -> int:
    from . import tags as tags_mod
    from .errors import ForgeError
    from .history import History

    cfg = _load_config(args)
    with History(cfg.db_path) as store:
        if args.group_id is None:
            # Undoing something implicitly would be the wrong kind of helpful,
            # so say what the ids are and stop.
            print("forge undo takes a change group id. The most recent are:\n")
            print_recent_groups(store, limit=args.limit)
            return EXIT_USAGE

        group = store.group(args.group_id)
        if group is None:
            print(f"no change group with id {args.group_id}", file=sys.stderr)
            return EXIT_USAGE

        plan = store.restore_values(args.group_id)
        if not plan:
            print("nothing in that group can be restored")
            return EXIT_OK

        print(f"undoing {group.describe()}")
        for path, fields in plan.items():
            print(f"  {path}")
            for field, value in sorted(fields.items()):
                print(f"    {field:<16} -> {value!r}")

        if args.dry_run:
            return EXIT_OK
        if input("\nrestore these values? [y/N] ").strip().casefold() != "y":
            print("cancelled")
            return EXIT_OK

        status = EXIT_OK
        undo_group: int | None = None
        for path, fields in plan.items():
            try:
                before = tags_mod.read_metadata(path)
                changes = tags_mod.diff_metadata(before, fields)
                if not changes:
                    print(f"{path}: already at those values")
                    continue
                tags_mod.write_metadata(path, fields)
                # One group for the whole undo, so it is itself undoable in one
                # step rather than one step per track.
                undo_group = store.record(
                    group.book_key,
                    changes,
                    file_path=path,
                    source="undo",
                    description=f"undo of group {args.group_id}",
                    group_id=undo_group,
                )
                print(f"{path}: restored {len(changes)} field(s)")
            except ForgeError as exc:
                print(f"{path}: {exc}", file=sys.stderr)
                status = EXIT_FAILURES
    return status


def _record_history(args: argparse.Namespace, path: Path, changes: dict, *, source: str,
                    description: str | None = None) -> None:
    """Best-effort history recording: a log failure must not lose a tag write."""

    try:
        from . import config as config_mod
        from .history import History
        from .scan import book_key

        cfg = config_mod.load(args.config)
        with History(cfg.db_path) as store:
            store.record(
                book_key(path), changes, file_path=path, source=source, description=description
            )
    except Exception:
        LOGGER.debug("could not record history for %s", path, exc_info=True)


def cmd_authors(args: argparse.Namespace) -> int:
    """Inspect and correct the author identity registry."""

    from . import authors as authors_mod
    from . import render

    cfg = _load_config(args)
    renderer = render.make_renderer(force_plain=args.plain, no_color=args.no_color)

    with authors_mod.AuthorRegistry(cfg.authors_db_path) as registry:
        if args.rename:
            identity, name = args.rename
            registry.rename(identity, name)
            renderer.info(f"{identity} is now written as {name!r}")
            return EXIT_OK

        if args.link:
            spelling, identity = args.link
            try:
                registry.link(spelling, identity, canonical_name=args.canonical)
            except ValueError as exc:
                renderer.warn(str(exc))
                return EXIT_USAGE
            canonical = registry.canonical_name(spelling)
            renderer.info(f"{spelling!r} is {identity} — written as {canonical!r}")
            return EXIT_OK

        if args.name:
            asin = registry.identity(args.name)
            if asin is None:
                renderer.info(
                    f"{args.name!r} is not in the registry "
                    f"(key {authors_mod.name_key(args.name)!r})"
                )
                return EXIT_OK
            renderer.info(f"{args.name!r} is {asin}")
            renderer.info(f"  written as: {registry.canonical_name(args.name)!r}")
            for spelling in registry.aliases_of(asin):
                renderer.info(f"  also seen as: {spelling!r}")
            return EXIT_OK

        unknown = registry.unknown_names()
        if args.unknown:
            if not unknown:
                renderer.info("every author seen so far has an identity")
                return EXIT_OK
            renderer.info(f"{len(unknown)} spelling(s) with no identity:")
            for spelling, seen in unknown[: args.limit]:
                renderer.info(f"  {spelling}  (seen {seen}×)")
            return EXIT_OK

        stats = registry.stats()
        renderer.info(
            f"{stats['authors']} author(s), {stats['aliases']} spelling(s), "
            f"{stats['unknown']} unplaced"
        )
        multiple = [row for row in registry.identities() if row[2] > 1]
        if multiple:
            renderer.info("")
            renderer.info("authors Amazon spells more than one way:")
            for asin, name, count in multiple[: args.limit]:
                spellings = ", ".join(repr(s) for s in registry.aliases_of(asin))
                renderer.info(f"  {name}  ({count}) {asin}")
                renderer.info(f"      {spellings}")
        if unknown:
            renderer.info("")
            renderer.info(
                f"{len(unknown)} spelling(s) could not be placed; "
                f"`forge authors --unknown` lists them"
            )
        return EXIT_OK


_HANDLERS = {
    "triage": cmd_triage,
    "run": cmd_run,
    "shelve": cmd_shelve,
    "list": cmd_list,
    "tags": cmd_tags,
    "disposition": cmd_disposition,
    "lookup": cmd_lookup,
    "history": cmd_history,
    "undo": cmd_undo,
    "authors": cmd_authors,
}


if __name__ == "__main__":
    raise SystemExit(main())
