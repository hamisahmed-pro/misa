# Misa Language Specification — v0.2

Status: **v0.2** (2026-10-05). Normative for the v0.2 interpreter.
Supersedes v0.1 (2026-10-04). New in v0.2: `fn` declarations, `import`
modules, `try`/`catch`/`throw`, `Map` type, `fs`/`http` stdlib.

 companion docs: `SEMANTICS.md`, `STDLIB.md`.

## 1. Design rules

1. One spelling per construct. No aliases.
2. Braces delimit blocks. Whitespace is never significant.
3. Statements end at newlines (Go-style implicit semicolons, §3).
4. `misa fmt` output is the canonical form (§8).
5. Every error has a code and is machine-readable (§9).

## 2. Lexical grammar

```
IDENT    := [A-Za-z_][A-Za-z0-9_]*
INT      := [0-9]+
FLOAT    := [0-9]+.[0-9]+
STRING   := '"' (ESC | INTERP | [^"\\])* '"'
TRISTRING:= '"""' (ESC | INTERP | .)*? '"""'
INTERP   := '{' expr '}'            // inside strings only; '{{' = literal brace
DURATION := (INT|FLOAT) ('ms' | 's')
MONEY    := '$' (INT|FLOAT)
```

Keywords: `app` `model` `type` `screen` `let` `var` `if` `else` `for` `in`
`while` `return` `stream` `await` `on` `budget` `prompt` `with` `eval`
`given` `expect` `runs` `provider` `models` `true` `false` `and` `or` `not`
`navigate` `env` `nil` `v` (version prefix, only after `prompt`)
`fn` `import` `try` `catch` `throw`

Operators: `+ - * / %` `== != < <= > >=` `=` `|` `.` `,` `:` `(` `)` `[` `]`
`{` `}` `?`

Comments: `//` to end of line; `/* ... */` (non-nesting).

## 3. Semicolons

A semicolon is implicitly inserted at a newline following any of:
an identifier, literal, `)`, `]`, `}`, `return`, or a type name.
An explicit `;` is also legal but the formatter never emits one.

## 4. Grammar (EBNF)

```
program     := item*
item        := appDecl | providerDecl | modelsDecl | typeDecl
             | modelDecl | screenDecl | evalDecl | fnDecl | importDecl
             | statement

appDecl     := "app" IDENT "{" appField* "}"
appField    := IDENT ":" literal ","?

providerDecl:= "provider" IDENT "{" providerField* "}"
providerField := "key" ":" "env" "(" STRING ")" ","?

modelsDecl  := "models" "{" modelsField* "}"
modelsField := IDENT ":" IDENT "(" STRING ")" ","?

typeDecl    := "type" IDENT "{" typeField* "}"
typeField   := IDENT ":" typeExpr ","?

typeExpr    := IDENT "?"? | "[" typeExpr "]" "?"?
             | "Map" "<" typeExpr "," typeExpr ">" "?"?

fnDecl      := "fn" IDENT "(" params? ")" ("->" typeExpr)? "{" statement* "}"
importDecl  := "import" STRING

modelDecl   := "model" IDENT "(" params ")" "->" typeExpr
               "with" IDENT "." IDENT
               budgetClause?
               promptClause
params      := (param ("," param)*)?
param       := IDENT ":" typeExpr
budgetClause:= "budget" "{" budgetField* "}"
budgetField := ("tokens" ":" INT
              | "latency" ":" DURATION
              | "cost" ":" MONEY) ","?
promptClause:= "prompt" "v" INT TRISTRING

screenDecl  := "screen" IDENT "(" params? ")" "{" component* "}"
component   := IDENT "(" compArgs? ")" ("{" component* "}")?
compArgs    := compArg ("," compArg)*
compArg     := IDENT ":" expr

evalDecl    := "eval" IDENT "{"
               "model" ":" IDENT ","
               "given" ":" recordLit ","
               "expect" ":" expr ","
               "runs" ":" INT ","? "}"

statement   := letStmt | varStmt | assignStmt | ifStmt | forStmt
             | whileStmt | returnStmt | tryStmt | throwStmt | onStmt
             | exprStmt ";"

tryStmt     := "try" "{" statement* "}" "catch" IDENT "{" statement* "}"
throwStmt   := "throw" expr
letStmt     := "let" IDENT (":" typeExpr)? "=" expr
varStmt     := "var" IDENT (":" typeExpr)? "=" expr
assignStmt  := (IDENT | fieldAccess | indexExpr) "=" expr
ifStmt      := "if" expr "{" statement* "}" ("else" "{" statement* "}")?
forStmt     := "for" IDENT "in" expr "{" statement* "}"
whileStmt   := "while" expr "{" statement* "}"
returnStmt  := "return" expr?
onStmt      := "on" expr "." IDENT "{" lambda "}"
exprStmt    := expr

expr        := orExpr
orExpr      := andExpr ("or" andExpr)*
andExpr     := eqExpr ("and" eqExpr)*
eqExpr      := cmpExpr (("==" | "!=") cmpExpr)*
cmpExpr     := addExpr (("<" | "<=" | ">" | ">=") addExpr)*
addExpr     := mulExpr (("+" | "-") mulExpr)*
mulExpr     := unary (("*" | "/" | "%") unary)*
unary       := ("-" | "not") unary | awaitExpr | primary
awaitExpr   := "await" unary
primary     := literal | IDENT | callExpr | fieldAccess | indexExpr
             | listLit | recordLit | mapLit | lambda | "(" expr ")"
             | "stream" IDENT "=" expr
callExpr    := IDENT "(" callArgs? ")"
callArgs    := expr ("," expr)*
fieldAccess := primary "." IDENT
indexExpr   := primary "[" expr "]"
listLit     := "[" (expr ("," expr)*)? "]"
recordLit   := "{" (IDENT ":" expr ("," IDENT ":" expr)*)? "}"
mapLit      := "{" (STRING ":" expr ("," STRING ":" expr)*)? "}"
            // map vs record is decided by the first key token:
            // STRING key -> map, IDENT key -> record. `{}` is an empty
            // record; an empty map needs an annotation, e.g.
            // let m: Map<Text,Int> = {}
lambda      := "|" (IDENT ("," IDENT)*)? "|" ("{" statement* "}" | expr)
literal     := INT | FLOAT | STRING | TRISTRING | DURATION | MONEY
             | "true" | "false" | "nil"
```

