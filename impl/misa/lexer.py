"""Misa v0.1 lexer.

Produces tokens with Go-style implicit semicolon insertion and leading
comment trivia (for the formatter). String contents are kept raw;
interpolation is parsed later (parser.parse_string_parts).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional


KEYWORDS = {
    "app", "model", "type", "screen", "let", "var", "if", "else", "for",
    "in", "while", "return", "stream", "await", "on", "budget", "prompt",
    "with", "eval", "given", "expect", "runs", "provider", "models",
    "true", "false", "and", "or", "not", "navigate", "env", "nil",
    "fn", "import", "try", "catch", "throw",
    # Phase 5b: `match` expression for sum types.
    "match",
    # Phase 5 (DB): `table` declarations for typed SQL queries.
    "table",
}

# token kinds that trigger implicit semicolon insertion at newline
INSERT_AFTER = {
    "IDENT", "INT", "FLOAT", "STRING", "DURATION", "MONEY",
    ")", "]", "}", "true", "false", "nil", "return",
}

MULTI = ["==", "!=", "<=", ">=", "->", "=>"]  # Phase 5b: "=>" match arms


@dataclass
class Token:
    kind: str
    value: object
    line: int
    col: int
    leading: List[str] = field(default_factory=list)


class LexError(Exception):
    def __init__(self, message, line, col):
        super().__init__(message)
        self.line = line
        self.col = col


class Lexer:
    def __init__(self, src: str, filename: str = "<input>"):
        self.src = src
        self.filename = filename
        self.i = 0
        self.line = 1
        self.col = 1
        self.pending_comments: List[str] = []

    # -- cursor helpers -------------------------------------------------

    def peek(self, n: int = 0) -> str:
        j = self.i + n
        return self.src[j] if j < len(self.src) else ""

    def advance(self, n: int = 1) -> str:
        s = self.src[self.i:self.i + n]
        for ch in s:
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
        self.i += n
        return s

    def at_end(self) -> bool:
        return self.i >= len(self.src)

    # -- main loop ------------------------------------------------------

    def tokens(self) -> List[Token]:
        out: List[Token] = []
        prev_kind: Optional[str] = None
        while not self.at_end():
            ch = self.peek()
            if ch in " \t\r":
                self.advance()
                continue
            if ch == "\n":
                self.advance()
                if prev_kind in INSERT_AFTER:
                    out.append(self._tok("SEMI", ";"))
                    prev_kind = "SEMI"
                continue
            if ch == "/" and self.peek(1) == "/":
                self._line_comment()
                continue
            if ch == "/" and self.peek(1) == "*":
                self._block_comment()
                continue
            tok = self._next_token()
            out.append(tok)
            prev_kind = tok.kind
        out.append(self._tok("EOF", None))
        return out

    def _tok(self, kind: str, value: object) -> Token:
        t = Token(kind, value, self.line, self.col, self.pending_comments)
        self.pending_comments = []
        return t

    # -- comments --------------------------------------------------------

    def _line_comment(self):
        start_col = self.col
        self.advance(2)
        buf = ""
        while not self.at_end() and self.peek() != "\n":
            buf += self.advance()
        self.pending_comments.append(("//" + buf).strip())

    def _block_comment(self):
        self.advance(2)
        buf = ""
        while not self.at_end():
            if self.peek() == "*" and self.peek(1) == "/":
                self.advance(2)
                break
            buf += self.advance()
        else:
            raise LexError("unterminated block comment", self.line, self.col)
        self.pending_comments.append(("/*" + buf + "*/").strip())

    # -- tokens ----------------------------------------------------------

    def _next_token(self) -> Token:
        ch = self.peek()
        line, col = self.line, self.col

        if ch == '"':
            if self.peek(1) == '"' and self.peek(2) == '"':
                return self._triple_string()
            return self._string()
        if ch == "$" and self.peek(1).isdigit():
            return self._money()
        if ch.isdigit():
            return self._number()
        if ch.isalpha() or ch == "_":
            return self._ident()
        if ch == "$":
            raise LexError("'$' must be followed by a number (money literal)",
                           line, col)

        two = ch + self.peek(1)
        if two in MULTI:
            t = self._tok(two, two)
            self.advance(2)
            return t
        if ch in "+-*/%()[]{}.,:|=?<>!@":
            # Phase 5 (DB): `@` for `@primary` in table declarations.
            if ch == "!" :
                raise LexError("unexpected '!'; did you mean '!=' or 'not'?",
                               line, col)
            t = self._tok(ch, ch)
            self.advance()
            return t
        raise LexError(f"unexpected character {ch!r}", line, col)

    def _ident(self) -> Token:
        line, col = self.line, self.col
        buf = ""
        while self.peek().isalnum() or self.peek() == "_":
            buf += self.advance()
        kind = buf if buf in KEYWORDS else "IDENT"
        t = Token(kind, buf, line, col, self.pending_comments)
        self.pending_comments = []
        return t

    def _number(self) -> Token:
        line, col = self.line, self.col
        buf = ""
        while self.peek().isdigit():
            buf += self.advance()
        is_float = False
        if self.peek() == "." and self.peek(1).isdigit():
            is_float = True
            buf += self.advance()
            while self.peek().isdigit():
                buf += self.advance()
        # duration suffix?
        rest = self.src[self.i:self.i + 2]
        suffix = ""
        if rest.startswith("ms") and not self._ident_char_at(self.i + 2):
            suffix = "ms"
        elif rest.startswith("s") and not self._ident_char_at(self.i + 1):
            suffix = "s"
        if suffix:
            self.advance(len(suffix))
            val = float(buf)
            ms = int(val * 1000) if suffix == "s" else int(val)
            t = Token("DURATION", ms, line, col, self.pending_comments)
            self.pending_comments = []
            return t
        kind = "FLOAT" if is_float else "INT"
        val = float(buf) if is_float else int(buf)
        t = Token(kind, val, line, col, self.pending_comments)
        self.pending_comments = []
        return t

    def _ident_char_at(self, j: int) -> bool:
        c = self.src[j] if j < len(self.src) else ""
        return c.isalnum() or c == "_"

    def _money(self) -> Token:
        line, col = self.line, self.col
        self.advance()  # $
        buf = ""
        while self.peek().isdigit():
            buf += self.advance()
        if self.peek() == "." and self.peek(1).isdigit():
            buf += self.advance()
            while self.peek().isdigit():
                buf += self.advance()
        t = Token("MONEY", float(buf), line, col, self.pending_comments)
        self.pending_comments = []
        return t

    def _string(self) -> Token:
        line, col = self.line, self.col
        self.advance()  # opening "
        buf = ""
        while True:
            if self.at_end():
                raise LexError("unterminated string", line, col)
            ch = self.peek()
            if ch == '"':
                self.advance()
                break
            if ch == "\n":
                raise LexError("newline in single-line string; use \"\"\" \"\"\"",
                               self.line, self.col)
            if ch == "\\":
                self.advance()
                e = self.peek()
                mapping = {"n": "\n", "t": "\t", "r": "\r",
                           '"': '"', "\\": "\\"}
                if e in mapping:
                    buf += mapping[e]
                    self.advance()
                else:
                    raise LexError(f"unknown escape '\\{e}'", self.line,
                                   self.col)
                continue
            buf += self.advance()
        t = Token("STRING", buf, line, col, self.pending_comments)
        self.pending_comments = []
        return t

    def _triple_string(self) -> Token:
        line, col = self.line, self.col
        self.advance(3)
        buf = ""
        while True:
            if self.at_end():
                raise LexError('unterminated """ string', line, col)
            if self.peek() == '"' and self.peek(1) == '"' \
                    and self.peek(2) == '"':
                self.advance(3)
                break
            if self.peek() == "\\":
                self.advance()
                e = self.peek()
                mapping = {"n": "\n", "t": "\t", "r": "\r",
                           '"': '"', "\\": "\\"}
                if e in mapping:
                    buf += mapping[e]
                    self.advance()
                else:
                    buf += "\\" + self.advance()
                continue
            buf += self.advance()
        # strip one leading newline, common for block templates
        if buf.startswith("\n"):
            buf = buf[1:]
        t = Token("STRING", buf, line, col, self.pending_comments)
        self.pending_comments = []
        return t
