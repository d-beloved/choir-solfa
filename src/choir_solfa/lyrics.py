"""Song words: read or find them, split them into syllables, and put each syllable under a soprano note.

Words come from the person (pasted, any language) or, for links, from LRCLIB, a free
lyrics library that often has the time each line starts. Syllables are matched to the
soprano notes in order, preferring to start lines after a rest, to give repeated notes
their own syllable and to keep each line near its time when there is one.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass

import numpy as np

from .models import Meter, QNote
from .notation import cell_states, join_syllables

MAX_CHARS = 10_000
LRCLIB = "https://lrclib.net/api"
_UA = "choir-solfa/0.1 (https://github.com/d-beloved/choir-solfa)"
TIMED_WITHIN = 3.0  # seconds: line times are used only from a recording this close in length


@dataclass
class Line:
    text: str
    time: float | None = None  # seconds into the recording, when known


@dataclass
class Words:
    lines: list[Line]
    source: str = "you"  # "you" or "online"
    found: str = ""      # what was found online, e.g. "Silent Night by Tyler Shaw"

    @property
    def timed(self) -> bool:
        return bool(self.lines) and all(line.time is not None for line in self.lines)


# ---------------------------------------------------------------- reading

_TIME = re.compile(r"^\s*\[(\d+):(\d+(?:\.\d+)?)\]")
_LABEL = re.compile(r"^\W*(verse|chorus|refrain|bridge|pre-?chorus|intro|outro|hook|interlude|coda|tag|repeat|"
                    r"ending|instrumental|solo|all|choir|lead|call|response)\W*(\d+|[ivx]+)?\W*(x\s*\d+)?\W*$", re.I)
_TIMES_NOTE = re.compile(r"\(\s*(?:x|×)?\s*\d+\s*(?:x|×)?\s*\)", re.I)


def parse(text: str, asides: bool = True) -> list[Line]:
    """Lines of words, from plain text or LRC ("[01:02.50] words"). Section labels are dropped.

    With `asides=False`, words in brackets are dropped too: in lyrics found online they are
    usually backing voices or ad-libs ("(ooh, yeah)") that the melody doesn't carry."""
    lines: list[Line] = []
    for raw in (text or "")[:MAX_CHARS].splitlines():
        times = []
        while m := _TIME.match(raw):
            times.append(int(m[1]) * 60 + float(m[2]))
            raw = raw[m.end():]
        raw = re.sub(r"\[[^\]]*\]", " ", raw)  # [Chorus], [x2] and other notes
        raw = _TIMES_NOTE.sub(" ", raw)
        if not asides:
            raw = re.sub(r"\([^)]*\)?", " ", raw)
        raw = re.sub(r"\s+([,;:.!?])", r"\1", " ".join(raw.split())).lstrip(",;:.!? ")
        if not raw or _LABEL.match(raw) or not any(ch.isalpha() for ch in raw):
            continue
        for t in times or [None]:
            lines.append(Line(raw, t))
    if any(line.time is None for line in lines):
        for line in lines:
            line.time = None
    else:
        lines.sort(key=lambda line: line.time)
    return lines


# ---------------------------------------------------------------- finding online

_NOISE = re.compile(r"\b(official|music|lyrics?|video|audio|visuali[sz]er|hd|hq|4k|live|performance|satb|ssa|ttbb|"
                    r"sab|choir|choral|cover|version|arr|arranged|acapella|a cappella|karaoke|instrumental|"
                    r"ft|feat|featuring)\b\.?", re.I)


def clean_title(title: str) -> str:
    """A video title without the extras: "Silent Night (Official Video) | SATB" -> "Silent Night"."""
    t = re.sub(r"[(\[{][^)\]}]*[)\]}]", " ", title or "")
    t = re.split(r"\s[|•~/]\s|\s//\s", t)[0]
    t = _NOISE.sub(" ", t)
    t = re.sub(r"\s[-–—:]\s*$|^\s*[-–—:]\s", " ", " ".join(t.split()))
    return " ".join(t.split()).strip(" -–—:")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").lower()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _contains(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


