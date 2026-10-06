"""Misa's portable regex engine (Wave 3 stdlib overtake).

A small, self-contained backtracking engine implementing a *portable*
subset of regex syntax. The SAME grammar and matching algorithm are
implemented twice — here in Python (interpreter) and in C
(`impl/misa/aot/rt/misa_rt.c`, AOT) — so both backends agree exactly.
No divergence to document: the check-time validator below accepts
exactly what both engines implement.

Supported syntax (v1):
    literal chars, `.` (any char except newline), `^` `$` anchors
    (start/end of text only — no multiline mode)
    `*` `+` `?` `{m}` `{m,}` `{m,n}` — greedy, with backtracking
    `(...)` groups (capturing), `|` alternation (leftmost wins)
    `[...]` character classes, ranges (`[a-z]`), negation (`[^...]`)
    escapes: `\\` `\\.` `\\*` `\\+` `\\?` `\\(` `\\)` `\\[` `\\]` `\\{` `\\}`
             `\\|` `\\^` `\\$` `\\n` `\\t` (anything else is an error)

NOT supported (rejected at check time with guidance):
    `\\d` `\\w` `\\s` `\\b` (use `[0-9]`, `[A-Za-z0-9_]`, `[ \\t\\n]`),
    `(?...)` groups, backreferences, lazy quantifiers (`*?`),
    lookahead/lookbehind.

Matching semantics: leftmost match; alternation prefers the left
branch; quantifiers are greedy with backtracking. `.` and `[^...]`
operate on Unicode code points (not bytes/UTF-16 units) on both
backends.
"""

# Node kinds (tuples):
#   ("lit", ch)            literal char (1-char str)
#   ("dot",)               any char except "\n"
#   ("class", neg, ranges) ranges = [(lo, hi), ...] code points
#   ("seq", [nodes])
#   ("alt", left, right)
#   ("quant", child, min, max)   max=None means unbounded
#   ("group", idx, child)  idx is 1-based
#   ("start",) ("end",)    anchors


class RegexError(Exception):
    pass


_ESCAPES = {
    "n": "\n", "t": "\t",
    "\\": "\\", ".": ".", "*": "*", "+": "+", "?": "?",
    "(": "(", ")": ")", "[": "[", "]": "]", "{": "}", "}": "}",
    "|": "|", "^": "^", "$": "$",
}
# Suggest a portable replacement for common unsupported escapes.
_ESCAPE_HINTS = {
    "d": "[0-9]", "w": "[A-Za-z0-9_]", "s": "[ \\t\\n\\r]",
    "b": None, "D": None, "W": None, "S": None,
}