Notes:

- `stream s = f(x)` is a primary-level declaration-expression: it declares
  an immutable binding `s` of type `TokenStream` and starts the call.
- `on s.token { |t| ... }` registers a per-token handler on a stream.
- `navigate(Home())` navigates to a screen (a screen call expression).
- Media handles are constructed by calling the type name:
  `Photo("img.jpg")`, `Audio("a.mp3")`, `Video("v.mp4")`,
  `Document("d.pdf")`.

## 5. Top-level rules

- At most one `app` block per program, counting imported files.
- `provider`, `models`, `type`, `model`, `screen`, `eval`, `fn` may appear
  in any order but must precede use at check time (forward references
  allowed, including recursion).
- `import "file.misa"`: the path is resolved relative to the importing
  file and must end in `.misa`. The imported file's top-level `fn`,
  `type`, `model`, `screen`, `eval`, `provider`, and `models`
  declarations become visible to the importer. A name defined in more
  than one file is error `E110`; a missing/unreadable import is `E112`;
  a circular import chain is `E111`.
- Execution: imported files' top-level statements run first, in import
  order (depth-first), then the main file's statements top to bottom.
- Executable statements run top to bottom after declarations are registered.

## 6. `model` semantics (summary)

Full semantics in `SEMANTICS.md`. In short: calling a `model` is a typed
remote call. The prompt template interpolates `{param}` with the call
arguments. The provider must return JSON; it is validated against the
declared return type, with up to 2 automatic repair retries. Declared
budgets are enforced; violations raise `BudgetExceeded`.

## 7. `screen` semantics (summary)

A screen is a pure component tree. Components are the closed set in
`STDLIB.md`. `id: name` as a component argument binds the component to
`name` in the screen's scope (e.g. `notes.text`). v0.1 `misa run` prints
the tree; native rendering is Phase 4.

## 8. Canonical format (`misa fmt`)

- 4-space indentation. K&R braces (`{` on the same line).
- One space around binary operators, after `,` and `:`, none inside
  parens/brackets at the edges.
- Exactly one blank line between top-level items. No trailing whitespace.
- File ends with a single newline.
- `//` comments are preserved, attached to the following item/statement.
- String contents are never reformatted.

## 9. Errors

Every diagnostic has a code: `E001` (syntax) … `E1xx` (name resolution),
`E2xx` (types), `E3xx` (runtime), `E4xx` (model/provider), `E5xx` (budget).
`misa check --json` emits `[{code, message, line, col, hint}]`.

New in v0.2: `E110` duplicate top-level name across imports, `E111`
circular import, `E112` missing/unreadable import, `E211` function
return/argument typing, `E212` `throw` of a non-`Error`, `E303` I/O
failure, `E304` HTTP failure, `E305` missing map key.
