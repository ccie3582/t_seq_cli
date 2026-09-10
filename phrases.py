"""Phrase expression parsing for the sequencer CLI.

A phrase is defined at track level and later referenced by a pattern. At
definition time it is a pure template over generic sequence references, e.g.:

    p3 = inf * (13 * seq0 + 7 * seq3)
    p2 = 10 * seq0 + 8 * (5 * seq6 + 2 * seq2)

Meaning: weights repeat the referenced sequence that many times, '+' chains
segments, parentheses group a sub-combination, and 'inf' wraps the whole
phrase as a loop that repeats until stopped elsewhere (not at phrase level).

Grammar (canonical forms only, no pattern binding yet):

    expr    := 'inf*' wrap | sum
    wrap    := '(' sum ')' | 'seq' DIGIT
    sum     := term ( '+' term )*
    term    := count '*' unit | unit
    unit    := 'seq' DIGIT | '(' sum ')'
    count   := positive integer
"""

import re

MAX_SEQ_INDEX = 9  # seq0..seq9
MAX_PATTERN = 16   # p1..p16 label constraint


class PhraseError(ValueError):
    """Raised for invalid phrase expression syntax."""


_TOKEN_RE = re.compile(r"\s*((?:seq|s)\d+|\d+|inf|[()+*])")
_SEQ_TOKEN_RE = re.compile(r"(?:seq|s)(\d+)$")


def _tokenize(text):
    tokens = []
    i = 0
    while i < len(text):
        m = _TOKEN_RE.match(text, i)
        if not m:
            raise PhraseError(f"Unexpected character {text[i]!r} in phrase expression.")
        tokens.append(m.group(1))
        i = m.end()
    return tokens


def _seq_index(token):
    """'s3' / 'seq3' -> 3, or None when the token is not a sequence ref."""
    m = _SEQ_TOKEN_RE.fullmatch(token)
    return int(m.group(1)) if m else None


def _parse_seq(token):
    """'s3' (or legacy 'seq3') -> 3, validated against s0..s9."""
    n = _seq_index(token)
    if n is None:
        raise PhraseError(f"Not a sequence reference: {token}")
    if not 0 <= n <= MAX_SEQ_INDEX:
        raise PhraseError(f"Sequence {token} out of range (use s0-s{MAX_SEQ_INDEX}).")
    return n


class _Parser:
    """Recursive-descent parser over the token stream."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, expected=None):
        tok = self.peek()
        if tok is None:
            raise PhraseError("Unexpected end of phrase expression.")
        if expected is not None and tok != expected:
            raise PhraseError(f"Expected {expected!r} but found {tok!r}.")
        self.pos += 1
        return tok

    def parse(self):
        if self.peek() == "inf":
            # inf must wrap the whole expression: inf*(sum) or inf*seqN
            self.pos += 1
            self.take("*")
            tok = self.peek()
            if tok is None:
                raise PhraseError("Expected a sequence or '(' after 'inf*'.")
            if _seq_index(tok) is not None:
                self.pos += 1
                inner = ("seq", _parse_seq(tok))
            elif tok == "(":
                self.pos += 1
                inner = self._parse_sum()
                self.take(")")
            else:
                raise PhraseError(f"Unexpected token {tok!r}: expected a sequence or '(' after 'inf*'.")
            if self.peek() is not None:
                raise PhraseError("inf can only wrap the whole phrase expression.")
            return ("inf", inner)
        items = self._parse_sum()
        if self.peek() is not None:
            raise PhraseError(f"Unexpected trailing token {self.peek()!r}.")
        return items

    def _parse_sum(self):
        items = [self._parse_term()]
        while self.peek() == "+":
            self.pos += 1
            items.append(self._parse_term())
        return items

    def _parse_term(self):
        count = 1
        if self.peek() is not None and self.peek().isdigit():
            count = int(self.take())
            if count < 1:
                raise PhraseError(f"Invalid repeat count {count}: must be a positive integer.")
            self.take("*")
        tok = self.peek()
        if tok is None:
            raise PhraseError("Expected a sequence or group after the multiplier.")
        if _seq_index(tok) is not None:
            self.pos += 1
            return (count, ("seq", _parse_seq(tok)))
        if tok == "inf":
            raise PhraseError("'inf' is only allowed as the outermost multiplier.")
        if tok == "(":
            self.pos += 1
            items = self._parse_sum()
            self.take(")")
            return (count, ("group", items))
        raise PhraseError(f"Unexpected token {tok!r}: expected a sequence or '('.")


def _fmt_sum(items):
    return "+".join(_fmt_term(it) for it in items)


def _fmt_term(item):
    count, unit = item
    body = f"s{unit[1]}" if unit[0] == "seq" else f"({_fmt_sum(unit[1])})"
    return body if count == 1 else f"{count}*{body}"


def parse_phrase_tree(text):
    """Parse an expression body into a tree.

    Tree shape (matching the grammar):
      no 'inf'  -> list of terms; each term is (count, unit) with
                   unit ('seq', index) or ('group', terms-list)
      with 'inf'-> ('inf', inner) where inner is a ('seq', index) tuple or a
                   terms-list.
    Raises PhraseError for invalid input.
    """
    tokens = _tokenize(text)
    if not tokens:
        raise PhraseError("Empty phrase expression.")
    return _Parser(tokens).parse()


def parse_phrase_expr(text):
    """Parse an expression body (no 'pN=' part) into canonical text.

    Returns the canonical string, e.g. 'inf*(13*seq0+7*seq3)' or 'inf*seq2'.
    Raises PhraseError for invalid input.
    """
    tree = parse_phrase_tree(text)
    if tree[0] == "inf":
        inner = tree[1]
        if isinstance(inner, list):  # inf*(sum)
            return f"inf*({_fmt_sum(inner)})"
        return f"inf*s{inner[1]}"  # inf*sN
    return _fmt_sum(tree)
