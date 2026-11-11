"""
The command line of the automated routing benchmark.

    python -m worlds.cvaos.tools.autoroute baselines
    python -m worlds.cvaos.tools.autoroute score answers.json --list lenient strict
    python -m worlds.cvaos.tools.autoroute questions --out questions.json
    python -m worlds.cvaos.tools.autoroute split
    python -m worlds.cvaos.tools.autoroute entries --list
    python -m worlds.cvaos.tools.autoroute entries --measure --write
    python -m worlds.cvaos.tools.autoroute trace
    python -m worlds.cvaos.tools.autoroute trace --record traces
    python -m worlds.cvaos.tools.autoroute trace --replay traces --emulator my_module:connect

``baselines``, ``score`` and ``questions`` work on the dev rooms unless ``--rooms`` says
otherwise, so that the held-out rooms are only ever read or scored by asking for them.

``entries --measure``, ``trace --record`` and ``trace --replay`` need an emulator. They talk to
an EmuHawk that ``bizhawk.launch`` started, and that EmuHawk must be running already. With
``--emulator MODULE:NAME`` they run on another emulator. The module is imported, and ``NAME`` in
it is called with no arguments to get an object with the operations of ``emulator.Emulator``.
So ``trace --replay`` and ``entries --measure`` check another emulator against what EmuHawk
gave.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import dataclasses
import importlib
import json
import os
import sys
import time

from ..annotator import settings
from . import benchmark, bizhawk, entries, game, reference, scoring, traces
from .benchmark import TOKENS, Question

ROOMS = ("dev", "held-out", "all")
TRACE_SUFFIX = ".trace.jsonl"


def chosen(rooms: str) -> list[Question]:
    """Return the questions of the ``"dev"`` rooms, the ``"held-out"`` rooms, or ``"all"``."""
    if rooms == "all":
        return list(benchmark.questions())
    return [question for question in benchmark.questions()
            if benchmark.held_out(question.room) == (rooms == "held-out")]


def _heading(options: argparse.Namespace, questions: list[Question]) -> str:
    rooms = len({question.room for question in questions})
    side = "" if options.rooms == "all" else f"{options.rooms} "
    granted = f", with {' and '.join(options.grant)} granted" if options.grant else ""
    return f"{len(questions)} questions in {rooms} {side}rooms{granted}"


def _baselines(options: argparse.Namespace) -> None:
    questions = chosen(options.rooms)
    print(_heading(options, questions))
    for name, answers in scoring.BASELINES.items():
        print(f"\nEvery question answered as: {name}")
        print(scoring.report(scoring.score(questions, answers(questions), options.grant)))


def _score(options: argparse.Namespace) -> None:
    questions = chosen(options.rooms)
    answers = scoring.load_answers(options.answers)
    verdicts = scoring.score(questions, answers, options.grant)
    print(_heading(options, questions))
    print(scoring.report(verdicts))
    keys = {question.key for question in questions}
    elsewhere = sum(1 for key in answers if key not in keys)
    if elsewhere:
        print(f"{elsewhere} answers are for questions outside these rooms, and were not scored.")
    for direction in options.list:
        lines = scoring.disagreements(verdicts, direction)
        print("\n".join([f"\n{direction}: {len(lines)} questions", *lines]))


def record(question: Question, labels: bool = True) -> dict:
    """Return a question as JSON data, with the sheet's ways when ``labels`` is true."""
    out = {"key": question.key, "kind": question.kind, "room": question.room,
           "door": question.door, "target": question.target, "name": question.name,
           "held_out": benchmark.held_out(question.room), "skip": question.skip}
    if labels:
        out["ways"] = [sorted(way, key=TOKENS.index) for way in question.ways]
        out["conditional"] = [{"tokens": sorted(way.tokens, key=TOKENS.index),
                               "conditions": sorted(way.conditions)}
                              for way in question.conditional]
    return out