class _Parser:
    def __init__(self, pat):
        self.pat = pat
        self.n = len(pat)
        self.i = 0
        self.ngroups = 0

    def error(self, msg):
        raise RegexError(f"{msg} (at index {self.i})")

    def peek(self):
        return self.pat[self.i] if self.i < self.n else ""

    def parse(self):
        node = self.parse_alt()
        if self.i != self.n:
            self.error(f"unexpected '{self.peek()}'")
        return node, self.ngroups

    def parse_alt(self):
        left = self.parse_seq()
        if self.peek() == "|":
            self.i += 1
            right = self.parse_alt()
            return ("alt", left, right)
        return left

    def parse_seq(self):
        nodes = []
        while self.i < self.n and self.peek() not in "|)":
            nodes.append(self.parse_quantified())
        if len(nodes) == 1:
            return nodes[0]
        return ("seq", nodes)

    def parse_quantified(self):
        atom = self.parse_atom()
        c = self.peek()
        if c != "" and c in "*+?":
            self.i += 1
            lo, hi = {"*": (0, None), "+": (1, None),
                      "?": (0, 1)}[c]
            return ("quant", atom, lo, hi)
        if c == "{":
            return self.parse_brace(atom)
        return atom

    def parse_brace(self, atom):
        # Strict: `{` must open a valid quantifier or it is an error
        # (write `\\{` for a literal brace).
        j = self.i + 1
        k = j
        while k < self.n and self.pat[k].isdigit():
            k += 1
        if k == j:
            self.error("expected a number after '{' "
                       "(write '\\{' for a literal brace)")
        lo = int(self.pat[j:k])
        hi = lo
        if k < self.n and self.pat[k] == ",":
            k += 1
            j2 = k
            while k < self.n and self.pat[k].isdigit():
                k += 1
            hi = int(self.pat[j2:k]) if k > j2 else None
        if k >= self.n or self.pat[k] != "}":
            self.error("expected '}' to close '{m,n}'")
        if hi is not None and hi < lo:
            self.error(f"{{{lo},{hi}}}: max < min")
        self.i = k + 1
        return ("quant", atom, lo, hi)

    def parse_atom(self):
        c = self.peek()
        if c == "":
            self.error("unexpected end of pattern")
        if c in "*+?":
            self.error(f"quantifier '{c}' has nothing to repeat")
        if c == "{":
            self.error("'{' has nothing to repeat "
                       "(write '\\{' for a literal brace)")
        if c == "^":
            self.i += 1
            return ("start",)
        if c == "$":
            self.i += 1
            return ("end",)
        if c == ".":
            self.i += 1
            return ("dot",)
        if c == "(":
            self.i += 1
            if self.peek() == "?":
                self.error("'(?...)' groups are not supported in v1 "
                           "(use plain '(...)' groups)")
            self.ngroups += 1
            idx = self.ngroups
            node = self.parse_alt()
            if self.peek() != ")":
                self.error("unclosed '('")
            self.i += 1
            return ("group", idx, node)
        if c == "[":
            return self.parse_class()
        if c == "\\":
            return self.parse_escape()
        if c == "|":
            self.error("empty branch in alternation")
        if c == ")":
            self.error("unmatched ')'")
        self.i += 1
        return ("lit", c)

    def parse_escape(self):
        # self.pat[self.i] == "\\"
        self.i += 1
        if self.i >= self.n:
            self.error("pattern ends with '\\'")
        c = self.pat[self.i]
        self.i += 1
        if c in _ESCAPES:
            return ("lit", _ESCAPES[c])
        if c in _ESCAPE_HINTS:
            hint = _ESCAPE_HINTS[c]
            msg = f"unsupported escape '\\{c}'"
            if hint:
                msg += f" — use {hint} instead"
            self.error(msg)
        self.error(f"unsupported escape '\\{c}'")

    def parse_class(self):
        # self.pat[self.i] == "["
        self.i += 1
        neg = False
        if self.peek() == "^":
            neg = True
            self.i += 1
        ranges = []
        first = True
        while True:
            if self.i >= self.n:
                self.error("unclosed '['")
            c = self.pat[self.i]
            if c == "]" and not first:
                self.i += 1
                break
            first = False
            lo = self.class_char()
            if self.peek() == "-" and self.i + 1 < self.n \
                    and self.pat[self.i + 1] != "]":
                self.i += 1  # consume "-"
                hi = self.class_char()
                if ord(hi) < ord(lo):
                    self.error(f"invalid range '{lo}-{hi}'")
                ranges.append((ord(lo), ord(hi)))
            else:
                ranges.append((ord(lo), ord(lo)))
        if not ranges:
            self.error("'[]' matches nothing — write '[\\]]' for a "
                       "literal ']'")
        return ("class", neg, ranges)

    def class_char(self):
        c = self.pat[self.i]
        if c == "\\":
            self.i += 1
            if self.i >= self.n:
                self.error("pattern ends with '\\'")
            e = self.pat[self.i]
            self.i += 1
            if e in _ESCAPES:
                return _ESCAPES[e]
            self.error(f"unsupported escape '\\{e}' in class")
        self.i += 1
        return c


def validate(pattern):
    """Check a pattern against the v1 grammar.

    Returns None when valid, else a human-readable error message.
    """
    try:
        _Parser(pattern).parse()
        return None
    except RegexError as ex:
        return str(ex)


def compile(pattern):  # noqa: A001 (shadows builtin, intentional)
    """Parse a pattern; raises RegexError on invalid patterns."""
    node, ngroups = _Parser(pattern).parse()
    return (node, ngroups)


# -- matching ---------------------------------------------------------
# Continuation-passing backtracker (PCRE semantics): `_match(node, ...
# k)` tries every way `node` can match at `pos`, calling `k(end)` for
# each; `k` returns >= 0 on success or -1 to keep backtracking.

