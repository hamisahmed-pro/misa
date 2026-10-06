# Misa self-hosted AST — tagged-record format (Phase 6a)

Every AST node is a Misa record with a `node: Text` tag field plus
`line: Int` and `col: Int`. This mirrors `impl/misa/ast.py`.
Nested nodes are nested records; lists are Misa lists.

## Top-level

- `{node:"Program", decls:[...], line, col}`
- `{node:"AppDecl", name:Text, line, col}`
- `{node:"ProviderDecl", name:Text, base_url:Text, api_key_env:Text, line, col}`
- `{node:"ModelsDecl", provider:Text, items:[{alias:Text, model:Text}], line, col}`
- `{node:"TypeDecl", name:Text, fields:[{name:Text, type:Text}], line, col}`
  - `type` is the type expression rendered as text, e.g. "Int", "[Text]", "Text?"
- `{node:"Budget", max_tokens:Int, max_cost:Float, timeout_ms:Int, line, col}`
- `{node:"ModelDecl", name:Text, params:[{name:Text,type:Text}], ret:Text,
   budget:Budget|nil, prompt:Text, line, col}`
- `{node:"Component", kind:Text, props:[{name:Text, value:NODE}], children:[NODE],
   line, col}` — screen-DSL element (`kind` = "Column", "Text", ...)
- `{node:"ScreenDecl", name:Text, params:[{name:Text,type:Text}],
   body:[STMT], line, col}`
- `{node:"ScreenIf", cond:NODE, then_body:[STMT], else_body:[STMT], line, col}`
- `{node:"ScreenFor", var:Text, iter:NODE, body:[STMT], line, col}`
- `{node:"EvalDecl", name:Text, cases:[{given:NODE, expect:NODE}], line, col}`
- `{node:"FnDecl", name:Text, params:[{name:Text,type:Text}], ret:Text,
   body:[STMT], line, col}`
- `{node:"ImportDecl", path:Text, line, col}`
- `{node:"StreamDecl", name:Text, params:[{name:Text,type:Text}],
   body:[STMT], line, col}`

## Statements

- `{node:"LetStmt", name:Text, type:Text|nil, value:NODE, line, col}`
- `{node:"AssignStmt", target:NODE, value:NODE, line, col}` — target is Ident|Field|Index
- `{node:"IfStmt", cond:NODE, then_body:[STMT], else_body:[STMT], line, col}`
- `{node:"ForStmt", var:Text, iter:NODE, body:[STMT], line, col}`
- `{node:"WhileStmt", cond:NODE, body:[STMT], line, col}`
- `{node:"ReturnStmt", value:NODE|nil, line, col}`
- `{node:"TryStmt", body:[STMT], catch_var:Text|nil, catch_body:[STMT], line, col}`
- `{node:"ThrowStmt", value:NODE, line, col}`
- `{node:"OnStmt", event:Text, handler:NODE, line, col}`
- `{node:"ExprStmt", expr:NODE, line, col}`

## Expressions

- `{node:"IntLit", value:Int, line, col}`
- `{node:"FloatLit", value:Float, line, col}`
- `{node:"StrLit", value:Text, line, col}` — value AFTER escape processing
- `{node:"BoolLit", value:Bool, line, col}`
- `{node:"NilLit", line, col}`
- `{node:"DurationLit", ms:Int, line, col}`
- `{node:"MoneyLit", amount:Float, line, col}`
- `{node:"Ident", name:Text, line, col}`
- `{node:"Call", name:Text, args:[NODE], line, col}` — `name` may be dotted ("str.slice")
- `{node:"Field", obj:NODE, field:Text, line, col}`
- `{node:"Index", obj:NODE, idx:NODE, line, col}`
- `{node:"ListLit", items:[NODE], line, col}`
- `{node:"RecordLit", fields:[{name:Text, value:NODE}], line, col}`
- `{node:"MapLit", entries:[{key:Text, value:NODE}], line, col}`
- `{node:"Lambda", params:[{name:Text,type:Text}], body:[STMT], line, col}`
- `{node:"BinOp", op:Text, left:NODE, right:NODE, line, col}`
- `{node:"UnOp", op:Text, expr:NODE, line, col}` — op is "not" or "-"
- `{node:"Await", expr:NODE, line, col}`

## String interpolation

The Python parser splits `"hello {name}!"` via `parse_string_parts`.
The Misa AST represents an interpolated string as:
- `{node:"InterpStr", parts:[{node:"StrPart",value:Text}|{node:"ExprPart",expr:NODE}], line, col}`
A plain string with no `{...}` is just `StrLit`.

## Conventions

- `type` fields hold the TYPE TEXT as written (e.g. "Int", "[Text]", "Text?").
- Missing optionals are `nil`.
- `line`/`col` = the token where the node STARTS.
- Errors: `throw {code:"E001", message:Text, line:Int, col:Int}`.
