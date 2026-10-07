"""Canonical C layout for Misa `extern` aggregates (structs, unions).

Overtake final wave: nested structs, unions, and bitfields. This module
is the single source of truth for C layout, shared by the checker
(size validation, cycle detection), the interpreter (ctypes classes
mirror this layout exactly), and the AOT emitter (LLVM types and
GEP/bitfield access follow it).

C rules implemented (System V / AAPCS64 / MSVC-compatible subset):
- Scalar sizes/alignments: Int->8/8, Float->8/8, Bool->4/4, Ptr->8/8.
- Struct fields are laid out in order, each at the next offset aligned
  to its own alignment; the struct's size is rounded up to its maximum
  field alignment; its alignment is that maximum.
- Nested aggregates are laid out recursively (a nested struct/union
  contributes its own size and alignment).
- Unions: every field starts at offset 0; size = max field size (at
  least 1); alignment = max field alignment.
- Bitfields (`field: Int:N`, 1 <= N <= 64): consecutive bitfields pack
  into 8-byte (Int) units, little-endian (bit 0 = LSB), never straddling
  a unit boundary. A non-bitfield field closes the open unit. In a
  union, each bitfield starts at bit 0 of offset 0 (matching GCC).

LayoutError is raised for undeclared field types and cyclic nesting;
callers convert it to E225.
"""

SCALAR_SIZE_ALIGN = {
    "Int": (8, 8),
    "Float": (8, 8),
    "Bool": (4, 4),
    "Ptr": (8, 8),
}

BITFIELD_UNIT_SIZE = 8   # Int bitfields pack into 8-byte units
BITFIELD_UNIT_ALIGN = 8
BITFIELD_MAX_WIDTH = 64


class LayoutError(Exception):
    pass


def _align_up(offset, align):
    return (offset + align - 1) // align * align


def _field_size_align(ftype_name, bitwidth, decls, seen):
    """(size, align, desc) for one field.

    desc is ("scalar", tname) | ("aggr", decl_name) | ("bits", width).
    """
    if bitwidth is not None:
        if ftype_name != "Int":
            raise LayoutError(
                f"bitfields must be declared as Int (got '{ftype_name}')")
        if not (1 <= bitwidth <= BITFIELD_MAX_WIDTH):
            raise LayoutError(
                f"bitfield width must be 1..{BITFIELD_MAX_WIDTH} "
                f"(got {bitwidth})")
        return (BITFIELD_UNIT_SIZE, BITFIELD_UNIT_ALIGN, ("bits", bitwidth))
    if ftype_name in SCALAR_SIZE_ALIGN:
        sz, al = SCALAR_SIZE_ALIGN[ftype_name]
        return (sz, al, ("scalar", ftype_name))
    if ftype_name in decls:
        if ftype_name in seen:
            raise LayoutError(
                f"cyclic nesting through '{ftype_name}' (C requires "
                f"complete types for by-value fields)")
        size, align, _ = layout_aggregate(decls[ftype_name], decls,
                                          seen + (ftype_name,))
        return (size, align, ("aggr", ftype_name))
    raise LayoutError(f"unknown field type '{ftype_name}'")


def layout_aggregate(decl, decls, seen=()):
    """C layout for one extern aggregate.

    decl: ast.StructDecl with .kind in {"struct", "union"}, .fields as
      [(fname, type_expr)] where type_expr has .name and
      .bitfield_width (None or int).
    decls: name -> StructDecl for every declared aggregate.

    Returns (size, align, fields) where fields is a list of
    (fname, desc, offset, extra):
      desc "scalar": extra = tname ("Int"/"Float"/"Bool"/"Ptr")
      desc "aggr":   extra = nested aggregate name
      desc "bits":   extra = (unit_offset, bit_offset, width)
    """
    kind = getattr(decl, "kind", "struct")
    if kind == "union":
        return _layout_union(decl, decls, seen)
    return _layout_struct(decl, decls, seen)


def _layout_union(decl, decls, seen):
    if not decl.fields:
        raise LayoutError(f"union '{decl.name}' must declare at least "
                          f"one field")
    size, align, fields = 1, 1, []
    for fname, fte in decl.fields:
        sz, al, desc = _field_size_align(fte.name, fte.bitfield_width,
                                        decls, seen)
        if desc[0] == "bits":
            width = desc[1]
            # Union bitfields all start at bit 0 of offset 0 (GCC).
            fields.append((fname, "bits", 0, (0, 0, width)))
            sz, al = BITFIELD_UNIT_SIZE, BITFIELD_UNIT_ALIGN
        else:
            fields.append((fname, desc[0], 0, desc[1]))
        size = max(size, sz)
        align = max(align, al)
    return (size, align, fields)