def _artist_in(artists: str, text: str) -> bool:
    for name in re.split(r"[,&;]|\bfeat\.?\b|\band\b", artists or "", flags=re.I):
        name = _norm(name)
        if len(name) >= 3 and _contains(text, name):
            return True
    return False


def _request(url: str) -> object:
    proxy = os.environ.get("CHOIR_SOLFA_PROXY")  # on the website, through the same tunnel as the song
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.build_opener(*handlers).open(req, timeout=15) as r:
        return json.loads(r.read(3_000_000).decode("utf-8", "replace"))


def find(title: str, artist: str = "", duration: float = 0.0, also: str = "", get=_request) -> Words | None:
    """Look the song up on LRCLIB. `also` is other text that may name the artist (such as the channel)."""
    title = (title or "").strip()
    if not _norm(title):
        return None
    wanted = _norm(title)
    context = _norm(f"{title} {artist} {also}")
    found: list[dict] = []
    if artist:
        try:
            hit = get(f"{LRCLIB}/get?" + urllib.parse.urlencode(
                {"track_name": title, "artist_name": artist, **({"duration": round(duration)} if duration else {})}))
            if isinstance(hit, dict):
                found.append(hit)
        except Exception:
            pass
    for query in ([f"{artist} {title}"] if artist else []) + [title]:
        try:
            results = get(f"{LRCLIB}/search?" + urllib.parse.urlencode({"q": query}))
        except Exception:
            continue
        if isinstance(results, list):
            found += [r for r in results if isinstance(r, dict)]

    usable = []
    seen = set()
    for r in found:
        if r.get("id") in seen or r.get("instrumental") or not (r.get("plainLyrics") or r.get("syncedLyrics")):
            continue
        seen.add(r.get("id"))
        name = _norm(r.get("trackName", ""))
        if len(name) < 3 or not (_contains(wanted, name) or _contains(name, wanted)):
            continue
        usable.append(r)
    if not usable:
        return None

    def closeness(r: dict) -> float:
        if not duration or not r.get("duration"):
            return 60.0
        return abs(float(r["duration"]) - duration)

    by_artist = [r for r in usable if _artist_in(r.get("artistName", ""), context)]
    if by_artist:
        pool = by_artist
    else:
        # No artist we can check: take the words only when most versions agree on them
        # (a hymn or carol), so a different song with the same name isn't used.
        groups: dict[str, list[dict]] = {}
        for r in usable:
            groups.setdefault(_opening(r), []).append(r)
        pool = max(groups.values(), key=len)
        if len(pool) < 2 or len(pool) < 0.6 * len(usable):
            return None
    # Line times only fit the same recording: the artist must match, and the length too.
    def usable_times(r: dict) -> bool:
        return bool(by_artist and r.get("syncedLyrics")) and closeness(r) <= TIMED_WITHIN

    pool.sort(key=lambda r: (not usable_times(r), closeness(r)))
    best = pool[0]
    timed = usable_times(best)
    lines = parse(best["syncedLyrics"] if timed else (best.get("plainLyrics") or _untimed(best["syncedLyrics"])),
                  asides=False)
    if not lines:
        return None
    name, by = best.get("trackName") or title, best.get("artistName") or ""
    return Words(lines, "online", f"{name} by {by}" if by else name)


def _untimed(lrc: str) -> str:
    return "\n".join(_TIME.sub("", line) for line in (lrc or "").splitlines())


def _opening(r: dict) -> str:
    text = r.get("plainLyrics") or _untimed(r.get("syncedLyrics") or "")
    words = _norm(" ".join(line.text for line in parse(text)[:2])).split()
    return " ".join(words[:6])


# ---------------------------------------------------------------- syllables

_ONSETS = {"bl", "br", "ch", "cl", "cr", "dr", "fl", "fr", "gl", "gr", "pl", "pr", "sc", "sh", "sk", "sl", "sm", "sn",
           "sp", "st", "sw", "th", "tr", "tw", "wh", "wr", "ph", "gb", "kp", "gh", "qu"}
_ENGLISH = {"the", "and", "you", "i", "is", "of", "to", "my", "in", "a", "me", "your", "all", "be", "for", "will",
            "we", "he", "his", "our", "with", "on", "that", "it", "are", "lord", "god", "love", "o", "oh"}


