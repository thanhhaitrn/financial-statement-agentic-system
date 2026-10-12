"""Topic matching between a question and news text, used to pick articles
and paragraphs about what was asked rather than the newest or the first.

Vietnamese is matched on folded syllables (no diacritics) and syllable pairs.
Folding merges words such as "tức" (tin tức) and "tức" (cổ tức), so known
compounds are joined first and generic news phrases are removed as phrases.
The question is only used in memory for ranking; it is never stored.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# Joined before tokenizing: their syllables are also function words.
_COMPOUNDS = ("co phieu", "co tuc", "co dong", "co phan", "von co", "gia von", "no vay", "tai san")
# Phrases every news question shares; removed from the question as a whole.
_GENERIC_PHRASES = (
    "tin tuc", "thong tin", "su kien", "bao chi", "truyen thong", "gan day", "moi nhat", "vua qua",
    "the nao", "ra sao", "bao nhieu", "co khong", "anh huong", "tac dong", "lien quan", "lien he",
    "giai thich", "nguyen nhan", "vi sao", "tai sao", "ly do", "cong ty", "doanh nghiep", "hien nay",
    "xet theo", "dua tren",
)
# Function syllables.
_STOP = frozenset("""
va cua cac nhung duoc cho trong voi la co nay da se mot theo tu ve de khi den tai nho do nen thi
nhu vao ra len bi rang cung con hay hoac neu sau truoc tren duoi giua qua lai nua thoi roi tin moi
nhat gi sao khong phan ngay thang nam quy tuan hom gan day toi muon xem biet hoi
""".split())


def fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").replace("đ", "d").replace("Đ", "D"))
    value = "".join(char for char in value if not unicodedata.combining(char)).lower()
    value = " ".join(re.sub(r"[^0-9a-z%]+", " ", value).split())
    spaced = f" {value} "
    for compound in _COMPOUNDS:
        spaced = spaced.replace(f" {compound} ", f" {compound.replace(' ', '_')} ")
    return spaced.strip()


def topic_terms(question: str, *, exclude: Iterable[str] = ()) -> tuple[frozenset[str], frozenset[str]]:
    """(syllables, syllable pairs) that carry the question's topic."""

    # The company's name and ticker are removed as whole phrases: its
    # syllables ("sữa" in "Sữa Việt Nam") can still be the topic.
    spaced = f" {fold(question)} "
    names = sorted({fold(item) for item in exclude if fold(item)}, key=len, reverse=True)
    for phrase in (*names, *_GENERIC_PHRASES):
        spaced = spaced.replace(f" {phrase} ", " | ")
    tokens = [token for token in spaced.split() if not token.isdigit()]

    def keep(token: str) -> bool:
        return token != "|" and token not in _STOP and len(token) > 1

    words = {token for token in tokens if keep(token)}
    pairs = {f"{a} {b}" for a, b in zip(tokens, tokens[1:]) if keep(a) and keep(b)}
    return frozenset(words), frozenset(pairs)


def topic_score(terms: tuple[frozenset[str], frozenset[str]], text: str) -> int:
    words, pairs = terms
    if not words and not pairs:
        return 0
    folded = fold(text)
    tokens = set(folded.split())
    spaced = f" {folded} "
    return len(words & tokens) + 2 * sum(1 for pair in pairs if f" {pair} " in spaced)


def has_topic(terms: tuple[frozenset[str], frozenset[str]]) -> bool:
    return bool(terms[0] or terms[1])


def without_common_terms(terms: tuple[frozenset[str], frozenset[str]], texts: list[str],
                         *, share: float = 0.5) -> tuple[frozenset[str], frozenset[str]]:
    """Drop topic words most of ``texts`` contain: the company's brand or
    subject (every article of a ticker says "Vinamilk") tells none apart."""

    if len(texts) < 3:
        return terms
    token_sets = [set(fold(text).split()) for text in texts]
    common = {word for word in terms[0]
              if sum(1 for tokens in token_sets if word in tokens) / len(token_sets) >= share}
    words = frozenset(terms[0] - common)
    pairs = frozenset(pair for pair in terms[1] if not set(pair.split()) <= common)
    return words, pairs