def _questions(options: argparse.Namespace) -> None:
    records = [record(question, not options.no_labels) for question in chosen(options.rooms)]
    text = json.dumps(records, indent=1)
    if options.out:
        with open(options.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"Wrote {len(records)} questions to {options.out}.")
    else:
        print(text)


def _mix(question: Question) -> str:
    if question.skip:
        return "skipped"
    if frozenset() in question.ways:
        return "free"
    if question.ways:
        return "tokens"
    return "conditional" if question.conditional else "impossible"


def _split(options: argparse.Namespace) -> None:
    if options.write:
        with open(options.rom or settings.us_rom_path(), "rb") as handle:
            rom = handle.read()
        rooms = benchmark.choose_held_out(benchmark.layout_groups(rom))
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), benchmark.HELD_OUT_FILE)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(benchmark.held_out_text(rooms))
        benchmark.held_out_rooms.cache_clear()
        print(f"Wrote {len(rooms)} rooms to {path}.\n")
    columns = ("free", "tokens", "conditional", "impossible", "skipped")
    print(f"{'':18}{'rooms':>7}{'questions':>11}" + "".join(f"{name:>13}" for name in columns))
    for kind in benchmark.KINDS:
        for side in ("dev", "held-out"):
            questions = [question for question in chosen(side) if question.kind == kind]
            counts = collections.Counter(_mix(question) for question in questions)
            rooms = len({question.room for question in questions})
            print(f"{kind:8}{side:10}{rooms:>7}{len(questions):>11}"
                  + "".join(f"{counts[name]:>13}" for name in columns))
    print("\nA question is free when it needs nothing, and under tokens when every plain way"
          " needs a token.\nUnder conditional it has only conditional ways, and under impossible"
          " it has no way at all.")


def _every_enchant(table: dict) -> dict:
    """Return an entry table with the entries that the file leaves out filled in."""
    full = dict(table)
    for (door, boss, enchant), entry in table.items():
        if not enchant:
            for other in entries.ENCHANTS[1:]:
                full.setdefault((door, boss, other), dataclasses.replace(entry, enchant=other))
    return full


def _entry_line(entry: entries.Entry) -> str:
    name = " ".join(filter(None, (entry.door, "boss" if entry.boss else "", entry.enchant)))
    extra = (", hurt" if entry.hurt else "") + (f", by {entry.left_by}" if entry.left_by else "")
    return f"  {name:<26}{entry.outcome} at {entry.x},{entry.y} after {entry.frames} frames{extra}"


@contextlib.contextmanager
def _emulator(options: argparse.Namespace):
    """Yield the emulator that a command runs on, and close it afterwards.

    It is EmuHawk, unless ``--emulator`` named another one (see the top of this module).
    """
    if options.emulator:
        module, _, name = options.emulator.partition(":")
        try:
            made = getattr(importlib.import_module(module), name)()
        except (ImportError, AttributeError) as error:
            raise ValueError(f"--emulator {options.emulator} can't be used: {error}") from error
    else:
        made = bizhawk.BizHawk(port=options.port or bizhawk.PORT)
    try:
        yield made
    finally:
        close = getattr(made, "close", None)
        if close is not None:
            close()


def _measure_entries(options: argparse.Namespace) -> dict:
    found = {}
    with _emulator(options) as hawk:
        session = game.Session(hawk)
        session.boot()
        base = hawk.save()
        for door, boss in entries.visits(options.measure):
            for enchant in entries.ENCHANTS:
                hawk.load(base)
                try:
                    entry = entries.arrive(session, door, boss, [enchant] if enchant else [])
                except game.GameError as error:
                    print(f"  {door}: {error}")
                    continue
                found[entry.key] = entry
    return found


