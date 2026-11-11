"""
Score a search's answers against the labelled questions of :mod:`.benchmark`.

An *answer* to a question is the list of ways that the search has proven, in the sheet's own
form: each way is a set of tokens, and the answer accepts a loadout that contains every token of
at least one way. An answer with no ways accepts nothing, which is what a search that found no
route reports. A question that the search has not tried has no answer at all.

For one question, the sheet puts each loadout in one of three groups:

* *accepted*, when the loadout contains a plain way;
* *conditional*, when it contains no plain way but does contain the tokens of a conditional way;
* *rejected* otherwise.

A caller can *grant* conditions. A conditional way whose conditions are all granted then counts
as a plain way. A search that only has to reach a pickup, for example, is scored with ``QSave``
granted, because the sheet's ways without ``QSave`` also cover the walk back to the door.

An answer can disagree with the sheet in two directions, and they are counted apart because they
cost differently:

* It is *lenient* on a loadout that it accepts and the sheet rejects. In logic, a lenient row
  can make a seed that cannot be finished.
* It is *strict* on a loadout that the sheet accepts and it does not. A strict row only makes
  logic ask for more than is needed.

A loadout in the conditional group counts in neither direction. The sheet says that it works
only with something a token search does not hold, so an answer that accepts it has found a
trick, and an answer that does not has found nothing wrong.

The functions work on whole families of loadouts at once. A family is held as one integer with a
bit for each of the 2,048 loadouts, where a loadout's bit position is the sum of ``1 << i`` over
the positions ``i`` of its tokens in ``TOKENS``.
"""
from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from . import benchmark
from .benchmark import CONDITIONS, KINDS, TOKENS, Question, minimal

Ways = Iterable[Iterable[str]]

LOADOUTS = 1 << len(TOKENS)
_BIT = {token: 1 << position for position, token in enumerate(TOKENS)}


def _number(tokens: Iterable[str]) -> int:
    """Return a loadout's bit position, raising ValueError for a name that is not a token."""
    number = 0
    for token in tokens:
        if token not in _BIT:
            raise ValueError(f"{token!r} is not a movement token; the tokens are"
                             f" {', '.join(TOKENS)}")
        number |= _BIT[token]
    return number


@functools.lru_cache(maxsize=None)
def _containing(way: int) -> int:
    """Return the family of loadouts that contain the way with bit position ``way``."""
    return sum(1 << loadout for loadout in range(LOADOUTS) if loadout & way == way)


def _family(ways: Ways) -> int:
    """Return the family of loadouts that a list of ways accepts."""
    family = 0
    for way in ways:
        family |= _containing(_number(way))
    return family


def accepts(ways: Ways, loadout: Iterable[str]) -> bool:
    """Return True when ``loadout`` contains every token of at least one of ``ways``."""
    return bool(_family(ways) >> _number(loadout) & 1)


@dataclass(frozen=True)
class Label:
    """What the sheet says about one question, once conditions are granted.

    ``ways`` are the minimal ways that count as plain. ``accepted``, ``conditional`` and
    ``rejected`` are the three groups of the module docstring, each as a family of loadouts.
    """

    ways: tuple[frozenset[str], ...]
    accepted: int
    conditional: int
    rejected: int


def label(question: Question, granted: Iterable[str] = ()) -> Label:
    """Return the sheet's label for ``question``, with the conditions in ``granted`` met.

    Raises ValueError for a name in ``granted`` that is not one of ``benchmark.CONDITIONS``.
    """
    granted = frozenset(granted)
    unknown = granted.difference(CONDITIONS)
    if unknown:
        raise ValueError(f"{sorted(unknown)} cannot be granted; the conditions are"
                         f" {', '.join(CONDITIONS)}")
    met = [way.tokens for way in question.conditional if way.conditions <= granted]
    unmet = [way.tokens for way in question.conditional if not way.conditions <= granted]
    ways = minimal([*question.ways, *met])
    accepted = _family(ways)
    conditional = _family(unmet) & ~accepted
    rejected = ((1 << LOADOUTS) - 1) & ~accepted & ~conditional
    return Label(ways, accepted, conditional, rejected)


@dataclass(frozen=True)
class Verdict:
    """How one answer compares with the sheet's label for its question.

    ``accepted`` and ``rejected`` count the loadouts that the sheet accepts and rejects. A
    question with neither is *unscorable*, because every loadout is in the conditional group.

    ``lenient`` counts the rejected loadouts that the answer accepts, and ``strict`` counts the
    accepted loadouts that it does not. ``conditional`` counts the loadouts of the conditional
    group that the answer accepts. All three are 0 for a question with no answer.

    The three lists name ways. ``lenient_ways`` are the answer's ways that the sheet rejects,
    and ``conditional_ways`` are the answer's ways that the sheet has only with a condition.
    ``missed_ways`` are the sheet's ways that the answer does not accept.

    An answer's way is sorted by the loadout that holds exactly its tokens. So a lenient way
    is listed as lenient only, even when larger loadouts that it accepts are in the conditional
    group. ``lenient`` and ``strict`` are above 0 exactly when their lists are not empty, while
    ``conditional`` can be above 0 with ``conditional_ways`` empty.
    """

    question: Question
    answered: bool
    accepted: int
    rejected: int
    lenient: int = 0
    strict: int = 0
    conditional: int = 0
    lenient_ways: tuple[frozenset[str], ...] = ()
    missed_ways: tuple[frozenset[str], ...] = ()
    conditional_ways: tuple[frozenset[str], ...] = ()

    @property
    def scorable(self) -> bool:
        """True when the question is not skipped and the sheet accepts or rejects a loadout."""
        return not self.question.skip and bool(self.accepted or self.rejected)

    @property
    def exact(self) -> bool:
        """True when the answer agrees with the sheet on every loadout the sheet settles."""
        return self.answered and not self.lenient and not self.strict


