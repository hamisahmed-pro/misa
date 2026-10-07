"""Pure-Python Ed25519 (RFC 8032) for Misa's registry signing.

No third-party dependencies — the registry Docker image is stdlib-only.
Used by the interpreter builtins (crypto.ed25519_*) and directly by
impl/misa/pkg.py (the CLI client). Verification of a 1MB tarball's
hash takes ~5ms; keygen/sign are similarly fast.
"""
import hashlib
import os

# Field prime and curve constants.
P = 2 ** 255 - 19
D = -121665 * pow(121666, P - 2, P) % P
Gx = 15112221349535400772501151409588531511454012693041857206046113283949847762202
Gy = 46316835694926478169428394003475163141307993866256225615783033603165251855960
L = 2 ** 252 + 27742317777372353535851937790883648493  # group order


def _inv(x: int) -> int:
    return pow(x, P - 2, P)


def _edwards_add(p1, p2):
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = t1 * 2 * D * t2 % P
    d = z1 * 2 * z2 % P
    e = b - a
    f = d - c
    g = d + c
    h = b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _edwards_mul(s: int, p):
    # constant-ish time ladder over 256 bits
    q = (0, 1, 1, 0)  # identity in extended coordinates
    bits = bin(s)[2:].zfill(256)
    for bit in bits:
        q = _edwards_add(q, q)
        if bit == "1":
            q = _edwards_add(q, p)
    return q


_BASE = (Gx, Gy, 1, Gx * Gy % P)


def _encode_point(p) -> bytes:
    x, y, z, _t = p
    zi = _inv(z)
    x, y = x * zi % P, y * zi % P
    bit = x & 1
    return (y | (bit << 255)).to_bytes(32, "little")


def _decode_point(s: bytes):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    sign = (s[31] >> 7) & 1
    # x = sqrt((y^2 - 1) / (d*y^2 + 1))
    y2 = y * y % P
    num = (y2 - 1) % P
    den = (D * y2 + 1) % P
    x2 = num * _inv(den) % P
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * pow(2, (P - 1) // 4, P) % P
    if (x * x - x2) % P != 0:
        return None
    if (x & 1) != sign:
        x = P - x
    return (x, y, 1, x * y % P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def keygen() -> tuple[bytes, bytes]:
    """Returns (private_key_32, public_key_32)."""
    seed = os.urandom(32)
    return seed, _pubkey(seed)


def _pubkey(seed: bytes) -> bytes:
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= ~(7 | (1 << 255))  # clear low 3 bits and bit 255
    a |= 1 << 254           # set bit 254
    return _encode_point(_edwards_mul(a, _BASE))


def sign(seed: bytes, msg: bytes) -> bytes:
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= ~(7 | (1 << 255))
    a |= 1 << 254
    r = int.from_bytes(_sha512(h[32:] + msg), "little") % L
    R = _encode_point(_edwards_mul(r, _BASE))
    A = _pubkey(seed)
    k = int.from_bytes(_sha512(R + A + msg), "little") % L
    S = (r + k * a) % L
    return R + S.to_bytes(32, "little")


def verify(pubkey: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pubkey) != 32 or len(sig) != 64:
        return False
    A = _decode_point(pubkey)
    if A is None:
        return False
    R = _decode_point(sig[:32])
    if R is None:
        return False
    S = int.from_bytes(sig[32:], "little")
    if S >= L:
        return False
    k = int.from_bytes(_sha512(sig[:32] + pubkey + msg), "little") % L
    # check [S]B == R + [k]A
    lhs = _edwards_mul(S, _BASE)
    rhs = _edwards_add(R, _edwards_mul(k, A))
    return _encode_point(lhs) == _encode_point(rhs)


def fingerprint(pubkey: bytes) -> str:
    """Hex SHA256 of the public key — the key identity."""
    return hashlib.sha256(pubkey).hexdigest()