def _entries(options: argparse.Namespace) -> None:
    table = {}
    if os.path.exists(entries.entry_path()):
        table = _every_enchant(entries.load_entries())
    if options.measure is not None:
        found = _measure_entries(options)
        changed = [key for key in found if table.get(key) != found[key]]
        print(f"Measured {len(found)} entries, of which {len(changed)} differ from the file.")
        for key in changed:
            if key in table:
                print(_entry_line(table[key]).replace("  ", "- ", 1))
            print(_entry_line(found[key]).replace("  ", "+ ", 1))
        table.update(found)
        if options.write:
            with open(entries.entry_path(), "w", encoding="utf-8", newline="\n") as handle:
                handle.write(entries.entries_text(table.values()))
            print(f"Wrote {len(table)} entries to {entries.entry_path()}.")
        print()
    doors = {door for door, _, _ in table}
    print(f"{len(table)} entries of {len(doors)} doors")
    print(f"{'':12}" + "".join(f"{enchant or 'none':>8}" for enchant in entries.ENCHANTS))
    counts = collections.Counter((entry.outcome, entry.enchant) for entry in table.values())
    for outcome in entries.OUTCOMES:
        print(f"{outcome:12}"
              + "".join(f"{counts[outcome, enchant]:>8}" for enchant in entries.ENCHANTS))
    plain = [entry for entry in table.values() if not entry.enchant]
    print("\nWith no enchant soul, the entries that leave or don't settle:")
    for entry in plain:
        if entry.outcome != "settles":
            print(_entry_line(entry))
    print("\nThe entries that an enchant soul changes:")
    for entry in table.values():
        same = dataclasses.replace(table[entry.door, entry.boss, ""], enchant=entry.enchant)
        if entry != same:
            print(_entry_line(entry))
    away = [(entry, entries.offset(entry)) for entry in plain if entry.outcome == "settles"]
    away = [(entry, by) for entry, by in away if by[0] or abs(by[1]) > 1]
    print(f"\nWith no enchant soul, {len(away)} entries settle away from the door's landing"
          + (":" if options.list else " (--list shows them)."))
    if options.list:
        for entry, by in away:
            print(f"{_entry_line(entry)}, {by[0]:+},{by[1]:+} from the landing")


def _trace_files(paths: list[str]) -> list[str]:
    """Return the trace files that ``paths`` name, taking every trace file of a folder."""
    found = []
    for path in paths:
        if os.path.isdir(path):
            found.extend(os.path.join(path, name) for name in sorted(os.listdir(path))
                         if name.endswith(TRACE_SUFFIX))
        else:
            found.append(path)
    return found


def _mismatch_line(mismatch: traces.Mismatch) -> str:
    """Describe a read that differs by its first differing bytes, since a read can be long."""
    op = mismatch.op
    if op[0] != "read":
        return str(mismatch)
    want, got = bytes.fromhex(op[2]), bytes.fromhex(mismatch.got)
    at = next((index for index, pair in enumerate(zip(want, got)) if pair[0] != pair[1]),
              min(len(want), len(got)))
    return (f"operation {mismatch.index}, {mismatch.frames} frames after the reset: the read at"
            f" {op[1]} differs from byte {at} on, where the trace has {want[at:at + 8].hex()}"
            f" and the emulator gave {got[at:at + 8].hex()}")


