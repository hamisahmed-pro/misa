"""Misa v0.2 AST nodes. Every node carries source position for diagnostics."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass
class Node:
    line: int = 0
    col: int = 0
    leading_comments: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- types

@dataclass
class TypeExpr(Node):
    name: str = ""
    is_list: bool = False
    is_map: bool = False
    map_key: Optional["TypeExpr"] = None
    map_val: Optional["TypeExpr"] = None
    optional: bool = False
    # Phase 5a: function types `(Int, Text) -> Bool`
    is_func: bool = False
    func_params: List["TypeExpr"] = field(default_factory=list)
    func_ret: Optional["TypeExpr"] = None
    # Phase 5c: applied generic types, e.g. `Box<Int>`.
    type_args: List["TypeExpr"] = field(default_factory=list)
    # Phase 5c: full inner expr for list types, preserving structure
    # (`[Box<Int>]`, `[[T]]`). When None, `name`+`is_list` carry the
    # (possibly flattened) form as before.
    list_elem: Optional["TypeExpr"] = None
    # Overtake final wave (FFI): C bitfield width for `field: Int:N`
    # inside an extern struct/union declaration. None = not a bitfield.
    bitfield_width: Optional[int] = None


# ---------------------------------------------------------------- program

@dataclass
class Program(Node):
    items: List[Node] = field(default_factory=list)


# ---------------------------------------------------------------- declarations

@dataclass
class AppDecl(Node):
    name: str = ""
    fields: List[Tuple[str, Node]] = field(default_factory=list)


@dataclass
class ProviderDecl(Node):
    name: str = ""
    key_env: str = ""      # environment variable holding the key
    model_file: str = ""   # on-device model path from `model: file("...")`


@dataclass
class ModelsDecl(Node):
    # (alias, provider_name, model_id)
    entries: List[Tuple[str, str, str]] = field(default_factory=list)


@dataclass
class TypeDecl(Node):
    name: str = ""
    # (field_name, TypeExpr)
    fields: List[Tuple[str, TypeExpr]] = field(default_factory=list)
    # Phase 5b: sum types. When is_sum is True, `fields` is unused and
    # `variants` holds the variant list instead.
    is_sum: bool = False
    variants: List["VariantDecl"] = field(default_factory=list)
    # Phase 5c: type parameters, e.g. `type Box<T> { value: T }`.
    type_params: List[str] = field(default_factory=list)
    # Phase 5d: interface bounds on type parameters. Inline
    # (`type Box<T: Printable>`) and `where`-clause
    # (`type C<K, V> where K: Hashable`) forms; both map a type
    # parameter name to interface name(s).
    type_param_bounds: dict = field(default_factory=dict)
    where_bounds: dict = field(default_factory=dict)


@dataclass
class VariantDecl(Node):
    """Phase 5b: one variant of a sum type, e.g. `Circle(radius: Float)`."""
    name: str = ""    # (field_name, TypeExpr); positional fields (e.g. `Some(Int)`) get
    # synthetic names `_0`, `_1`, ...
    fields: List[Tuple[str, TypeExpr]] = field(default_factory=list)


@dataclass
class TableDecl(Node):
    """Phase 5 (DB): a declared table schema for typed SQL queries, e.g.
    `table users { id: Int @primary, name: Text }`."""
    name: str = ""
    # (field_name, TypeExpr, is_primary)
    fields: List[Tuple[str, TypeExpr, bool]] = field(default_factory=list)


@dataclass
class Budget(Node):
    tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    cost_usd: Optional[float] = None


@dataclass
class ModelDecl(Node):
    name: str = ""
    params: List[Tuple[str, TypeExpr]] = field(default_factory=list)
    ret: Optional[TypeExpr] = None
    provider: str = ""
    alias: str = ""
    budget: Optional[Budget] = None
    prompt_version: int = 1
    prompt: List[Any] = field(default_factory=list)  # str | Expr


@dataclass
class Component(Node):
    name: str = ""
    args: List[Tuple[str, Node]] = field(default_factory=list)
    children: List["Component"] = field(default_factory=list)


@dataclass
class ScreenDecl(Node):
    name: str = ""
    params: List[Tuple[str, TypeExpr]] = field(default_factory=list)
    body: List[Node] = field(default_factory=list)  # Component|ScreenIf|ScreenFor
    # Phase 4 (M4): ordered (id-name, component-kind) bindings recorded
    # by the checker, slots 0..n-1. Ids inside ScreenFor bodies are
    # excluded (dynamic replication has no static slot; AOT rejects
    # those with E600).
    id_bindings: List[Tuple[str, str]] = field(default_factory=list)


@dataclass
class ScreenIf(Node):
    cond: Optional[Node] = None
    then: List[Node] = field(default_factory=list)
    els: List[Node] = field(default_factory=list)


@dataclass
class ScreenFor(Node):
    var: str = ""
    iter: Optional[Node] = None
    body: List[Node] = field(default_factory=list)


@dataclass
class EvalDecl(Node):
    name: str = ""
    model_name: str = ""
    given: List[Tuple[str, Node]] = field(default_factory=list)
    expect: Optional[Node] = None
    runs: int = 1


@dataclass
class TestDecl(Node):
    # `test <name> { ... }` — a unit test block. Parsed as a contextual
    # keyword (`test` is not reserved): `test foo { }` at the top level
    # is a TestDecl; `test` used any other way parses as an expression.
    name: str = ""
    body: List[Node] = field(default_factory=list)


@dataclass
class FnDecl(Node):
    name: str = ""
    params: List[Tuple[str, TypeExpr]] = field(default_factory=list)
    ret: Optional[TypeExpr] = None  # None = Void
    body: List[Node] = field(default_factory=list)
    # Phase 5c: type parameters, e.g. `fn first<T>(xs: [T]) -> T`.
    type_params: List[str] = field(default_factory=list)
    # Phase 8 (overtake): interface bounds, e.g. `fn f<T: Drawable>(x: T)`.
    # Maps type parameter name -> interface name.
    type_param_bounds: dict = field(default_factory=dict)
    # Phase 5d: `where`-clause bounds, e.g.
    # `fn sort<T>(xs: [T]) -> [T] where T: Comparable + Hashable`.
    # Maps type parameter name -> list of interface names. Merged with
    # type_param_bounds by the checker.
    where_bounds: dict = field(default_factory=dict)
    # Phase 8 (overtake): checked `throws` signatures, e.g.
    # `fn fetch(u: Text) -> Text throws E304`. The declared E-codes
    # (as written); empty = no clause (strict: uncaught codes error).
    throws: List[str] = field(default_factory=list)
    throws_line: int = 0
    throws_col: int = 0
    # Overtake Wave 2 (FFI): C varargs, e.g.
    # `extern "c" { fn printf(fmt: Text, ...) -> Int }`.
    # Only legal on extern declarations.
    varargs: bool = False


@dataclass
class ImportDecl(Node):
    path: str = ""  # as written, e.g. "utils.misa"
    resolved: str = ""  # absolute path, filled by the loader


@dataclass
class ExternDecl(Node):
    """Phase 7: `extern "<lib>" { fn f(...) -> T, ... }` — FFI block.

    Each entry is an FnDecl carrying only the signature (name, params,
    ret); body is always empty. `lib` is the library name as written,
    e.g. "c" (C standard library) or "m" (libm).

    Overtake Wave 2: blocks may also declare C-layout structs
    (`struct Point { x: Float, y: Float }`) usable by value and by
    pointer in the block's signatures; `lib` may be a direct shared
    object path (contains "/" or ends in .so/.dylib) instead of a
    `-l` name.
    """
    lib: str = ""
    fns: List["FnDecl"] = field(default_factory=list)
    structs: List["StructDecl"] = field(default_factory=list)


@dataclass
class StructDecl(Node):
    """Overtake Wave 2 (FFI): `struct Point { x: Float, y: Float }`
    inside an `extern` block. Declares a C-layout struct type usable
    in extern signatures by value and by pointer, and constructible
    from Misa as `Point { x: 1.0, y: 2.0 }`.

    v1 fields: Int, Float, Bool, Ptr only (checked with E225).

    Overtake final wave: `kind` is "struct" or "union" (`union Value
    { i: Int, f: Float }` — all fields share offset 0); fields may be
    nested aggregates (another declared struct/union) or bitfields
    (`a: Int:3`, stored on the field's TypeExpr.bitfield_width).
    """
    name: str = ""
    fields: List[Tuple[str, "TypeExpr"]] = field(default_factory=list)
    kind: str = "struct"


@dataclass
class StructLit(Node):
    """Overtake Wave 2 (FFI): `Point { x: 1.0, y: 2.0 }` — construct
    a C-layout struct value. All declared fields required (E211
    otherwise); unknown fields are E101."""
    name: str = ""
    fields: List[Tuple[str, Node]] = field(default_factory=list)


@dataclass
class InterfaceDecl(Node):
    """Phase 8 (overtake): `interface Drawable { fn draw(self) -> Text }`.

    `methods` are FnDecls carrying signatures only (no bodies — a body
    in an interface method is a parse error). The first param must be
    named `self`.
    """
    name: str = ""
    methods: List["FnDecl"] = field(default_factory=list)


@dataclass
class ImplDecl(Node):
    """Phase 8 (overtake): `impl Drawable for Circle { ... }`.

    `methods` are FnDecls with bodies; the first param must be named
    `self` (bare `self` defaults to the target type, `self: Circle`
    must match it). Signatures must match the interface's.
    """
    iface: str = ""
    target: str = ""
    methods: List["FnDecl"] = field(default_factory=list)


@dataclass
class IfaceWrap(Node):
    """Phase 8 (overtake): checker-inserted conversion of a concrete
    value to an interface value. The interpreter builds an IfaceValue;
    AOT builds a {__tag, __fields} record."""
    iface: str = ""
    concrete: str = ""
    expr: Optional[Node] = None


@dataclass
class IfaceWrapList(Node):
    """Phase 8 (overtake): checker-inserted element-wise conversion of
    a [T] value to [I] (single level; nested non-literal coercion is
    E200 in v1)."""
    iface: str = ""
    concrete: str = ""
    expr: Optional[Node] = None


# ---------------------------------------------------------------- statements

@dataclass
class LetStmt(Node):
    name: str = ""
    ann: Optional[TypeExpr] = None
    value: Optional[Node] = None
    mutable: bool = False  # False=let, True=var


@dataclass
class AssignStmt(Node):
    target: Optional[Node] = None
    value: Optional[Node] = None


@dataclass
class IfStmt(Node):
    cond: Optional[Node] = None
    then: List[Node] = field(default_factory=list)
    els: List[Node] = field(default_factory=list)


@dataclass
class ForStmt(Node):
    var: str = ""
    iter: Optional[Node] = None
    body: List[Node] = field(default_factory=list)


@dataclass
class WhileStmt(Node):
    cond: Optional[Node] = None
    body: List[Node] = field(default_factory=list)


@dataclass
class ReturnStmt(Node):
    value: Optional[Node] = None


@dataclass
class TryStmt(Node):
    body: List[Node] = field(default_factory=list)
    catch_var: str = ""
    handler: List[Node] = field(default_factory=list)


@dataclass
class ThrowStmt(Node):
    value: Optional[Node] = None


@dataclass
class OnStmt(Node):
    stream: Optional[Node] = None
    event: str = ""
    handler: Optional[Node] = None  # Lambda


@dataclass
class ExprStmt(Node):
    expr: Optional[Node] = None


# ---------------------------------------------------------------- expressions

@dataclass
class IntLit(Node):
    value: int = 0


@dataclass
class FloatLit(Node):
    value: float = 0.0


@dataclass
class StrLit(Node):
    parts: List[Any] = field(default_factory=list)  # str | Expr
    raw: bool = False  # W-6: from r"..." — fmt must preserve rawness


@dataclass
class BoolLit(Node):
    value: bool = False


@dataclass
class NilLit(Node):
    pass


@dataclass
class DurationLit(Node):
    ms: int = 0


@dataclass
class MoneyLit(Node):
    usd: float = 0.0


@dataclass
class Ident(Node):
    name: str = ""
    # Phase 5c: transient — type args parsed on `Result<Int>.Ok(...)`;
    # consumed by parse_postfix into the Call node.
    type_args: List[TypeExpr] = field(default_factory=list)


@dataclass
class Call(Node):
    name: str = ""
    args: List[Node] = field(default_factory=list)
    # Phase 5a: calling the result of an expression, e.g. `mk(9)(1)`.
    # When callee is not None, `name` is unused.
    callee: Optional[Node] = None
    # Phase 5c: explicit type arguments, e.g. `first<Text>(["x"])`,
    # `Box<Int>(42)`, `Result<Int>.Ok(42)`.
    type_args: List[TypeExpr] = field(default_factory=list)


@dataclass
class Field(Node):
    obj: Optional[Node] = None
    field: str = ""


@dataclass
class Index(Node):
    obj: Optional[Node] = None
    idx: Optional[Node] = None


@dataclass
class ListLit(Node):
    items: List[Node] = field(default_factory=list)


@dataclass
class RecordLit(Node):
    fields: List[Tuple[str, Node]] = field(default_factory=list)


@dataclass
class MapLit(Node):
    # (key_string, value); keys are Text or Int at runtime
    entries: List[Tuple[Any, Node]] = field(default_factory=list)


@dataclass
class Lambda(Node):
    # Phase 5a: (param_name, optional TypeExpr); None = untyped, inferred
    # from the expected function type at the use site.
    params: List[Tuple[str, Optional[TypeExpr]]] = field(
        default_factory=list)
    body: List[Node] = field(default_factory=list)
    expr_body: Optional[Node] = None


@dataclass
class MatchArm(Node):
    """Phase 5b: one arm of a `match` expression."""
    # Pattern: Type.Variant(binds...) — or a wildcard when wildcard=True
    # (type_name/variant/binds unused then).
    type_name: str = ""
    variant: str = ""
    binds: List[str] = field(default_factory=list)
    body: Optional[Node] = None
    wildcard: bool = False
    # Phase 5d: optional `if` guard, e.g.
    # `Shape.Circle(r) if r > 0 => ...`. Evaluated with the arm's
    # bindings in scope; the arm only fires when it is true.
    guard: Optional[Node] = None


@dataclass
class Match(Node):
    """Phase 5b: `match` expression over a sum-typed scrutinee."""
    scrutinee: Optional[Node] = None
    arms: List[MatchArm] = field(default_factory=list)


@dataclass
class BinOp(Node):
    op: str = ""
    left: Optional[Node] = None
    right: Optional[Node] = None


@dataclass
class UnOp(Node):
    op: str = ""
    operand: Optional[Node] = None


@dataclass
class Await(Node):
    operand: Optional[Node] = None
    # Phase 7 (concurrency): `await t timeout <ms>` — optional timeout
    # expression (Int milliseconds). On expiry the await raises E306.
    timeout: Optional[Node] = None


@dataclass
class Spawn(Node):
    """Phase 7 (concurrency): `spawn { ... }` — a concurrent task.

    An expression evaluating to `Task<T>` where `T` is the block's
    return type (a block with no `return` is `Task<Void>`).
    """
    body: List[Node] = field(default_factory=list)


@dataclass
class StreamDecl(Node):
    name: str = ""
    call: Optional[Node] = None  # Call to a model
