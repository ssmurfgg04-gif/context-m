"""Pure-Python secp256k1 backend for ZK proofs (fastecdsa fallback).

fastecdsa ships no Windows wheels, so ``pip install fastecdsa`` fails on
stock Windows Python (needs MSVC to build its C extension). This module
adapts the pure-Python ``ecdsa`` package (always installable) to the exact
duck-type surface ``zk_proofs.py`` uses from fastecdsa:

* curve object with ``.G`` / ``.q`` / ``.p``
* ``Point(x, y, curve)`` constructor with on-curve validation
* ``int * Point`` (rmul), ``Point + Point``, ``Point - Point``,
  ``-Point``, ``==``, ``.x`` / ``.y`` int attributes

fastecdsa stays the preferred backend (C speed); this one is only used
when the fastecdsa import fails. Slower (~ms per scalar mult vs ~us),
but correct — ZK prove/verify is an occasional op, not the hot path.
Determinism contract holds: same scalars in, same points out.
"""

from __future__ import annotations

from ecdsa import SECP256k1
from ecdsa.ellipticcurve import INFINITY as _EC_INFINITY
from ecdsa.ellipticcurve import Point as _ECPoint

_ORDER: int = SECP256k1.order
_P: int = SECP256k1.curve.p()


class EcdsaPoint:
    """fastecdsa-Point-compatible wrapper around ecdsa's Point."""

    __slots__ = ("_pt",)

    def __init__(self, x: int, y: int, curve=None) -> None:
        if (y * y - (x * x * x + 7)) % _P != 0:
            raise ValueError(f"({x}, {y}) is not on secp256k1")
        self._pt = _ECPoint(SECP256k1.curve, x, y, _ORDER)

    @classmethod
    def _from_raw(cls, pt) -> EcdsaPoint:
        obj = cls.__new__(cls)
        obj._pt = pt
        return obj

    @classmethod
    def _infinity(cls) -> EcdsaPoint:
        return cls._from_raw(_EC_INFINITY)

    @property
    def x(self) -> int:
        return self._pt.x()

    @property
    def y(self) -> int:
        return self._pt.y()

    def __add__(self, other: EcdsaPoint) -> EcdsaPoint:
        if not isinstance(other, EcdsaPoint):
            return NotImplemented
        return EcdsaPoint._from_raw(self._pt + other._pt)

    def __sub__(self, other: EcdsaPoint) -> EcdsaPoint:
        if not isinstance(other, EcdsaPoint):
            return NotImplemented
        return self + (-other)

    def __neg__(self) -> EcdsaPoint:
        if self._pt == _EC_INFINITY:
            return self
        return EcdsaPoint._from_raw(
            _ECPoint(SECP256k1.curve, self._pt.x(), (-self._pt.y()) % _P, _ORDER)
        )

    def __rmul__(self, scalar: int) -> EcdsaPoint:
        s = int(scalar) % _ORDER
        if s == 0:
            return EcdsaPoint._infinity()
        return EcdsaPoint._from_raw(self._pt * s)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EcdsaPoint):
            return NotImplemented
        return self._pt == other._pt

    def __hash__(self) -> int:
        if self._pt == _EC_INFINITY:
            return hash(("secp256k1-inf",))
        return hash((self._pt.x(), self._pt.y()))

    def __repr__(self) -> str:
        if self._pt == _EC_INFINITY:
            return "EcdsaPoint(INFINITY)"
        return f"EcdsaPoint(x={self._pt.x()}, y={self._pt.y()})"


class EcdsaCurve:
    """fastecdsa-curve-compatible namespace (G / q / p)."""

    G = EcdsaPoint._from_raw(SECP256k1.generator)
    q: int = _ORDER
    p: int = _P


BACKEND_NAME = "ecdsa-pure-python"