def _trace(options: argparse.Namespace) -> None:
    if options.record is not None:
        os.makedirs(options.record, exist_ok=True)
        with _emulator(options) as emulator:
            for name in options.scenarios or reference.SCENARIOS:
                started = time.monotonic()
                try:
                    ops = reference.run(name, emulator)
                except reference.ScenarioError as error:
                    print(f"{name}: not recorded. {error}")
                    continue
                about = reference.about(name)
                if options.emulator:
                    about["reference"] = {"emulator": options.emulator}
                path = os.path.join(options.record, name + TRACE_SUFFIX)
                traces.save_trace(path, about, ops)
                frames = sum(op[2] for op in ops if op[0] == "step")
                print(f"{name}: {len(ops)} operations over {frames} frames in"
                      f" {time.monotonic() - started:.0f} s, written to {path}")
    elif options.replay:
        with _emulator(options) as emulator:
            for path in _trace_files(options.replay):
                _, ops = traces.load_trace(path)
                found = traces.replay(ops, emulator, digests=not options.no_digests,
                                      stop_after=options.limit or None)
                reads = sum(1 for op in ops if op[0] == "read"
                            or (op[0] == "digest" and not options.no_digests))
                if not found:
                    print(f"{os.path.basename(path)}: all {reads} reads match")
                    continue
                stopped = len(found) == options.limit
                print(f"{os.path.basename(path)}: {len(found)} of {reads} reads differ"
                      + (", and the replay stopped there" if stopped else ""))
                for mismatch in found:
                    print(f"  {_mismatch_line(mismatch)}")
    else:
        for name, scenario in reference.SCENARIOS.items():
            print(f"{name:10}{(scenario.__doc__ or '').strip().partition(chr(10))[0]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m worlds.cvaos.tools.autoroute",
        description="Score an automated routing search against the requirement sheets, and show"
                    " the state that a search starts from at each door.")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, run, text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=text, description=text)
        sub.set_defaults(run=run)
        return sub

    def add_rooms(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--rooms", choices=ROOMS, default="dev",
                         help="which rooms to use (default: dev)")

    def add_grant(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--grant", nargs="+", default=[], choices=benchmark.CONDITIONS,
                         metavar="CONDITION",
                         help=f"conditions to count as met: {', '.join(benchmark.CONDITIONS)}")

    def add_emulator(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--port", type=int,
                         help="the port of EmuHawk's bridge (default: the one bizhawk.launch uses)")
        sub.add_argument("--emulator", metavar="MODULE:NAME",
                         help="run on what MODULE's NAME returns when called, and not on EmuHawk")

    sub = command("baselines", _baselines, "Print the scores of three answers that use no search.")
    add_rooms(sub)
    add_grant(sub)

    sub = command("score", _score, "Score a JSON file of answers.")
    sub.add_argument("answers", help="a JSON object that maps question keys to lists of ways")
    add_rooms(sub)
    add_grant(sub)
    sub.add_argument("--list", nargs="+", default=[], choices=("lenient", "strict", "conditional"),
                     help="also list the questions that disagree in these directions")

    sub = command("questions", _questions, "Write the questions out as JSON.")
    add_rooms(sub)
    sub.add_argument("--out", help="the file to write (default: print to the terminal)")
    sub.add_argument("--no-labels", action="store_true", help="leave out the sheet's ways")

    sub = command("split", _split, "Show how the questions divide between dev and held-out rooms.")
    sub.add_argument("--write", action="store_true",
                     help="work the held-out rooms out again from the ROM and save them")
    sub.add_argument("--rom", help="the USA ROM to read with --write (default: the annotator's)")

    sub = command("entries", _entries, "Show where Soma ends up after he arrives by each door.")
    sub.add_argument("--list", action="store_true",
                     help="also list the entries that settle away from the door's landing")
    sub.add_argument("--measure", nargs="*", metavar="ROOM",
                     help="measure the entries again on an emulator: of every room, or of these")
    add_emulator(sub)
    sub.add_argument("--write", action="store_true",
                     help="save what --measure found in the entry file")

    sub = command("trace", _trace, "Record the reference scenarios on an emulator, or replay"
                                   " their traces on one. With neither, list the scenarios.")
    sub.add_argument("--record", metavar="FOLDER",
                     help="play the scenarios and write their traces to this folder")
    sub.add_argument("--scenarios", nargs="+", choices=tuple(reference.SCENARIOS), metavar="NAME",
                     help="the scenarios to record (default: all of them)")
    sub.add_argument("--replay", nargs="+", metavar="PATH",
                     help="trace files to replay, or folders of them")
    sub.add_argument("--no-digests", action="store_true",
                     help="skip the digests of whole memory blocks when replaying")
    sub.add_argument("--limit", type=int, default=10, metavar="N",
                     help="stop replaying a file once N reads differ (default: 10, 0: no limit)")
    add_emulator(sub)

    options = parser.parse_args()
    failures = (OSError, ValueError, bizhawk.BizHawkError, game.GameError, traces.TraceError)
    try:
        options.run(options)
    except failures as error:
        sys.exit(f"error: {error}")


if __name__ == "__main__":
    main()