def answer_ways(answer: Ways) -> tuple[frozenset[str], ...]:
    """Return an answer's minimal ways.

    Raises ValueError when ``answer`` is not a list of ways, when a way is not a list of
    tokens, or when a name is not a token. Text is refused in both places, because ``"Bat"``
    would otherwise be read as the three tokens ``B``, ``a`` and ``t``.
    """
    if isinstance(answer, (str, bytes, Mapping)):
        raise ValueError(f"an answer is a list of ways, not {answer!r}")
    ways = list(answer)
    for way in ways:
        if isinstance(way, (str, bytes, Mapping)):
            raise ValueError(f"a way is a list of tokens, not {way!r}")
        _number(way)
    return minimal(ways)


def judge(question: Question, answer: Optional[Ways], granted: Iterable[str] = ()) -> Verdict:
    """Compare one answer with the sheet's label. ``answer`` is None for a question not tried."""
    sheet = label(question, granted)
    accepted, rejected = sheet.accepted.bit_count(), sheet.rejected.bit_count()
    if answer is None:
        return Verdict(question, False, accepted, rejected)
    ways = answer_ways(answer)
    family = _family(ways)

    def among(group: int, candidates: Iterable[frozenset[str]]) -> tuple[frozenset[str], ...]:
        return tuple(way for way in candidates if group >> _number(way) & 1)

    return Verdict(
        question, True, accepted, rejected,
        lenient=(family & sheet.rejected).bit_count(),
        strict=(sheet.accepted & ~family).bit_count(),
        conditional=(family & sheet.conditional).bit_count(),
        lenient_ways=among(sheet.rejected, ways),
        missed_ways=among(~family, sheet.ways),
        conditional_ways=among(sheet.conditional, ways))


def score(questions: Iterable[Question], answers: Mapping[str, Optional[Ways]],
          granted: Iterable[str] = ()) -> list[Verdict]:
    """Return one verdict for each of ``questions``, in order.

    ``answers`` maps a question's ``key`` to its answer. A question whose key is missing, or
    maps to None, counts as not tried. A skipped question is never judged, whatever its answer.

    Raises ValueError when ``answers`` has a key that belongs to no question, either of
    ``questions`` or of the benchmark. That is nearly always a misspelled key, and ignoring it
    would report the question as not tried.
    """
    questions = list(questions)
    granted = frozenset(granted)
    known = {question.key for question in questions}
    stray = [key for key in answers if key not in known]
    if stray:
        known.update(question.key for question in benchmark.questions())
        stray = [key for key in stray if key not in known]
    if stray:
        more = f" and {len(stray) - 1} more keys" if len(stray) > 1 else ""
        raise ValueError(f"no question has the key {stray[0]!r}{more};"
                         " a key is kind|door|target, as in Question.key")
    verdicts = []
    for question in questions:
        try:
            answer = None if question.skip else answers.get(question.key)
            verdicts.append(judge(question, answer, granted))
        except ValueError as error:
            raise ValueError(f"the answer to {question.key}: {error}") from None
    return verdicts


def load_answers(path: str) -> dict[str, Optional[list]]:
    """Read a file of answers: one JSON object that maps question keys to answers.

    An answer is a list of ways, and a way is a list of token names, so a free question is
    ``[[]]`` and a question with nothing proven is ``[]``. ``null`` marks a question as not
    tried, which leaving the key out also does.
    """
    with open(path, encoding="utf-8-sig") as handle:
        try:
            answers = json.load(handle)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path} is not JSON: {error}") from None
    if not isinstance(answers, dict):
        raise ValueError(f"{path} does not hold a JSON object of answers by question key")
    return answers


# --- Reports --------------------------------------------------------------------------------------