def _match(node, text, pos, caps, k):
    kind = node[0]
    n = len(text)
    if kind == "lit":
        if pos < n and text[pos] == node[1]:
            return k(pos + 1)
        return -1
    if kind == "dot":
        if pos < n and text[pos] != "\n":
            return k(pos + 1)
        return -1
    if kind == "class":
        _, neg, ranges = node
        if pos < n:
            cp = ord(text[pos])
            hit = any(lo <= cp <= hi for lo, hi in ranges)
            if hit != neg:
                return k(pos + 1)
        return -1
    if kind == "start":
        return k(pos) if pos == 0 else -1
    if kind == "end":
        return k(pos) if pos == n else -1
    if kind == "seq":
        return _match_seq(node[1], 0, text, pos, caps, k)
    if kind == "group":
        _, idx, child = node
        old = caps[idx][:]
        caps[idx][0] = pos

        def done(q):
            caps[idx][1] = q
            r = k(q)
            if r < 0:
                caps[idx][:] = old
            return r

        r = _match(child, text, pos, caps, done)
        if r < 0:
            caps[idx] = [-1, -1]
        return r
    if kind == "alt":
        _, left, right = node
        saved = [c[:] for c in caps]
        r = _match(left, text, pos, caps, k)
        if r >= 0:
            return r
        caps[:] = saved
        return _match(right, text, pos, caps, k)
    if kind == "quant":
        return _match_quant(node, text, pos, caps, k)
    raise RegexError(f"internal: bad node {kind}")


def _match_seq(nodes, idx, text, pos, caps, k):
    if idx == len(nodes):
        return k(pos)
    node = nodes[idx]

    def rest(q):
        return _match_seq(nodes, idx + 1, text, q, caps, k)

    return _match(node, text, pos, caps, rest)


def _match_quant(qnode, text, pos, caps, k):
    _, child, lo, hi = qnode

    def rec(p, count):
        # Greedy: try to extend the run first, then stop.
        if hi is None or count < hi:
            saved = [c[:] for c in caps]

            def after_child(q):
                if q == p:
                    return -1  # empty match: no progress, stop extending
                return rec(q, count + 1)

            r = _match(child, text, p, caps, after_child)
            if r >= 0:
                return r
            caps[:] = saved
        if count >= lo:
            return k(p)
        return -1

    return rec(pos, 0)


class Regex:
    """A compiled pattern. `Regex(pattern)` validates + compiles."""

    def __init__(self, pattern):
        err = validate(pattern)
        if err is not None:
            raise RegexError(err)
        self.pattern = pattern
        self.node, self.ngroups = compile(pattern)

    def _search(self, text, start=0):
        n = len(text)
        for pos in range(start, n + 1):
            caps = [[-1, -1] for _ in range(self.ngroups + 1)]
            q = _match(self.node, text, pos, caps, lambda e: e)
            if q >= 0:
                return pos, q, caps
        return None

    def matches(self, text):
        """True if the whole text matches."""
        caps = [[-1, -1] for _ in range(self.ngroups + 1)]
        # An implicit end-anchor INSIDE the match so backtracking
        # (alternations, quantifiers) respects the full-match.
        q = _match_seq([self.node, ("end",)], 0, text, 0, caps,
                       lambda e: e)
        return q >= 0

    def find(self, text):
        """The first match, or None."""
        r = self._search(text)
        if r is None:
            return None
        s, e, _caps = r
        return text[s:e]

    def find_all(self, text):
        """All non-overlapping matches. Empty matches advance 1 char."""
        out = []
        n = len(text)
        pos = 0
        while pos <= n:
            r = self._search(text, pos)
            if r is None:
                break
            s, e, _caps = r
            out.append(text[s:e])
            pos = e if e > s else s + 1
        return out

    def captures(self, text):
        """Group texts of the first match, or None if no match.

        Groups that did not participate are "" (v1 simplification).
        """
        r = self._search(text)
        if r is None:
            return None
        s, e, caps = r
        groups = []
        for i in range(1, self.ngroups + 1):
            a, b = caps[i]
            groups.append(text[a:b] if a >= 0 else "")
        return groups

    def replace(self, text, repl):
        """Replace all non-overlapping matches. `$1`..`$9` in `repl`
        name groups (unmatched groups expand to ""); `$$` is `$`."""
        out = []
        n = len(text)
        pos = 0
        last = 0
        while pos <= n:
            r = self._search(text, pos)
            if r is None:
                break
            s, e, caps = r
            out.append(text[last:s])
            out.append(self._expand(repl, text, caps))
            last = e
            pos = e if e > s else s + 1
        out.append(text[last:])
        return "".join(out)

    def _expand(self, repl, text, caps):
        out = []
        i = 0
        while i < len(repl):
            c = repl[i]
            if c == "$" and i + 1 < len(repl):
                d = repl[i + 1]
                if d == "$":
                    out.append("$")
                    i += 2
                    continue
                if d.isdigit() and d != "0":
                    g = int(d)
                    if g <= self.ngroups:
                        a, b = caps[g]
                        out.append(text[a:b] if a >= 0 else "")
                    i += 2
                    continue
            out.append(c)
            i += 1
        return "".join(out)
