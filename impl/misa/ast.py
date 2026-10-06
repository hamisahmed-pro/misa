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


@dataclass
class VariantDecl(Node):
    """Phase 5b: one variant of a sum type, e.g. `Circle(radius: Float)`."""
    name: str = ""
    # (field_name, TypeExpr); positional fields (e.g. `Some(Int)`) get
    # synthetic names `_0`, `_1`, ...
    fields: List[Tuple[str, TypeExpr]] = field(default_factory=list)


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
class FnDecl(Node):
    name: str = ""
    params: List[Tuple[str, TypeExpr]] = field(default_factory=list)
    ret: Optional[TypeExpr] = None  # None = Void
    body: List[Node] = field(default_factory=list)


@dataclass
class ImportDecl(Node):
    path: str = ""  # as written, e.g. "utils.misa"
    resolved: str = ""  # absolute path, filled by the loader


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


@dataclass
class Call(Node):
    name: str = ""
    args: List[Node] = field(default_factory=list)
    # Phase 5a: calling the result of an expression, e.g. `mk(9)(1)`.
    # When callee is not None, `name` is unused.
    callee: Optional[Node] = None


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


@dataclass
class StreamDecl(Node):
    name: str = ""
    call: Optional[Node] = None  # Call to a model
