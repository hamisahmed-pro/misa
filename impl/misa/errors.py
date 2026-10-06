"""Misa v0.1 structured errors. Every error has a code (see spec §9)."""
from __future__ import annotations


class MisaError(Exception):
    code = "E300"

    def __init__(self, message: str, *, line: int = 0, col: int = 0,
                 hint: str = ""):
        super().__init__(message)
        self.message = message
        self.line = line
        self.col = col
        self.hint = hint

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "line": self.line, "col": self.col, "hint": self.hint}


class RuntimeError_(MisaError):
    code = "E300"


class UndefinedName(RuntimeError_):
    code = "E101"


class TypeMismatch(RuntimeError_):
    code = "E200"


class ImmutableAssign(RuntimeError_):
    code = "E201"


class AssertFailed(RuntimeError_):
    code = "E301"


class EnvNotSet(MisaError):
    """M-8: env() on an unset variable. Code E302, as STDLIB.md promises."""
    code = "E302"


class ModelError(MisaError):
    code = "E400"


class ProviderConfigError(ModelError):
    code = "E401"


class StreamingUnsupported(ModelError):
    code = "E402"


class OnDeviceShapeError(ModelError):
    code = "E501"


class OnDeviceSignatureError(ModelError):
    code = "E502"


class OnDeviceRuntimeUnavailable(ModelError):
    code = "E503"


class OnDeviceTargetIncompatible(MisaError):
    code = "E504"


class SchemaValidationError(ModelError):
    code = "E400"

    def __init__(self, message: str, *, validation_errors=None, **kw):
        super().__init__(message, **kw)
        self.validation_errors = validation_errors or []


class BudgetExceeded(MisaError):
    code = "E500"


class ThrownError(MisaError):
    """A `throw` value surfacing as a structured error. Carries the
    user-supplied code so `catch e` sees e.code."""

    def __init__(self, code: str, message: str, **kw):
        super().__init__(message, **kw)
        self.code = code


class IOError_(MisaError):
    code = "E303"


class HTTPError_(MisaError):
    code = "E304"


class MapKeyError(MisaError):
    code = "E305"


class FFIError(MisaError):
    """Phase 7: foreign-function interface failure — missing library,
    missing symbol, or marshalling failure. Code E226."""
    code = "E226"
