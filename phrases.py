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
    wrap    := '(' sum ')' | 'seq' DIGIT | 'o' DIGIT
    sum     := term ( '+' term )*
    term    := count '*' unit | unit
    unit    := 's' DIGIT | 'o' DIGIT | '(' sum ')'
    count   := positive integer

Note patterns and CC patterns reference **sequences** (``s0``); loop patterns
reference **order lists** (``o0``). The same grammar covers both, and the
reference kind travels with the unit so the caller can resolve it.
"""

import re

MAX_SEQ_INDEX = 9    # sequences  s0..s9
MAX_ORDER_INDEX = 9  # order lists o0..o9
MAX_PATTERN = 16     # p1..p16 label constraint


class PhraseError(ValueError):
    """Raised for invalid phrase expression syntax."""


_TOKEN_RE = re.compile(r"\s*((?:seq|s|o)\d+|\d+|inf|[()+*])")
_UNIT_TOKEN_RE = re.compile(r"(?i)(seq|s|o)(\d+)$")


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


def _unit_ref(token):
    """'s3'/'seq3' -> ('seq', 3); 'o2' -> ('order', 2); None otherwise."""
    m = _UNIT_TOKEN_RE.fullmatch(token or "")
    if not m:
        return None
    kind = "order" if m.group(1).lower() == "o" else "seq"
    return (kind, int(m.group(2)))


def _seq_index(token):
    """3 for 's3'/'seq3', or None when the token is not a sequence ref."""
    ref = _unit_ref(token)
    return ref[1] if ref and ref[0] == "seq" else None


def _parse_unit(token):
    """Validate a unit reference -> ('seq', n) or ('order', n)."""
    ref = _unit_ref(token)
    if ref is None:
        raise PhraseError(f"Not a sequence or order reference: {token}")
    kind, n = ref
    if kind == "seq":
        if not 0 <= n <= MAX_SEQ_INDEX:
            raise PhraseError(
                f"Sequence {token} out of range (use s0-s{MAX_SEQ_INDEX}).")
    elif not 0 <= n <= MAX_ORDER_INDEX:
        raise PhraseError(
            f"Order list {token} out of range (use o0-o{MAX_ORDER_INDEX}).")
    return (kind, n)


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
                raise PhraseError("Expected a sequence, order or '(' after 'inf*'.")
            if _unit_ref(tok) is not None:
                self.pos += 1
                inner = _parse_unit(tok)
            elif tok == "(":
                self.pos += 1
                inner = self._parse_sum()
                self.take(")")
            else:
                raise PhraseError(f"Unexpected token {tok!r}: expected a "
                                  f"sequence, order or '(' after 'inf*'.")
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
            raise PhraseError("Expected a sequence, order or group after the multiplier.")
        if _unit_ref(tok) is not None:
            self.pos += 1
            return (count, _parse_unit(tok))
        if tok == "inf":
            raise PhraseError("'inf' is only allowed as the outermost multiplier.")
        if tok == "(":
            self.pos += 1
            items = self._parse_sum()
            self.take(")")
            return (count, ("group", items))
        raise PhraseError(f"Unexpected token {tok!r}: expected a sequence, "
                          f"order or '('.")


def _fmt_sum(items):
    return "+".join(_fmt_term(it) for it in items)


def _fmt_term(item):
    count, unit = item
    if unit[0] == "seq":
        body = f"s{unit[1]}"
    elif unit[0] == "order":
        body = f"o{unit[1]}"
    else:
        body = f"({_fmt_sum(unit[1])})"
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
        return f"inf*{_fmt_term((1, inner))}"  # inf*sN / inf*oN
    return _fmt_sum(tree)


def unit_refs(text):
    """All (kind, index) references used by a phrase ('seq' or 'order')."""
    out = []

    def walk(items):
        for _count, unit in items:
            if unit[0] == "group":
                walk(unit[1])
            else:
                out.append(unit)

    tree = parse_phrase_tree(text)
    if tree[0] == "inf":
        inner = tree[1]
        walk(inner) if isinstance(inner, list) else out.append(inner)
    else:
        walk(tree)
    return out
