#!/usr/bin/env python3
"""
Unit Converter
==============

A tiny, dependency-free command-line converter for length, weight and
temperature. Pure arithmetic only -- no file access, no network, no external
processes -- so it is safe to run anywhere.

Usage:
    python unit_converter.py 10 km mi
    python unit_converter.py 72 f c
    python unit_converter.py --list
"""

from __future__ import annotations

import argparse
from typing import Dict, List, Optional

# --------------------------------------------------------------------------- #
# Conversion tables
# --------------------------------------------------------------------------- #
# Length and weight are expressed relative to a base unit (metre / kilogram),
# so any pair converts by going through the base: value -> base -> target.

LENGTH_TO_METRE: Dict[str, float] = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "km": 1000.0,
    "in": 0.0254,
    "ft": 0.3048,
    "yd": 0.9144,
    "mi": 1609.344,
}

WEIGHT_TO_KILOGRAM: Dict[str, float] = {
    "mg": 0.000001,
    "g": 0.001,
    "kg": 1.0,
    "oz": 0.0283495,
    "lb": 0.453592,
    "st": 6.35029,
}

TEMPERATURE_UNITS = ("c", "f", "k")


# --------------------------------------------------------------------------- #
# Core conversion
# --------------------------------------------------------------------------- #

def convert_linear(value: float, src: str, dst: str, table: Dict[str, float]) -> float:
    """Convert between two units that share a linear base (length / weight)."""
    base = value * table[src]
    return base / table[dst]


def convert_temperature(value: float, src: str, dst: str) -> float:
    """Convert between Celsius, Fahrenheit and Kelvin."""
    # Step 1: bring the input to Celsius.
    if src == "c":
        celsius = value
    elif src == "f":
        celsius = (value - 32.0) * 5.0 / 9.0
    else:  # kelvin
        celsius = value - 273.15

    # Step 2: send Celsius out to the requested unit.
    if dst == "c":
        return celsius
    if dst == "f":
        return celsius * 9.0 / 5.0 + 32.0
    return celsius + 273.15


def convert(value: float, src: str, dst: str) -> float:
    """Dispatch to the right converter based on the unit family."""
    src, dst = src.lower(), dst.lower()

    if src in LENGTH_TO_METRE and dst in LENGTH_TO_METRE:
        return convert_linear(value, src, dst, LENGTH_TO_METRE)
    if src in WEIGHT_TO_KILOGRAM and dst in WEIGHT_TO_KILOGRAM:
        return convert_linear(value, src, dst, WEIGHT_TO_KILOGRAM)
    if src in TEMPERATURE_UNITS and dst in TEMPERATURE_UNITS:
        return convert_temperature(value, src, dst)

    raise ValueError(
        f"cannot convert from '{src}' to '{dst}' "
        "(units must belong to the same family)"
    )


def supported_units() -> Dict[str, List[str]]:
    """Return every supported unit grouped by family, for the --list output."""
    return {
        "length": sorted(LENGTH_TO_METRE),
        "weight": sorted(WEIGHT_TO_KILOGRAM),
        "temperature": list(TEMPERATURE_UNITS),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python unit_converter.py",
        description="Convert a value between length, weight or temperature units.",
    )
    parser.add_argument("value", nargs="?", type=float, help="The number to convert.")
    parser.add_argument("src", nargs="?", help="Source unit (e.g. km, lb, f).")
    parser.add_argument("dst", nargs="?", help="Target unit (e.g. mi, kg, c).")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List every supported unit grouped by family and exit.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for family, units in supported_units().items():
            print(f"{family:>12}: {', '.join(units)}")
        return 0

    if args.value is None or args.src is None or args.dst is None:
        parser.error("provide VALUE SRC DST, e.g. '10 km mi' (or use --list)")

    try:
        result = convert(args.value, args.src, args.dst)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1

    print(f"{args.value:g} {args.src} = {result:g} {args.dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