def _layout_struct(decl, decls, seen):
    offset, max_align, fields = 0, 1, []
    # Open bitfield unit: (unit_offset, bits_used) or None.
    unit = None
    for fname, fte in decl.fields:
        sz, al, desc = _field_size_align(fte.name, fte.bitfield_width,
                                        decls, seen)
        if desc[0] == "bits":
            width = desc[1]
            if unit is None or unit[1] + width > 64:
                # Open a fresh 8-byte unit (never straddle, like GCC).
                offset = _align_up(offset, BITFIELD_UNIT_ALIGN)
                unit = (offset, 0)
                offset += BITFIELD_UNIT_SIZE
            uoff, used = unit
            fields.append((fname, "bits", uoff, (uoff, used, width)))
            unit = (uoff, used + width)
            max_align = max(max_align, BITFIELD_UNIT_ALIGN)
        else:
            unit = None  # a plain field closes the open bitfield unit
            offset = _align_up(offset, al)
            fields.append((fname, desc[0], offset, desc[1]))
            offset += sz
            max_align = max(max_align, al)
    size = _align_up(offset, max_align)
    return (size, max_align, fields)


def aggregate_size(name, decls):
    """Total C size of a declared aggregate (recursive)."""
    size, _, _ = layout_aggregate(decls[name], decls, (name,))
    return size


def field_map(name, decls):
    """fname -> (desc, offset, extra) for quick field lookup."""
    _, _, fields = layout_aggregate(decls[name], decls, (name,))
    return {fname: (desc, offset, extra)
            for fname, desc, offset, extra in fields}


# -- SysV AMD64 union classification (overtake final wave) ----------------
# A bare LLVM byte array mislowers at the FFI boundary (passed on the
# stack) while C passes small unions in registers. The AOT backend
# coerces union values to these per-eightbyte classes at calls.

def union_chunk_classes(name, decls):
    """SysV AMD64 eightbyte classes for a union: one 'int' or 'sse'
    per 8-byte chunk, by merging the classes of all fields overlapping
    each chunk ('sse' only when every contributor is 'sse').

    Verified against `gcc -S`: {i64,double} -> ['int'] (RDI),
    {double,double} -> ['sse'] (XMM0), {double,{i64,i64}} ->
    ['int','int'] (RDI,RSI)."""
    nchunks = (aggregate_size(name, decls) + 7) // 8
    chunk_sets = [set() for _ in range(nchunks)]
    decl = decls[name]
    for _fname, fte in decl.fields:
        for k, c in enumerate(_type_chunk_classes(fte, decls)):
            if k < nchunks:
                chunk_sets[k].add(c)
    return ["sse" if s == {"sse"} else "int" for s in chunk_sets]


def _type_chunk_classes(fte, decls):
    """Per-eightbyte 'int'/'sse' classes for a field type."""
    if fte.bitfield_width is not None:
        return ["int"]
    n = fte.name
    if n in SCALAR_SIZE_ALIGN:
        return ["sse"] if n == "Float" else ["int"]
    if n not in decls:
        raise LayoutError(f"unknown type '{n}'")
    sub = decls[n]
    if sub.kind == "union":
        return union_chunk_classes(n, decls)
    return _struct_chunk_classes(n, decls)


def _struct_chunk_classes(name, decls):
    """Per-eightbyte 'int'/'sse' classes for a struct, following its
    own C layout (field byte offsets from field_map)."""
    nchunks = (aggregate_size(name, decls) + 7) // 8
    chunk_sets = [set() for _ in range(nchunks)]
    fmap = field_map(name, decls)
    for _fname, (desc, off, extra) in fmap.items():
        if desc == "bits":
            # Bitfield unit: 8 bytes, always integer class.
            chunk_sets[off // 8].add("int")
            continue
        if desc == "scalar":
            fclasses = ["sse"] if extra == "Float" else ["int"]
        else:  # nested aggregate
            sub = decls[extra]
            if sub.kind == "union":
                fclasses = union_chunk_classes(extra, decls)
            else:
                fclasses = _struct_chunk_classes(extra, decls)
        for k, c in enumerate(fclasses):
            # Field eightbyte k covers bytes [off+8k, off+8k+8);
            # attribute it to every chunk it overlaps.
            lo, hi = off + 8 * k, off + 8 * k + 8
            for j in range(nchunks):
                if lo < 8 * (j + 1) and hi > 8 * j:
                    chunk_sets[j].add(c)
    return ["sse" if s == {"sse"} else "int" for s in chunk_sets]
