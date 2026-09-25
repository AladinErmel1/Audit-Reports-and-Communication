"""Turn a parsed report into short-video storyboards.

One short gives the audit at a glance, then there is one short per finding
(what we found, why it matters, what must happen, what management said) and a
final short with the action list. Every scene has on-screen text and a narration
line. Narration is kept to about 150 words per short, which is 45–60 seconds of speech.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .parser import Finding, Report

_NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
                 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve"}
_ABBREV = re.compile(r"\b(?:e\.g|i\.e|etc|No|Nr|Dr|Mr|Ms|Mrs|St|vs|approx|Inc|Ltd|Reg)\.$", re.IGNORECASE)
_KEY_TERMS = re.compile(
    r"\b(not|no|lack|without|fail\w*|duplicate\w*|delay\w*|late|outdated|insufficient|unavailable|"
    r"override|incorrect\w*|inconsisten\w*|exceed\w*|should|must|recommend\w*|risk\w*|fraud\w*|loss\w*|"
    r"contradict\w*|missing|error\w*|approv\w*)\b",
    re.IGNORECASE,
)
_STAT = re.compile(r"(\$\s?[\d,]+(?:\.\d+)?(?:\s?(?:million|billion|[mk]))?|\b\d+(?:\.\d+)?\s?%|\b\d{1,3}(?:,\d{3})+\b|\b\d{2,}\b(?=\s+(?:invoices|payments|transactions|cases|items|days|users|accounts|vendors|employees)))", re.IGNORECASE)


@dataclass
class Scene:
    kind: str  # title | stat | text | stance | list | outro
    label: str
    headline: str
    narration: str
    body: str = ""
    bullets: list[str] = field(default_factory=list)
    stat: str = ""


@dataclass
class Short:
    id: str
    index: int
    kind: str  # overview | finding | actions
    title: str
    risk_level: str
    scenes: list[Scene]
    finding_number: int | None = None
    source: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------- text helpers

def sentences(text: str) -> list[str]:
    parts = re.split(r"(?:(?<=[.!?])|(?<=[.!?][\"”’)]))\s+(?=[A-Z\"“(])", text.strip())
    out: list[str] = []
    for p in parts:
        # Re-attach splits that happened after an abbreviation.
        if out and _ABBREV.search(out[-1]):
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    # Chart titles and table headings end up glued to the text without a full stop.
    return [s.strip() for s in out if len(s.split()) >= 4 and re.search(r"[.!?][\"”’)]?$", s.strip())]


def _score(sentence: str, position: int) -> float:
    s = 0.0
    s += 3.0 * len(_STAT.findall(sentence))
    s += 0.8 * len(_KEY_TERMS.findall(sentence))
    s += 1.0 if position == 0 else 0
    words = len(sentence.split())
    if words > 45:
        s -= 2.0  # long quotations and legal references make poor narration
    if re.search(r"\b(principle|framework|section \d|states that|according to|as quoted|as stated in|is available to)\b", sentence, re.IGNORECASE):
        s -= 2.0
    if re.match(r"(However|Additionally|Furthermore|Moreover|For these same reasons|Likewise|For example)\b", sentence):
        s -= 1.0  # depends on the previous sentence, weak on its own
    return s


def summarize(text: str, max_words: int, max_sentences: int = 3, exclude: str = "") -> list[str]:
    """Extractive summary: highest-scoring sentences, kept in original order."""
    sents = [tidy(s) for s in sentences(text) if s != exclude]
    if not sents:
        return []
    ranked = sorted(range(len(sents)), key=lambda i: -_score(sents[i], i))
    chosen: list[int] = []
    words = 0
    for i in ranked:
        n = len(sents[i].split())
        if chosen and words + n > max_words:
            continue
        chosen.append(i)
        words += n
        if len(chosen) >= max_sentences or words >= max_words:
            break
    return [shorten(sents[i], max_words) for i in sorted(chosen)]


_CONCESSIVE = re.compile(r"^(While|Although|Though|Whereas|Even though)\b")
_MAIN_CLAUSE_START = re.compile(r"(?:the|this|these|those|it|we|management|there|our|staff|[A-Z][\w']*)\b")
_LEAD_INS = re.compile(r"^(?:It is (?:also )?recommended that|We (?:also )?recommend that|Additionally,|Also,|Furthermore,|Moreover,|In addition,)\s+", re.IGNORECASE)


def tidy(sentence: str) -> str:
    """Drop lead-ins and concessive clauses so a sentence stands on its own."""
    s = _LEAD_INS.sub("", sentence).strip()
    if _CONCESSIVE.match(s):
        # Keep the main clause, which carries the point (e.g. "..., the practice increases risk").
        # The last comma that opens a clause: lists inside the concession have commas too.
        starts = [c.end() for c in re.finditer(r",\s+", s)
                  if _MAIN_CLAUSE_START.match(s[c.end():]) and len(s[c.end():].split()) >= 6]
        # A main clause opening with a pronoun would lose what the pronoun refers to.
        if starts and not re.match(r"(?:it|they|this|these|those)\b", s[starts[-1]:], re.IGNORECASE):
            s = s[starts[-1]:]
    return s[:1].upper() + s[1:]


def shorten(sentence: str, max_words: int) -> str:
    """Trim a sentence to max_words, preferring a clause boundary.

    A sentence up to 40% over budget is kept whole: a clipped sentence can
    change the meaning of an audit statement, a slightly long one cannot.
    """
    words = sentence.split()
    if len(words) <= max_words * 1.4:
        return sentence
    cut = " ".join(words[:max_words])
    m = re.search(r"^(.{25,}?)[,;:](?=[^,;:]*$)", cut)
    base = m.group(1) if m else cut
    return base.rstrip(",;: ") + "…"


def find_stat(text: str) -> tuple[str, str] | None:
    """The most telling number in a text and the sentence it comes from."""
    best: tuple[float, str, str] | None = None
    for i, s in enumerate(sentences(text)):
        for m in _STAT.finditer(s):
            value = m.group(1).strip()
            # Skip years, regulation numbers and dates.
            if re.fullmatch(r"(19|20)\d\d", value) or re.search(r"(Reg|section|Stat|Gen)\s*[-\d.]*$", s[: m.start()].strip()[-12:]):
                continue
            weight = 3 if "$" in value else 2.5 if "%" in value else 1
            score = weight - 0.1 * i
            if best is None or score > best[0]:
                best = (score, value, s)
    if not best:
        return None
    return best[1].replace(" ", ""), best[2]


def spoken(text: str) -> str:
    """Make on-screen text read naturally by a text-to-speech engine."""
    t = text.replace("…", ".")
    t = re.sub(r"\$([\d,]+)\.(\d{2})\b", lambda m: f"{m.group(1)} dollars and {int(m.group(2))} cents", t)
    t = re.sub(r"\$([\d,.]+)\s?(million|billion)", r"\1 \2 dollars", t, flags=re.IGNORECASE)
    t = re.sub(r"\$([\d,]+)", r"\1 dollars", t)
    t = re.sub(r"(\d)\s?%", r"\1 percent", t)
    t = re.sub(r"#\s?(\d+)", r"number \1", t)
    t = t.replace("&", " and ").replace("/", " or ")
    t = re.sub(r"\bAP\b", "A P", t)
    t = re.sub(r"\bSOPs?\b", lambda m: "S O Ps" if m.group(0).endswith("s") else "S O P", t)
    t = re.sub(r"\bIAO\b", "the Internal Audit Office", t)
    t = re.sub(r"\bthe the\b", "the", t, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", t).strip()


def say(text: str) -> str:
    """Narration as it appears in captions; `spoken()` adapts it for the voice."""
    return re.sub(r"\s{2,}", " ", text.replace("…", ".")).strip()


def _num(n: int) -> str:
    return _NUMBER_WORDS.get(n, str(n))


STANCE_LINES = {
    "Agrees": "Management agrees",
    "Agrees with reservations": "Management agrees, with reservations",
    "Disagrees": "Management disagrees",
    "Response provided": "Management responded",
    "Not provided": "No management response yet",
}


# ----------------------------------------------------------------- storyboards

def overview_short(report: Report) -> Short:
    n = len(report.findings)
    levels: dict[str, int] = {}
    for f in report.findings:
        levels[f.risk_level] = levels.get(f.risk_level, 0) + 1
    level_text = ", ".join(f"{c} {lvl.lower()}-risk" for lvl, c in levels.items() if lvl != "Unrated")
    objective = summarize(report.objectives or report.executive_summary, 28, 1)
    opinion = summarize(report.opinion, 34, 2)
    scenes = [
        Scene("title", "AUDIT IN 60 SECONDS", report.title,
              say(f"Here is the {report.title} audit in under a minute."),
              body=" · ".join(x for x in [report.reference, report.date] if x)),
    ]
    if objective:
        scenes.append(Scene("text", "WHAT WE LOOKED AT", "The objective", say(objective[0]), body=objective[0]))
    if len(levels) == 1 and "Unrated" not in levels:
        found_line = f"We found {n} issues. {'All' if n > 2 else 'Both' if n == 2 else 'It is'} {'' if n == 1 else f'{n} are '}rated {next(iter(levels)).lower()} risk."
    else:
        found_line = f"We found {n} issues" + (f": {level_text}." if level_text else ".")
    scenes.append(Scene("stat", "WHAT WE FOUND", f"{n} findings", say(found_line), stat=str(n),
        body=level_text.capitalize() if level_text else "Swipe to see each one"))
    scenes.append(Scene("list", "THE FINDINGS", "At a glance",
                        say("Here they are at a glance."),
                        bullets=[f"#{f.number} {f.title}" for f in report.findings[:10]]))
    if opinion:
        scenes.append(Scene("text", "OUR OPINION", "Bottom line", say(" ".join(opinion)), body=" ".join(opinion)))
    scenes.append(Scene("outro", "NEXT", "Swipe up", say("Swipe up to go through each finding."),
                        body="Each finding in under a minute"))
    return Short(id="00-overview", index=0, kind="overview", title="The audit at a glance",
                 risk_level="Info", scenes=scenes,
                 source={"Executive summary": report.executive_summary, "Objectives": report.objectives,
                         "Scope": report.scope, "Opinion": report.opinion})


def finding_short(f: Finding, total: int, index: int) -> Short:
    risk_label = f"{f.risk_level} risk" if f.risk_level != "Unrated" else "Risk not rated"
    scenes = [Scene("title", f"FINDING {f.number} OF {total}", f.title,
                    say(f"Finding {_num(f.number)}: {f.title}. " + (f"Rated {f.risk_level.lower()} risk." if f.risk_level != "Unrated" else "")),
                    body=risk_label)]

    stat = find_stat(f.condition)
    if stat:
        value, sentence = stat
        short_sentence = shorten(tidy(sentence), 26)
        scenes.append(Scene("stat", "THE KEY NUMBER", value, say(short_sentence), body=short_sentence, stat=value))
        found = summarize(f.condition, 26, 1, exclude=sentence)
    else:
        found = summarize(f.condition, 38, 2)
    if found:
        scenes.append(Scene("text", "WHAT WE FOUND", "The issue", say(" ".join(found)), body=" ".join(found)))

    risk = summarize(f.risk or f.cause, 30, 2)
    if risk:
        scenes.append(Scene("text", "WHY IT MATTERS", "The risk", say(" ".join(risk)), body=" ".join(risk)))

    rec = summarize(f.recommendation, 36, 3)
    if rec:
        scenes.append(Scene("list", "WHAT NEEDS TO HAPPEN", "Recommendation",
                            say("We recommend: " + " ".join(rec)), bullets=rec))

    # "Management concurs with the recommendation." only repeats the stance badge.
    resp = [r for r in summarize(f.response, 30, 3)
            if not re.fullmatch(r"Management (?:concurs|agrees) with the recommendations?\.", r)][:2]
    stance_line = STANCE_LINES.get(f.response_stance, "Management responded")
    scenes.append(Scene("stance", "MANAGEMENT RESPONSE", f.response_stance,
                        say(f"{stance_line}. " + " ".join(resp)) if resp else say(stance_line + "."),
                        body=" ".join(resp)))

    due = f"Fix due within {f.deadline_days} days" if f.deadline_days else "Agree a fix date with Internal Audit"
    scenes.append(Scene("outro", "YOUR ACTION", due,
                        say(f"{due.replace('Fix due', 'Corrective action is due')} of the report. Swipe up for the next finding.") if f.deadline_days
                        else say(f"{due}. Swipe up for the next finding."),
                        body="Swipe up for the next finding" if index < total else "Swipe up for your action list"))
    return Short(id=f"{index:02d}-finding-{f.number}", index=index, kind="finding",
                 title=f"#{f.number} {f.title}", risk_level=f.risk_level, scenes=scenes,
                 finding_number=f.number,
                 source={"Finding": f.condition, "Risk / internal control": f.risk,
                         "Recommendation": f.recommendation, "Management response": f.response})


def actions_short(report: Report, index: int) -> Short:
    by_deadline: dict[int | None, list[Finding]] = {}
    for f in report.findings:
        by_deadline.setdefault(f.deadline_days, []).append(f)
    scenes = [Scene("title", "WRAP-UP", "Your action list",
                    say("That's all the findings. Here is what happens next."), body=report.title)]
    for days, items in sorted(by_deadline.items(), key=lambda kv: kv[0] or 10_000):
        label = f"DUE WITHIN {days} DAYS" if days else "DEADLINE TO BE AGREED"
        for start in range(0, len(items), 5):
            part = items[start:start + 5]
            if start:
                line = "And these."
            elif days:
                every = "All " if len(items) == len(report.findings) and len(items) > 1 else ""
                line = f"{every}{len(items)} {'actions are' if len(items) > 1 else 'action is'} due within {days} days of the report."
            else:
                line = f"{len(items)} {'actions still need' if len(items) > 1 else 'action still needs'} a deadline."
            scenes.append(Scene("list", label, f"{len(items)} actions" if days else "Open actions",
                                say(line), bullets=[f"#{f.number} {f.title}" for f in part]))
    scenes.append(Scene("outro", "QUESTIONS?", "Contact Internal Audit",
                        say(f"Questions? Contact {report.sender or 'Internal Audit'}. Tap the details button on any finding to read the full report text."),
                        body=report.sender or report.auditor or "Internal Audit"))
    return Short(id=f"{index:02d}-actions", index=index, kind="actions", title="Your action list",
                 risk_level="Info", scenes=scenes)


def build_shorts(report: Report) -> list[Short]:
    shorts = [overview_short(report)]
    total = len(report.findings)
    for i, f in enumerate(report.findings, start=1):
        shorts.append(finding_short(f, total, i))
    if report.findings:
        shorts.append(actions_short(report, total + 1))
    return shorts
