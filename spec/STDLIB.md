# Misa Standard Library — v0.2

v0.2 (2026-10-05); supersedes v0.1. Everything here is available with
no import.

## 1. Builtin functions

| Function | Signature | Description |
|---|---|---|
| `print` | `print(x: Text)` | Write a line to stdout. `print` accepts any value; non-text renders canonically. |
| `len` | `len(x: [T]) -> Int` | Number of elements. Also accepts `Text` (chars) and `Map` (entries). |
| `str` | `str(x: any) -> Text` | Canonical text rendering of any value. |
| `int` | `int(x: Text) -> Int` | Parse; error `E300` on failure. |
| `float` | `float(x: Text) -> Float` | Parse; error `E300` on failure. |
| `assert` | `assert(cond: Bool, msg: Text)` | Abort with `E301` and `msg` if `cond` is false. |
| `env` | `env(name: Text) -> Text` | Environment variable; error `E302` if unset. |

## 2. Filesystem (`fs` namespace)

| Function | Signature | Description |
|---|---|---|
| `fs.read` | `fs.read(path: Text) -> Text` | Read a whole file as text; throws `Error{code: "E303"}` on failure. |
| `fs.write` | `fs.write(path: Text, text: Text)` | Write text to a file (creates/overwrites); throws `Error{code: "E303"}` on failure. |

Paths are resolved against the process working directory. `misa run`
is a developer tool and runs with the user's privileges; do not run
untrusted Misa programs — a program can read and write your files.

## 3. HTTP (`http` namespace)

| Function | Signature | Description |
|---|---|---|
| `http.get` | `http.get(url: Text) -> Text` | GET a URL, return the body as text; throws `Error{code: "E304"}` on network failure or non-2xx status (30s timeout). |

## 4. Maps

`Map<K, V>` with `K` = `Text` or `Int`:

```
let scores: Map<Text, Int> = {"ada": 92, "bisi": 71}
print(scores["ada"])   // 92
scores["chidi"] = 58   // insert
```

- Literal keys are strings (`{"k": v}`); a leading `IDENT` key would
  be a record literal instead. `{}` is an empty record; annotate for
  an empty map.
- `m[k]` returns the value; a missing key throws
  `Error{code: "E305"}` (catchable). `m[k] = v` inserts or updates
  (requires a `var` binding, like list element assignment).
- `len(m)` is the entry count.

## 5. Media constructors

Calling a media type name with a path builds a handle:

- `Photo("img.jpg")`, `Audio("a.mp3")`, `Video("v.mp4")`,
  `Document("report.pdf")`

A handle renders in prompts as `<Photo: img.jpg>`. The `gemini`
provider upgrades `Photo` handles whose files exist to real image
parts. (Phase 2: transcoding, thumbnails, OCR/text extraction.)

## 6. Screen components (closed set)

Layout: `Column`, `Row`, `Spacer`
Text: `Text(text:)`, `Heading(text:)`, `Banner(text:, style:)` —
  `style` is one of `"info"`, `"warning"`, `"error"`.
Input: `Button(label:, onTap:)`, `TextInput(id:, hint:)`,
  `CameraButton(onCapture:)`
Media: `Image(photo:)`
Lists: `List(items:)` — `items` is `[Text]` in v0.1 (Phase 2: rows).

Component arguments are named (`label: "Go"`). A block after the
argument list holds child components (layout only; giving children
to a leaf component is error `E206`).

`id: name` binds the component in the screen scope. Bound components
expose: `TextInput.text -> Text`. Event lambdas: `onTap: || { ... }`,
`onCapture: |photo| { ... }`.

## 7. Reserved for future versions

`Embedding` type and vector ops, JSON value type, dates/times,
on-device inference bindings, first-class function values.
