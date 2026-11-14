from __future__ import annotations

from abc import ABC
from uuid import UUID

from construct import Adapter, int2byte


class ISO6392TLanguageCode(Adapter, ABC):
    def _decode(self, obj, context, path=None, *args):
        return "".join([
            chr(bit + 0x60)
            for bit in (
                (obj >> 10) & 0b11111,
                (obj >> 5) & 0b11111,
                obj & 0b11111
            )
        ])

    def _encode(self, obj, context, path=None, *args):
        bits = [ord(c) - 0x60 for c in obj]
        return (bits[0] << 10) | (bits[1] << 5) | bits[2]


class MaskedInteger(Adapter, ABC):
    def _decode(self, obj, context, path=None, *args):
        return obj & 0x1F

    def _encode(self, obj, context, path=None, *args):
        return obj & 0x1F


class UUIDBytes(Adapter, ABC):
    def _decode(self, obj, context, path=None, *args):
        if isinstance(obj, UUID):
            return obj
        return UUID(bytes=obj)

    def _encode(self, obj, context, path=None, *args):
        if isinstance(obj, UUID):
            return obj.bytes
        if isinstance(obj, bytes):
            return obj
        if isinstance(obj, str):
            return UUID(obj).bytes
        return bytes(obj)