@dataclass
class Tally:
    """Counts over a set of verdicts. :func:`report` says what each one means."""

    questions: int = 0
    answered: int = 0
    exact: int = 0
    lenient: int = 0
    strict: int = 0
    conditional: int = 0
    lenient_loadouts: int = 0
    strict_loadouts: int = 0
    accepted_loadouts: int = 0
    rejected_loadouts: int = 0
    skipped: int = 0
    unscorable: int = 0

    def add(self, verdict: Verdict) -> None:
        if verdict.question.skip:
            self.skipped += 1
            return
        self.conditional += bool(verdict.conditional_ways)
        if not verdict.scorable:
            self.unscorable += 1
            return
        self.questions += 1
        if not verdict.answered:
            return
        self.answered += 1
        self.exact += verdict.exact
        self.lenient += bool(verdict.lenient)
        self.strict += bool(verdict.strict)
        self.lenient_loadouts += verdict.lenient
        self.strict_loadouts += verdict.strict
        self.accepted_loadouts += verdict.accepted
        self.rejected_loadouts += verdict.rejected

    @property
    def lenient_share(self) -> Optional[float]:
        """The share of the loadouts the sheet rejects that the answers accept, or None."""
        return self.lenient_loadouts / self.rejected_loadouts if self.rejected_loadouts else None

    @property
    def strict_share(self) -> Optional[float]:
        """The share of the loadouts the sheet accepts that the answers do not, or None."""
        return self.strict_loadouts / self.accepted_loadouts if self.accepted_loadouts else None


def tally(verdicts: Iterable[Verdict]) -> dict[str, Tally]:
    """Return a :class:`Tally` for each kind of question, and one under ``"all"``."""
    out = {kind: Tally() for kind in (*KINDS, "all")}
    for verdict in verdicts:
        out[verdict.question.kind].add(verdict)
        out["all"].add(verdict)
    return out


def _percent(share: Optional[float]) -> str:
    return "-" if share is None else f"{100 * share:.2f}%"


def report(verdicts: Iterable[Verdict]) -> str:
    """Return a table of the verdicts, with one line for each kind of question and one for all.

    The columns count questions:

    * ``questions``: the scorable ones, which leaves out skipped and unscorable questions.
    * ``answered``: those with an answer. The columns after it count answered questions only.
    * ``exact``: no lenient and no strict loadout.
    * ``lenient`` and ``strict``: at least one such loadout. A question can be in both.
    * ``cond.``: the answer has a way that the sheet has only with a condition. This one also
      counts unscorable questions.

    The last two columns are shares of loadouts over the answered questions. ``lenient %`` is
    the share of the loadouts the sheet rejects that the answers accept, and ``strict %`` is the
    share of the loadouts the sheet accepts that the answers do not.
    """
    counts = tally(verdicts)
    head = (f"{'':8}{'questions':>10}{'answered':>10}{'exact':>8}{'lenient':>9}{'strict':>8}"
            f"{'cond.':>7}{'lenient %':>11}{'strict %':>10}")
    lines = [head]
    for kind, row in counts.items():
        lines.append(
            f"{kind:8}{row.questions:>10}{row.answered:>10}{row.exact:>8}{row.lenient:>9}"
            f"{row.strict:>8}{row.conditional:>7}{_percent(row.lenient_share):>11}"
            f"{_percent(row.strict_share):>10}")
    everything = counts["all"]
    lines.append(f"Left out: {everything.skipped} skipped rows and {everything.unscorable}"
                 " questions with every loadout in the conditional group.")
    return "\n".join(lines)


def describe(way: Iterable[str]) -> str:
    """Return a way as text, such as ``DJump+Panth``, or ``free`` for the empty way."""
    return "+".join(sorted(way, key=TOKENS.index)) or "free"


def disagreements(verdicts: Iterable[Verdict], direction: str) -> list[str]:
    """Return one line for each verdict that disagrees in ``direction``.

    ``direction`` is ``"lenient"``, ``"strict"`` or ``"conditional"``. Each line gives the
    question's key and name, the ways behind the disagreement, and the sheet's ways.
    """
    field = {"lenient": "lenient_ways", "strict": "missed_ways",
             "conditional": "conditional_ways"}[direction]
    lines = []
    for verdict in verdicts:
        ways = getattr(verdict, field)
        if not ways:
            continue
        question = verdict.question
        sheet = [describe(way) for way in question.ways]
        sheet += [f"{describe(way.tokens)} with {'+'.join(sorted(way.conditions))}"
                  for way in question.conditional]
        lines.append(f"{question.key}  ({question.name})  {direction}:"
                     f" {', '.join(describe(way) for way in ways)}"
                     f"  sheet: {', '.join(sheet) or 'impossible'}")
    return lines


# --- Baselines ------------------------------------------------------------------------------------

def free_answers(questions: Iterable[Question]) -> dict[str, list[list[str]]]:
    """Answer every question as free. This is the most lenient answer there is."""
    return {question.key: [[]] for question in questions}


def strict_answers(questions: Iterable[Question]) -> dict[str, list[list[str]]]:
    """Answer every question with no way. This is what a search that proves nothing reports."""
    return {question.key: [] for question in questions}


def common_answers(questions: Iterable[Question]) -> dict[str, list[list[str]]]:
    """Answer every question with the three souls the sheets name most, each alone."""
    return {question.key: [["Bat"], ["HJump"], ["DJump"]] for question in questions}


BASELINES = {"free": free_answers, "nothing proven": strict_answers,
             "Bat, HJump or DJump": common_answers}