def looks_english(text: str) -> bool:
    words = _norm(text).split()
    return bool(words) and sum(w in _ENGLISH for w in words) >= 0.12 * len(words)


def _base(ch: str) -> str:
    return unicodedata.normalize("NFD", ch)[0].lower()


def _marked(ch: str) -> bool:
    return len(unicodedata.normalize("NFD", ch)) > 1


def syllables(word: str, english: bool = True) -> list[str]:
    """Split one word into sung syllables: "Silent" -> ["Si", "lent"], "Olúwa" -> ["O", "lú", "wa"]."""
    m = re.match(r"^(\W*)(.*?)(\W*)$", word, re.S)
    lead, core, trail = m.groups()
    if not core:
        return [word]
    if "-" in core:
        parts = [p for p in core.split("-") if p]
        out: list[str] = []
        for p in parts:
            out += syllables(p, english)
        if not out:
            return [word]
        out[0], out[-1] = lead + out[0], out[-1] + trail
        return out
    n = len(core)
    low = [_base(ch) for ch in core]

    def vowel(i: int) -> bool:
        ch = low[i]
        if ch in "aeio":
            return True
        if ch == "u":
            return not (i > 0 and low[i - 1] == "q")
        if ch == "y":
            return i > 0 and not (i + 1 < n and low[i + 1] in "aeiou")
        return False

    nuclei: list[list[int]] = []
    for i in range(n):
        if not vowel(i):
            continue
        if nuclei and nuclei[-1][1] == i and not (_marked(core[i]) and not english):
            nuclei[-1][1] = i + 1
        else:
            nuclei.append([i, i + 1])
    if english and len(nuclei) >= 2:
        w = "".join(low)
        last = nuclei[-1]
        if last == [n - 1, n] and w[-1] == "e" and not (n >= 3 and w[-2] == "l" and not vowel(n - 3)):
            nuclei.pop()  # love, grace, peace
        elif last == [n - 2, n - 1] and w.endswith("es") and w[-3] not in "sxzh" and not w.endswith(("ces", "ges")):
            nuclei.pop()  # comes, names
        elif last == [n - 2, n - 1] and w.endswith("ed") and w[-3] not in "td":
            nuclei.pop()  # loved, saved
    if len(nuclei) <= 1:
        return [word]
    cuts = []
    for a, b in zip(nuclei, nuclei[1:]):
        cons = "".join(low[a[1]:b[0]])
        k = len(cons)
        if k == 0:
            cut = b[0]
        elif k == 1:
            cut = a[1]
        elif english and b == [n - 1, n] and low[-2:] == ["l", "e"] and k >= 2:
            cut = b[0] - 2  # lit-tle, can-dle
        elif k == 2 and cons in ("ng", "ck"):
            cut = b[0]
        elif cons[-2:] in _ONSETS:
            cut = b[0] - 2 if k > 2 else a[1]
        else:
            cut = b[0] - 1
        cuts.append(cut)
    pieces = [core[i:j] for i, j in zip([0, *cuts], [*cuts, n])]
    pieces = [p for p in pieces if p]
    pieces[0], pieces[-1] = lead + pieces[0], pieces[-1] + trail
    return pieces


@dataclass
class Syllable:
    text: str          # as printed: "Si-", "lent"
    line: int
    first: bool        # first syllable of its line
    word_start: bool


def split_lines(lines: list[Line]) -> list[Syllable]:
    english = looks_english(" ".join(line.text for line in lines))
    out: list[Syllable] = []
    for li, line in enumerate(lines):
        first = True
        for word in line.text.split():
            if not any(ch.isalnum() for ch in word):  # a dash or a stray mark is not sung
                continue
            parts = syllables(word, english)
            for k, part in enumerate(parts):
                out.append(Syllable(part + ("-" if k < len(parts) - 1 else ""), li, first, k == 0))
                first = False
    return out


# ---------------------------------------------------------------- placing

