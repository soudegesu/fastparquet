"""encoding.py - methods for reading parquet encoded data blocks."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import array
import io
import math
import numpy as np
import os
import struct
import sys

from .thrift_structures import parquet_thrift


def read_plain_boolean(raw_bytes, count):
    """Read `count` booleans using the plain encoding."""
    return np.unpackbits(np.fromstring(raw_bytes, dtype=np.uint8)).reshape(
            (-1, 8))[:, ::-1].ravel().astype(bool)[:count]

DECODE_TYPEMAP = {
    parquet_thrift.Type.INT32: np.int32,
    parquet_thrift.Type.INT64: np.int64,
    parquet_thrift.Type.INT96: np.dtype('S12'),
    parquet_thrift.Type.FLOAT: np.float32,
    parquet_thrift.Type.DOUBLE: np.float64,
}


def read_plain(raw_bytes, type_, count, width=0):
    if type_ in DECODE_TYPEMAP:
        dtype = DECODE_TYPEMAP[type_]
        return np.frombuffer(memoryview(raw_bytes), dtype=dtype, count=count)
    if type_ == parquet_thrift.Type.FIXED_LEN_BYTE_ARRAY:
        if count == 1:
            width = len(raw_bytes)
        dtype = np.dtype('S%i' % width)
        return np.frombuffer(memoryview(raw_bytes), dtype=dtype, count=count)
    if type_ == parquet_thrift.Type.BOOLEAN:
        return read_plain_boolean(raw_bytes, count)
    # variable byte arrays (rare)
    file_obj = io.BytesIO(raw_bytes)
    return np.array([file_obj.read(struct.unpack('<i', file_obj.read(4))[0])
                     for _ in range(count)], dtype="O")


def read_unsigned_var_int(file_obj):  # pragma: no cover
    """Read a value using the unsigned, variable int encoding.
    file-obj is a NumpyIO of bytes; avoids struct to allow numba-jit
    """
    result = 0
    shift = 0
    while True:
        byte = file_obj.read_byte()
        result |= ((byte & 0x7F) << shift)
        if (byte & 0x80) == 0:
            break
        shift += 7
    return result


def read_rle(file_obj, header, bit_width, o):  # pragma: no cover
    """Read a run-length encoded run from the given fo with the given header and bit_width.

    The count is determined from the header and the width is used to grab the
    value that's repeated. Yields the value repeated count times.
    """
    count = header >> 1
    width = (bit_width + 7) // 8
    zero = np.zeros(4, dtype=np.int8)
    data = file_obj.read(width)
    zero[:len(data)] = data
    value = zero.view(np.int32)
    o.write_many(value, count)


def width_from_max_int(value):  # pragma: no cover
    """Convert the value specified to a bit_width."""
    for i in range(0, 64):
        if value == 0:
            return i
        value >>= 1


def _mask_for_bits(i):  # pragma: no cover
    """Generate a mask to grab `i` bits from an int value."""
    return (1 << i) - 1


def read_bitpacked(file_obj, header, width, o):  # pragma: no cover
    """
    Read values packed into width-bits each (which can be >8)

    file_obj is a NumbaIO array (int8)
    o is an output NumbaIO array (int32)
    """
    num_groups = header >> 1
    count = num_groups * 8
    byte_count = (width * count) // 8
    raw_bytes = file_obj.read(byte_count)
    mask = _mask_for_bits(width)
    current_byte = 0
    data = raw_bytes[current_byte]
    bits_wnd_l = 8
    bits_wnd_r = 0
    total = byte_count * 8
    while total >= width:
        # NOTE zero-padding could produce extra zero-values
        if bits_wnd_r >= 8:
            bits_wnd_r -= 8
            bits_wnd_l -= 8
            data >>= 8
        elif bits_wnd_l - bits_wnd_r >= width:
            o.write_byte((data >> bits_wnd_r) & mask)
            total -= width
            bits_wnd_r += width
        elif current_byte + 1 < byte_count:
            current_byte += 1
            data |= (raw_bytes[current_byte] << bits_wnd_l)
            bits_wnd_l += 8


def read_rle_bit_packed_hybrid(io_obj, width, length=None, o=None):  # pragma: no cover
    """Read values from `io_obj` using the rel/bit-packed hybrid encoding.

    If length is not specified, then a 32-bit int is read first to grab the
    length of the encoded data.

    file-obj is a NumpyIO of bytes; o if an output NumpyIO of int32
    """
    if length is None:
        length = read_length(io_obj)
    start = io_obj.loc
    while io_obj.loc-start < length and o.loc < o.len:
        header = read_unsigned_var_int(io_obj)
        if header & 1 == 0:
            read_rle(io_obj, header, width, o)
        else:
            read_bitpacked(io_obj, header, width, o)
    return o.so_far()


def read_length(file_obj):  # pragma: no cover
    """ Numpy trick to get a 32-bit length from four bytes

    Equivalent to struct.unpack('<i'), but suitable for numba-jit
    """
    sub = file_obj.read(4)
    return sub[0] + sub[1]*256 + sub[2]*256*256 + sub[3]*256*256*256


class NumpyIO(object):  # pragma: no cover
    """
    Read or write from a numpy arra like a file object

    This class is numba-jit-able (for specific dtypes)
    """
    def __init__(self, data):
        self.data = data
        self.len = data.size
        self.loc = 0

    def read(self, x):
        self.loc += x
        return self.data[self.loc-x:self.loc]

    def read_byte(self):
        self.loc += 1
        return self.data[self.loc-1]

    def write(self, d):
        l = len(d)
        self.loc += l
        self.data[self.loc-l:self.loc] = d

    def write_byte(self, b):
        if self.loc >= self.len:
            # ignore attempt to write past end of buffer
            return
        self.data[self.loc] = b
        self.loc += 1

    def write_many(self, b, count):
        self.data[self.loc:self.loc+count] = b
        self.loc += count

    def tell(self):
        return self.loc

    def so_far(self):
        """ In write mode, the data we have gathered until now
        """
        return self.data[:self.loc]

spec8 = [('data', np.uint8[:]), ('loc', np.int64), ('len', np.int64)]
Numpy8 = NumpyIO(spec8)
spec32 = [('data', np.uint32[:]), ('loc', np.int64), ('len', np.int64)]
Numpy32 = NumpyIO(spec32)
