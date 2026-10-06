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
RAWSTRING:= 'r' '"' [^"\n]* '"' | 'r' '"""' .*? '"""'
           // W-6: raw strings — no INTERP, no ESC; braces and
           // backslashes are literal. For JSON templates etc.
INTERP   := '{' expr '}'            // inside strings only; '{{' = literal brace
                                     // (M-1: there is NO '}}' escape — '}}'
                                     // is two literal chars, because only
                                     // '{' opens interpolation. To emit a
                                     // literal "{{...}}", write "{{" + "}" + "}".
DURATION := (INT|FLOAT) ('ms' | 's')
MONEY    := '$' (INT|FLOAT)
```

Keywords: `app` `model` `type` `screen` `let` `var` `if` `else` `for` `in`
`while` `return` `stream` `await` `on` `budget` `prompt` `with` `eval`
`given` `expect` `runs` `provider` `models` `true` `false` `and` `or` `not`
`navigate` `env` `nil` `v` (version prefix, only after `prompt`)
`fn` `import` `try` `catch` `throw`

Contextual keywords (ordinary IDENTs, special only in item position):
`interface` `impl`, plus `test`.

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
             | modelDecl | screenDecl | evalDecl | testDecl | fnDecl
             | importDecl | tableDecl | externDecl
             | interfaceDecl | implDecl | statement
             // Phase 8 (overtake): interfaces — `interface`/`impl` are
             // contextual keywords (ordinary IDENTs elsewhere).

appDecl     := "app" IDENT "{" appField* "}"
appField    := IDENT ":" literal ","?

providerDecl:= "provider" IDENT "{" providerField* "}"
providerField := ( "key" ":" "env" "(" STRING ")"
                 | "model" ":" "file" "(" STRING ")" ) ","?

modelsDecl  := "models" "{" modelsField* "}"
modelsField := IDENT ":" IDENT "(" STRING ")" ","?

typeDecl    := "type" IDENT typeParams? "{" (typeField | sumVariant)* "}"
typeParams  := "<" typeParam ("," typeParam)* ">"
typeParam   := IDENT (":" IDENT)?
              // Phase 8 (overtake): interface bounds, e.g. <T: Drawable>.
              // Bounds are only legal on fnDecl; a bound on a type or
              // extern declaration is a parse error.
typeField   := IDENT ":" typeExpr ","?
sumVariant  := IDENT "(" (IDENT ":" typeExpr ("," IDENT ":" typeExpr)*)? ")" ","?
              // Phase 5b: sum types, e.g. type Shape { Circle(radius: Float), None, }

tableDecl   := "table" IDENT "{" tableField* "}"
tableField  := IDENT ":" typeExpr ("@" IDENT)? ","?
              // Phase 5 (DB): e.g. table users { id: Int @primary, name: Text, }

externDecl  := "extern" STRING "{" (externFn | externStruct)* "}"
externFn    := "fn" IDENT "(" (params ","? "..."?)? ")" ("->" typeExpr)? ","?
              // Phase 7 (FFI): e.g. extern "c" { fn strlen(s: Text) -> Int }
              // Overtake Wave 2: trailing "..." = C varargs
externStruct:= "struct" IDENT "{" structField* "}" ","?
structField := IDENT ":" typeExpr ","?
              // Overtake Wave 2: e.g. struct Point { x: Float, y: Float }
              // (v1 fields: Int, Float, Bool, Ptr)
structLit   := IDENT "{" (IDENT ":" expr ","?)* "}"
              // Overtake Wave 2: e.g. Point { x: 1.0, y: 2.0 }

interfaceDecl := "interface" IDENT "{" ifaceSig* "}"
ifaceSig    := "fn" IDENT "(" ifaceParam ("," ifaceParam)* ")"
               ("->" typeExpr)? ("throws" IDENT ("," IDENT)*)?
ifaceParam  := "self" | IDENT ":" typeExpr
              // Phase 8 (overtake): `self` may be bare or typed
              // (`self: Circle`); other params need types.

implDecl    := "impl" IDENT "for" IDENT "{" implMethod* "}"
implMethod  := "fn" IDENT "(" ifaceParam ("," ifaceParam)* ")"
               ("->" typeExpr)? ("throws" IDENT ("," IDENT)*)?
               "{" statement* "}"
              // Phase 8 (overtake): e.g.
              //   interface Drawable { fn draw(self) -> Text }
              //   impl Drawable for Circle { fn draw(self) -> Text { … } }
              // v1: no default bodies, no blanket impls; the impl
              // target must be a plain record `type`.

typeExpr    := IDENT "?"? | "[" typeExpr "]" "?"?
             | "Map" "<" typeExpr "," typeExpr ">" "?"?
             | IDENT "<" typeExpr ("," typeExpr)* ">" "?"?
             // Phase 5c: generic instantiation, e.g. Box<Int>, Result<Text>
             | "(" (typeExpr ("," typeExpr)*)? ")" "->" typeExpr "?"?
             // Phase 5a: function types, e.g. (Int, Text) -> Bool

fnDecl      := "fn" IDENT typeParams? "(" params? ")" ("->" typeExpr)? throwsClause? "{" statement* "}"
             // Phase 5c: generic functions, e.g. fn first<T>(xs: [T]) -> T
             // Phase 8: checked throws, e.g. fn fetch(u: Text) -> Text throws E304
throwsClause:= "throws" IDENT ("," IDENT)*
             // contextual keyword; each IDENT is an E-code like E304
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

screenDecl  := "screen" IDENT "(" params? ")" "{" screenItem* "}"
screenItem  := component | screenIf | screenFor | letDecl
screenIf    := "if" expr "{" screenItem* "}" ("else" "{" screenItem* "}")?
screenFor   := "for" IDENT "in" expr "{" screenItem* "}"
letDecl     := ("let" | "var") IDENT (":" typeExpr)? "=" expr
component   := IDENT "(" compArgs? ")" ("{" component* "}")?
compArgs    := compArg ("," compArg)*
compArg     := IDENT ":" expr

evalDecl    := "eval" IDENT "{"
               "model" ":" IDENT ","
               "given" ":" recordLit ","
               "expect" ":" expr ","
               "runs" ":" INT ","? "}"

testDecl    := "test" IDENT "{" statement* "}"
              // contextual keyword: `test` is not reserved; only
              // `test <name> {` at the top level is a test block.

statement   := letStmt | varStmt | assignStmt | ifStmt | forStmt
             | whileStmt | returnStmt | tryStmt | throwStmt | onStmt
             | exprStmt ";"

tryStmt     := "try" "{" statement* "}" "catch" IDENT "{" statement* "}"
throwStmt   := "throw" expr
letStmt     := "let" IDENT (":" typeExpr)? "=" expr
varStmt     := "var" IDENT (":" typeExpr)? "=" expr
assignStmt  := (IDENT | fieldAccess | indexExpr) "=" expr
ifStmt      := "if" expr "{" statement* "}" ("else" ("if" expr "{" statement* "}")* ("{" statement* "}")?)?
             // W-5: 'else if' chains desugar to nested ifs at parse time
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
awaitExpr   := "await" unary ("timeout" unary)?
             // Phase 7 (concurrency): `await t`, `await t timeout 5000`.
             // `timeout` is contextual — only special directly after
             // an await operand.
spawnExpr   := "spawn" "{" statement* "}"
             // Phase 7 (concurrency): task expression, type Task<T>.
primary     := literal | IDENT | callExpr | fieldAccess | indexExpr
             | listLit | recordLit | mapLit | lambda | "(" expr ")"
             | "stream" IDENT "=" expr | matchExpr | sumCtor | spawnExpr
             // Phase 5b: match expressions and sum constructors
             // Phase 7: spawnExpr (concurrency)
matchExpr   := "match" expr "{" matchArm* "}"
matchArm    := (IDENT "." IDENT "(" (IDENT ("," IDENT)*)? ")" | IDENT | "_")
               "=>" (expr | "{" statement* "}") ","?
sumCtor     := IDENT "." IDENT "(" (expr ("," expr)*)? ")"
             // e.g. Shape.Circle(1.0)
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
lambda      := "|" (lambdaParam ("," lambdaParam)*)? "|" ("{" statement* "}" | expr)
lambdaParam := IDENT (":" typeExpr)?
// Phase 5a: typed params, e.g. |x: Int| x * 2
callExpr    := IDENT "(" callArgs? ")" | expr "(" callArgs? ")"
// Phase 5a: calling a call result, e.g. mk(9)(1)
literal     := INT | FLOAT | STRING | TRISTRING | RAWSTRING | DURATION | MONEY
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
`E2xx` (types), `E3xx` (runtime), `E4xx` (model/provider), `E5xx`
(budget, on-device inference). `misa check --json` emits
`[{code, message, line, col, hint}]`.

New in v0.2: `E110` duplicate top-level name across imports, `E111`
circular import, `E112` missing/unreadable import, `E211` function
return/argument typing, `E212` `throw` of a non-`Error`, `E303` I/O
failure, `E304` HTTP failure, `E305` missing map key.

A provider block may name a local model file instead of a key:
`provider edge { model: file("./tiny.onnx") }`. The extension selects
the runtime (`.onnx` → ONNX Runtime, `.mlpackage`/`.mlmodelc` →
CoreML, `.tflite` → TFLite); the `model` call syntax is unchanged.
On-device errors: `E501` tensor shape mismatch, `E502` unsupported
on-device signature, `E503` runtime unavailable on this platform,
`E504` on-device model rejected by `misa build` (interpreter-only in
M1). Full design: `spec/ONDEVICE.md`.
