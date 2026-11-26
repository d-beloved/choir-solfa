"""Plain data types shared by every stage of the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Note:
    """A note heard in the audio. Times are in seconds."""
    pitch: int
    start: float
    end: float
    amp: float = 1.0


@dataclass
class QNote:
    """A note placed on the rhythm grid. `start` and `end` are cell indexes (end is exclusive)."""
    pitch: int
    start: int
    end: int
    amp: float = 1.0

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Meter:
    """How a bar is laid out in tonic solfa.

    A pulse is one slot between accent marks (| : ¦). A cell is the smallest
    rhythm unit we write (a quarter of a pulse in simple time).
    """
    name: str
    pulses_per_bar: int
    cells_per_pulse: int
    pulses_per_beat: int  # how many pulses one tracked beat holds
    separators: tuple[str, ...]  # marks between pulses inside a bar

    @property
    def cells_per_bar(self) -> int:
        return self.pulses_per_bar * self.cells_per_pulse

    @property
    def cells_per_beat(self) -> int:
        return self.pulses_per_beat * self.cells_per_pulse


METERS: dict[str, Meter] = {
    "2/4": Meter("2/4", 2, 4, 1, (":",)),
    "3/4": Meter("3/4", 3, 4, 1, (":", ":")),
    "4/4": Meter("4/4", 4, 4, 1, (":", "¦", ":")),
    "6/8": Meter("6/8", 6, 2, 3, (":", ":", "¦", ":", ":")),
}


@dataclass
class Score:
    title: str
    doh_pc: int          # pitch class of doh (0 = C)
    tonic_pc: int        # pitch class of the key's tonic (differs from doh in minor)
    mode: str            # "major" or "minor"
    meter: Meter
    tempo_bpm: float
    doh_midi: int        # MIDI number of the unmarked doh
    total_cells: int
    parts: dict[str, list[QNote]]
    kind: str            # "choir" (parts heard), "backing" (lead + backing singers' parts) or "solo" (arranged)
    notes: list[str] = field(default_factory=list)
    words: dict[int, str] = field(default_factory=dict)  # grid cell -> syllables sung there
    words_part: str = "Soprano"  # the line the words are printed under