SKIP_EDGE, SKIP = 0.6, 1.2          # a note with no words, at the very start or end / in the middle
HOLD, HOLD_SAME, HOLD_REST = 0.5, 1.5, 4.0  # one syllable carried on to the next note
HOLD_SAME_OFFBEAT = 0.6              # ... onto the same pitch starting inside a pulse (often one note heard twice)
HOLD_CLIPPED = 0.15                  # ... when that note is much shorter than the one before
HOLD_AFTER_LONG = 0.5                # ... or comes after a long note
HOLD_LONG = 0.1                      # ... per beat that note is held beyond one
HOLD_PHRASE_END = 0.3                # less when the syllable ends a line or comes before a comma
SHARE = 1.8                          # two syllables on one note
LINE_MID_PHRASE = 1.5                # a line starting without a breath before it
BREATH_IN_LINE, BREATH_IN_WORD = 0.8, 3.0
COMMA_MID_PHRASE = 0.8               # words after a comma starting without a breath
GAP_IN_LINE = 1.0                    # per bar of rest beyond one, in the middle of a line
LATE = 0.25                          # per beat away from the line's time
LINE_LEFT_OUT = 4.0                  # a whole line not sung (found lyrics often have more verses)
LINE_LEFT_OUT_PER = 0.1              # ... per syllable in it
FIRST_LINE_LEFT_OUT = 4.0            # ... more for the song's first line, which is nearly always sung
INF = 1e18


