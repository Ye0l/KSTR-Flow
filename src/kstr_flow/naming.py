from __future__ import annotations

import keyword
import re


def str_to_raw_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Expected str, got {type(value).__name__}")
    value = value.lstrip()
    if not value:
        return "_"
    if value.isascii():
        value = re.sub(r"[^A-Za-z_0-9]", "_", value)
        value = re.sub(r"^[0-9]", lambda m: "_" + m.group(0), value, count=1)
    else:
        value = "".join(ch if f"_{ch}".isidentifier() else "_" for ch in value)
        if value and not value[0].isidentifier():
            value = "_" + value
    value = re.sub(r"__+", "_", value).rstrip("_") or "_"
    if keyword.iskeyword(value):
        value += "_"
    return value


def str_to_class_id(value: str) -> str:
    ident = str_to_raw_id(value)
    if ident.isupper():
        ident = ident.lower()
    ident = ident[0].upper() + ident[1:]
    return re.sub(r"_([A-Za-z])", lambda m: m.group(1).upper(), ident)
