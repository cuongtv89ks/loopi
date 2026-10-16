"""The numbers of an evaluation (spec §13, D22): word and character error rates, hit@k, recall@k, the reciprocal
rank, and their 95 % intervals.

Text is compared after NFC normalisation, in lower case, without format characters (zero-width spaces, the byte
order mark), with every punctuation mark and symbol read as a space and the Vietnamese tone mark of an open "oa",
"oe", "uy" always on its second vowel (both "hòa" and "hoà" are in use).
Vietnamese and English are scored by words (a Vietnamese word is a syllable: WER is the syllable error rate);
Korean by characters without spaces (CER: Whisper's spacing of Korean is no error). Rates over several clips add
their errors first (corpus WER), so a long clip weighs more than a short one.
"""
from __future__ import annotations

import math
import random
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

# The five Vietnamese tone marks as combining characters, written as escapes (a raw combining mark in the source is
# composed into its letter by any NFC-normalising tool, which breaks the patterns below): nặng, huyền, sắc, hỏi, ngã.
_TONES = "\u0323\u0300\u0301\u0309\u0303"
# Old style puts the tone of an open "oa", "oe", "uy" on its first vowel (hòa, khỏe, tùy), new style on the second
# (hoà, khoẻ, tuỳ); closed syllables (hoàng, toán) and "qu" words (quý) are written one way only.
_COMBINING = "\u0300-\u036f"  # the combining diacritical marks, as an escaped range
_OLD_STYLE = re.compile(f"(o(?=[{_TONES}][ae])|u(?=[{_TONES}]y))([{_TONES}])([aey])(?![\\w{_COMBINING}])")


def _tone_on_second_vowel(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return unicodedata.normalize("NFC", _OLD_STYLE.sub(lambda m: m.group(1) + m.group(3) + m.group(2), decomposed))


def normalise(text: str) -> str:
    """NFC, lower case, punctuation and symbols as spaces, the new tone-mark placement, whitespace collapsed; format
    characters (zero-width spaces, the byte order mark, soft hyphens, direction marks: category Cf) are dropped."""
    text = unicodedata.normalize("NFC", "".join(c for c in text if unicodedata.category(c) != "Cf")).lower()
    text = "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in text)
    return " ".join(_tone_on_second_vowel(text).split())


def words(text: str) -> list[str]:
    return normalise(text).split()


def chars(text: str) -> list[str]:
    return list(normalise(text).replace(" ", ""))


@dataclass(frozen=True)
class Errors:
    """An alignment of a hypothesis with its reference: the edits and the length of the reference."""

    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0
    reference: int = 0

    @property
    def edits(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def rate(self) -> float | None:
        """Edits per reference token; None without reference tokens."""
        return self.edits / self.reference if self.reference else None

    def __add__(self, other: Errors) -> Errors:
        return Errors(self.substitutions + other.substitutions, self.deletions + other.deletions,
                      self.insertions + other.insertions, self.reference + other.reference)


def align(reference: Sequence[str], hypothesis: Sequence[str]) -> Errors:
    """The Levenshtein alignment (unit costs) of two token lists; of equally cheap ones, the one that prefers a
    match or a substitution over a deletion, and a deletion over an insertion (deterministic)."""
    # Each cell: (cost, substitutions, deletions, insertions); one row at a time.
    previous = [(j, 0, 0, j) for j in range(len(hypothesis) + 1)]
    for i in range(1, len(reference) + 1):
        current = [(i, 0, i, 0)]
        for j in range(1, len(hypothesis) + 1):
            cost, sub, dele, ins = previous[j - 1]
            if reference[i - 1] == hypothesis[j - 1]:
                diagonal = (cost, sub, dele, ins)
            else:
                diagonal = (cost + 1, sub + 1, dele, ins)
            cost, sub, dele, ins = previous[j]
            up = (cost + 1, sub, dele + 1, ins)
            cost, sub, dele, ins = current[j - 1]
            left = (cost + 1, sub, dele, ins + 1)
            current.append(min(diagonal, up, left, key=lambda cell: cell[0]))
        previous = current
    _cost, sub, dele, ins = previous[-1]
    return Errors(sub, dele, ins, len(reference))


def wer(reference: str, hypothesis: str) -> Errors:
    return align(words(reference), words(hypothesis))


def cer(reference: str, hypothesis: str) -> Errors:
    return align(chars(reference), chars(hypothesis))


def recall_at(ranks: Sequence[int], relevant: int, k: int) -> float:
    """Capped recall: the correct moments found in the first ``k`` results over min(``relevant``, ``k``), so a
    question with many correct moments can still score 1 (D22). ``ranks`` holds, for each correct moment that was
    found, the rank (from 1) of its result."""
    if relevant <= 0:
        raise ValueError("a question needs at least one correct moment")
    return sum(1 for rank in ranks if rank <= k) / min(relevant, k)


def hit_at(ranks: Sequence[int], k: int) -> float:
    """1.0 when a correct moment is in the first ``k`` results, else 0.0."""
    return 1.0 if any(rank <= k for rank in ranks) else 0.0


def reciprocal_rank(ranks: Sequence[int]) -> float:
    """1 / the rank of the first correct result; 0.0 when none was found."""
    return 1.0 / min(ranks) if ranks else 0.0


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """The 95 % Wilson score interval of a proportion (questions found, clips with invented text); None for n = 0."""
    if n <= 0:
        return None
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def bootstrap(clips: Sequence[Errors], iterations: int = 2000, seed: int = 0) -> tuple[float, float] | None:
    """A 95 % percentile interval of a corpus error rate, resampling whole clips (the words of one clip are not
    independent); deterministic for a seed. None with fewer than two clips that have reference tokens."""
    usable = [c for c in clips if c.reference]
    if len(usable) < 2:
        return None
    rng = random.Random(seed)
    rates = sorted(
        sum((usable[rng.randrange(len(usable))] for _ in usable), Errors()).rate for _ in range(iterations)
    )
    return rates[int(0.025 * iterations)], rates[int(0.975 * iterations) - 1]