def place(lines: list[Line], soprano: list[QNote], total: int, meter: Meter,
          time_cell=None) -> dict[int, str]:
    """Map grid cells to the syllables sung there. `time_cell(seconds)` turns line times into cells."""
    syls = split_lines(lines)
    pitch, onset = cell_states(soprano, total)
    starts = [c for c in range(total) if onset[c]]
    n, k = len(syls), len(starts)
    if not n or not k or n > 8 * k:
        return {}
    cpp = meter.cells_per_pulse
    ends = []
    for c in starts:
        e = c + 1
        while e < total and pitch[e] is not None and not onset[e]:
            e += 1
        ends.append(e)
    rest_before = np.array([j == 0 or starts[j] - ends[j - 1] >= max(1, cpp // 2) for j in range(k)])
    bar = cpp * meter.pulses_per_bar
    long_gap = np.array([0.0] + [min(max(starts[j] - ends[j - 1] - bar, 0) / bar, 3.0) for j in range(1, k)])
    long_before = np.array([j > 0 and ends[j - 1] - starts[j - 1] >= 2 * cpp for j in range(k)])
    breath = np.where(rest_before, 1.0, np.where(long_before, 0.5, 0.0))
    same = np.array([j > 0 and pitch[starts[j]] == pitch[starts[j - 1]] for j in range(k)])
    # After a long note, a short one usually gets its own syllable (Si- . lent).
    clipped = np.array([j > 0 and 2 * (ends[j] - starts[j]) <= ends[j - 1] - starts[j - 1] for j in range(k)])
    on_pulse = np.array([c % cpp == 0 for c in starts])
    hold = np.where(rest_before, HOLD_REST, np.where(same, np.where(on_pulse, HOLD_SAME, HOLD_SAME_OFFBEAT), HOLD)) + np.where(clipped, HOLD_CLIPPED, 0.0) + np.where(long_before, HOLD_AFTER_LONG, 0.0)
    hold = hold + HOLD_LONG * np.clip(np.array([(e - c) / cpp - 1 for c, e in zip(starts, ends)]), 0, 4)

    first = np.array([s.first for s in syls])
    word_start = np.array([s.word_start for s in syls])
    after_stop = np.array([i > 0 and syls[i - 1].text.rstrip()[-1:] in ",;:.!?" for i in range(n)])
    breath_in_line = np.where(word_start, BREATH_IN_LINE, BREATH_IN_WORD)
    # B[i] holds syllable i - 1; it ends a phrase when the next syllable starts a line or follows a comma.
    phrase_end = np.array([i > 0 and (i == n or syls[i].first or after_stop[i]) for i in range(n + 1)])
    timed = time_cell is not None and all(line.time is not None for line in lines)
    if timed:
        line_cell = [time_cell(line.time) for line in lines]
        lo = np.array([line_cell[s.line] for s in syls], dtype=float)
        hi = np.array([line_cell[s.line + 1] if s.line + 1 < len(lines) else total for s in syls], dtype=float)

    def start_cost(j: int) -> np.ndarray:
        b = breath[j]
        rest = float(rest_before[j])
        cost = np.where(first, LINE_MID_PHRASE * (1 - b),
                        np.where(after_stop, COMMA_MID_PHRASE * (1 - b), breath_in_line * rest)
                        + GAP_IN_LINE * long_gap[j])
        if timed:
            c = starts[j]
            away = np.where(first, np.abs(c - lo), np.maximum(lo - c, 0) + np.maximum(c - hi, 0))
            cost = cost + LATE * np.minimum(away / cpp, 24)
        return cost

    # A whole line can be left out: from its first syllable straight to the next line's.
    line_starts = [i for i, s in enumerate(syls) if s.first] + [n]
    line_jumps = [(a, b, LINE_LEFT_OUT + LINE_LEFT_OUT_PER * (b - a) + (FIRST_LINE_LEFT_OUT if a == 0 else 0.0))
                  for a, b in zip(line_starts, line_starts[1:])]

    def leave_out(state: np.ndarray, record: np.ndarray) -> None:
        for a, b, cost in line_jumps:
            if state[a] + cost < state[b]:
                state[b] = state[a] + cost
                record[b] = a

    skip_cost = np.full(n + 1, SKIP)
    skip_cost[0] = skip_cost[n] = SKIP_EDGE
    A = np.full(n + 1, INF)  # last note had no syllable (or no note yet)
    B = np.full(n + 1, INF)  # last note is sung
    A[0] = 0.0
    from_a = np.zeros((k + 1, n + 1), dtype=np.int8)   # A came from: 0 A, 1 B
    how_b = np.zeros((k + 1, n + 1), dtype=np.int8)    # B came from: 0 start after A, 1 start after B, 2 hold
    shared = np.full((k + 1, n + 1), -1, dtype=np.int32)  # B: syllables shared on the note, from this index
    left_a = np.full((k + 1, n + 1), -1, dtype=np.int32)  # lines left out to reach here, from this index
    left_b = np.full((k + 1, n + 1), -1, dtype=np.int32)
    leave_out(A, left_a[0])
    idx = np.arange(n + 1)
    for j in range(k):
        best = np.minimum(A, B)
        nA = best + skip_cost
        from_a[j + 1] = (B < A).astype(np.int8)
        nB = np.full(n + 1, INF)
        nB[1:] = best[:-1] + start_cost(j)
        how = np.where(B[:-1] < A[:-1], 1, 0).astype(np.int8)
        held = B + np.maximum(hold[j] - HOLD_PHRASE_END * phrase_end, 0.1)
        held[0] = INF
        better = held < np.concatenate([[INF], nB[1:]])
        nB = np.where(better, held, nB)
        how_b[j + 1] = np.where(better, 2, np.concatenate([[0], how]))
        # several syllables on this note: nB[m] = min over i <= m of nB[i] + (m - i) * SHARE
        v = nB - idx * SHARE
        run = np.minimum.accumulate(v)
        origin = np.maximum.accumulate(np.where(v <= run, idx, 0))
        moved = run + idx * SHARE < nB - 1e-9
        shared[j + 1] = np.where(moved, origin, -1)
        nB = np.where(moved, run + idx * SHARE, nB)
        leave_out(nA, left_a[j + 1])
        leave_out(nB, left_b[j + 1])
        A, B = nA, nB

    words: dict[int, list[str]] = {}
    i, j, s = n, k, (1 if B[n] < A[n] else 0)
    if min(A[n], B[n]) >= INF:
        return {}
    while j > 0 or i > 0:
        left = (left_b if s else left_a)[j][i]
        if left >= 0:
            i = int(left)
            continue
        if j == 0:
            break
        if s == 0:
            s = int(from_a[j][i])
            j -= 1
            continue
        if shared[j][i] >= 0:
            i0 = int(shared[j][i])
            words.setdefault(starts[j - 1], [])[:0] = [syls[x].text for x in range(i0, i)]
            i = i0
            continue
        how = int(how_b[j][i])
        if how == 2:
            j -= 1
            continue
        words.setdefault(starts[j - 1], []).insert(0, syls[i - 1].text)
        i, j, s = i - 1, j - 1, how
    return {c: join_syllables(t) for c, t in words.items()}
