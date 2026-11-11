"""Python mirror of the sentence-boundary and occurrence logic in extension/content.js.

Node is not installed, so the JS cannot run here; this file ports isSentenceEnd / expandSentence /
parseOccurrence / pickIndex line for line so the rule can be checked. Keep it in sync with content.js.
Run: python tests/test_sentence.py
"""
import re
import unittest

TERM = ".!?"
CLOSERS = "\"')]”’"
OPENERS = "\"'([“‘"
ABBREVIATIONS = {"fig", "figs", "eq", "eqs", "sec", "secs", "no", "nos", "vs", "cf",
                 "al", "dr", "mr", "mrs", "ms", "prof", "ref", "refs", "vol", "pp", "ch",
                 "approx", "resp", "eg", "ie"}


def normalize(s):
    """Same as buildIndex().norm.trim(): lowercase, whitespace collapsed to one space."""
    return re.sub(r"\s+", " ", s).strip().lower()


def is_term_at(norm, p):
    if p < 0:
        return False
    if norm[p] in TERM:
        return True
    return norm[p] in CLOSERS and p > 0 and norm[p - 1] in TERM


def is_sentence_end(norm, p):
    if not is_term_at(norm, p) or p + 1 >= len(norm) or norm[p + 1] != " ":
        return False
    tp = p if norm[p] in TERM else p - 1
    if norm[tp] != ".":
        return True
    j = tp
    while j > 0 and norm[j - 1] != " ":
        j -= 1
    w = norm[j:tp]
    while w and w[0] in OPENERS:
        w = w[1:]
    if not w:
        return True
    if len(w) == 1 and re.match(r"[a-z]", w):
        return False
    if re.match(r"^([a-z]\.)+[a-z]$", w):
        return False
    return w not in ABBREVIATIONS


def expand_sentence(norm, s, e, block_starts=frozenset()):
    a = s
    while a > 0:
        if a in block_starts:
            break
        if a >= 2 and norm[a - 1] == " " and is_sentence_end(norm, a - 2):
            break
        a -= 1
    b = e
    while b < len(norm) - 1:
        if is_sentence_end(norm, b):
            break
        if (b + 1) in block_starts:
            break
        b += 1
    while b > a and norm[b] == " ":
        b -= 1
    return a, b


def sentence_of(text, needle):
    norm = normalize(text)
    n = normalize(needle)
    i = norm.index(n)
    a, b = expand_sentence(norm, i, i + len(n) - 1)
    return norm[a:b + 1]


def parse_occurrence(v):
    if v is None or v == "":
        return 1
    if v == "last":
        return "last"
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, str) and re.fullmatch(r"\d+", v.strip()):
        n = int(v.strip())
    else:
        return None
    return n if n >= 1 else None


def pick_index(count, occ):
    if occ is None:
        return -1
    k = count - 1 if occ == "last" else occ - 1
    return k if 0 <= k < count else -1


def all_positions(norm, n):
    out, p = [], norm.find(n)
    while p >= 0:
        out.append(p)
        p = norm.find(n, p + len(n))
    return out


# (text, needle, expected sentence in normalized form)
CASES = [
    ("Models rely on RNNs. They work! Next one.", "rely", "models rely on rnns."),
    ("Models rely on RNNs. They work! Next one.", "work", "they work!"),
    ("Models rely on RNNs. They work! Next one.", "next", "next one."),
    ("Intro. See Fig. 3 for the setup. Then more.", "setup", "see fig. 3 for the setup."),
    ("Intro. See Fig. 3 for the setup. Then more.", "see", "see fig. 3 for the setup."),
    ("Many methods, e.g. SCvx and GuSTO, work. Next.", "scvx", "many methods, e.g. scvx and gusto, work."),
    ("That is, i.e. the cost, is convex. Next.", "cost", "that is, i.e. the cost, is convex."),
    ("As shown by Szmuk et al. the method converges. Done.", "converges", "as shown by szmuk et al. the method converges."),
    ("Szmuk et al. showed this. Done.", "showed", "szmuk et al. showed this."),
    ("Substituting into Eq. 5 gives the bound. Done.", "bound", "substituting into eq. 5 gives the bound."),
    ("This is covered in Sec. 4 of the paper. Done.", "paper", "this is covered in sec. 4 of the paper."),
    ("See No. 7 in the list. Done.", "list", "see no. 7 in the list."),
    ("Ours is faster vs. the baseline. Done.", "faster", "ours is faster vs. the baseline."),
    ("Compare cf. the earlier result. Done.", "earlier", "compare cf. the earlier result."),
    ("The value is 3.5 percent here. Next.", "percent", "the value is 3.5 percent here."),
    ("The value is 3.5 percent here. Next.", "next", "next."),
    ("Tutorial by D. Malyuta and B. Acikmese is good. Next.", "malyuta", "tutorial by d. malyuta and b. acikmese is good."),
    ("Tutorial by D. Malyuta and B. Acikmese is good. Next.", "good", "tutorial by d. malyuta and b. acikmese is good."),
    ('He said "stop." Then he left. Next.', "left", "then he left."),
    ('He said "stop." Then he left. Next.', "stop", 'he said "stop."'),
    ("It holds (see the proof.) Then more follows.", "more", "then more follows."),
    ("Is it convex? Yes, it is. Done.", "yes", "yes, it is."),
    ("We use the U.S. data set here. Next.", "data", "we use the u.s. data set here."),
    ("The test ended in 5 min. The next began.", "began", "the next began."),
    ("Pure text with no terminator", "terminator", "pure text with no terminator"),
]


class T(unittest.TestCase):
    def test_cases(self):
        for text, needle, want in CASES:
            with self.subTest(text=text, needle=needle):
                self.assertEqual(sentence_of(text, needle), want)

    def test_enough_cases(self):
        self.assertGreaterEqual(len(CASES), 15)

    def test_block_start_stops(self):
        norm = normalize("Heading Body text here")
        # block boundary before 'body' (index 8)
        a, b = expand_sentence(norm, 10, 12, frozenset({8}))
        self.assertEqual(norm[a:b + 1], "body text here")

    def test_occurrence_parse(self):
        self.assertEqual(parse_occurrence(None), 1)
        self.assertEqual(parse_occurrence(""), 1)
        self.assertEqual(parse_occurrence(3), 3)
        self.assertEqual(parse_occurrence("2"), 2)
        self.assertEqual(parse_occurrence("last"), "last")
        for bad in (0, -1, 1.5, "x", "0", True, [1]):
            self.assertIsNone(parse_occurrence(bad), bad)

    def test_occurrence_pick(self):
        norm = normalize("the cat and the dog and the end")
        pos = all_positions(norm, "the")
        self.assertEqual(len(pos), 3)
        self.assertEqual(pick_index(3, 1), 0)
        self.assertEqual(pick_index(3, 3), 2)
        self.assertEqual(pick_index(3, "last"), 2)
        self.assertEqual(pick_index(3, 4), -1)
        self.assertEqual(pick_index(3, None), -1)
        self.assertEqual(pos[pick_index(3, 2)], norm.index("the dog") )

    def test_occurrence_then_expand(self):
        text = "Alpha is one. Beta is two. Alpha is three."
        norm = normalize(text)
        n = "alpha"
        pos = all_positions(norm, n)
        k = pick_index(len(pos), "last")
        a, b = expand_sentence(norm, pos[k], pos[k] + len(n) - 1)
        self.assertEqual(norm[a:b + 1], "alpha is three.")


if __name__ == "__main__":
    unittest.main()
