# coding: utf-8
#
#    Project: X-ray image reader
#             https://github.com/silx-kit/fabio
#
#
#    Copyright (C) European Synchrotron Radiation Facility, Grenoble, France
#
#    Principal author:       Jérôme Kieffer (Jerome.Kieffer@ESRF.eu)
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
"""

Authors: Henning O. Sorensen & Erik Knudsen
         Center for Fundamental Research: Metal Structures in Four Dimensions
         Risoe National Laboratory
         Frederiksborgvej 399
         DK-4000 Roskilde
         email:erik.knudsen@risoe.dk

+ mods for fabio by JPW

"""

from __future__ import with_statement, print_function
import numpy
import logging

from .fabioimage import FabioImage
from .fabioutils import to_str

logger = logging.getLogger(__name__)


_DATA_TYPES = {
    "signed char": numpy.int8,
    "unsigned char": numpy.uint8,
    "short int": numpy.int16,
    "unsigned short int": numpy.uint16,
    "long int": numpy.int32,
    "unsigned long int": numpy.uint32,
    "float IEEE": numpy.float32,
    # Valid but unsupported
    "Compressed": None,
    # Valid but unsupported
    "Other_type": None,
}
"""Mapping from Data_type content to numpy equicalent"""


class DtrekImage(FabioImage):
    """Read an image using the d*TREK format.

    This format is used to process X-ray diffraction data from area detectors.
    It supports processing of data from multiple detector types (imaging plates,
    CCDs and pixel arrays) and from multiple vendors (Rigaku, Mar, Dectris,
    Bruker and ADSC).

    Rigaku providing a `specification <https://www.rigaku.com/downloads/software/free/dTREK%20Image%20Format%20v1.1.pdf>`_.
    """

    DESCRIPTION = "D*trek format (Rigaku specification 1.1)"

    DEFAULT_EXTENSIONS = ["img"]

    def __init__(self, *args, **kwargs):
        FabioImage.__init__(self, *args, **kwargs)

    def read(self, fname, frame=None):
        """ read in the file """
        with self._open(fname, "rb") as infile:
            try:
                self._readheader(infile)
            except Exception:
                logger.debug("Backtrace", exc_info=True)
                raise Exception("Error processing ADSC header")
            # banned by bzip/gzip???
            try:
                infile.seek(int(self.header['HEADER_BYTES']), 0)
            except TypeError:
                # Gzipped does not allow a seek and read header is not
                # promising to stop in the right place
                infile.close()
                infile = self._open(fname, "rb")
                infile.read(int(self.header['HEADER_BYTES']))
            binary = infile.read()

        # Read information of the binary data type
        data_type = self.header.get("Data_type", None)
        if data_type is None:
            # Compatibility with old supported files
            data_type = self.header.get("TYPE", None)
            if data_type is not None and data_type == "unsigned_short":
                pass
            else:
                logger.warning("Data_type key is expected. Fallback to unsigner integer 16-bits.")
            numpy_type = numpy.uint16
        else:
            if data_type not in _DATA_TYPES:
                raise IOError("Data_type key contains an invalid/unsupported value: %s", data_type)
            numpy_type = _DATA_TYPES[data_type]
            if type is None:
                raise IOError("Data_type %s is not supported by fabio", data_type)

        # Stored in case data reading fails
        self._dtype = numpy.dtype(numpy_type)
        self._shape = int(self.header['SIZE2']), int(self.header['SIZE1'])

        # Read the data into the array
        data = numpy.frombuffer(binary, numpy_type).copy()
        if self.swap_needed():
            data.byteswap(inplace=True)
        try:
            data.shape = self._shape
        except ValueError:
                raise IOError('Size spec in ADSC-header does not match ' +
                              'size of image data field %s != %s' % (self._shape, data.size))
        self.data = data
        self._shape = None
        self._dtype = None
        self.resetvals()
        return self

    def _readheader(self, infile):
        """ read an adsc header """
        line = infile.readline()
        bytesread = len(line)
        while not line.startswith(b'}'):
            if b'=' in line:
                (key, val) = to_str(line).split('=')
                self.header[key.strip()] = val.strip(' ;\n\r')
            line = infile.readline()
            bytesread = bytesread + len(line)

    def write(self, fname):
        """
        Write d*TREK format
        """

        # From specification
        HEADER_START = b"{\n"
        HEADER_END = b"}\n\x0C\n"
        HEADER_BYTES_TEMPLATE = b"HEADER_BYTES=% 5d;\n"
        # start + end + header_bytes_key + header_bytes_value + header_bytes_end
        MINIMAL_HEADER_SIZE = 2 + 4 + 13 + 5 + 2

        data = self.data

        dtrek_data_type = None
        for key, value in _DATA_TYPES.items():
            if data.dtype.type == value:
                dtrek_data_type = key
                break

        if dtrek_data_type is None:
            if data.dtype.kind == 'f':
                dtrek_data_type = "float IEEE"
            elif data.dtype.kind == 'u':
                dtrek_data_type = "unsigned long int"
            elif data.dtype.kind == 'i':
                dtrek_data_type = "long int"
            else:
                raise TypeError("Unsupported data type %s", data.dtype)
            new_dtype = numpy.dtype(_DATA_TYPES[dtrek_data_type])
            logger.warning("Data type %s unsupported. Store it as %s.", data.dtype, new_dtype)
            data = data.astype(new_dtype)

        if self.swap_needed():
            # Do not swap the data stored in the object
            data = data.byteswap()

        # Patch header to match the data
        self.header["Data_type"] = dtrek_data_type
        size2, size1 = data.shape
        self.header['SIZE1'] = str(size1)
        self.header['SIZE2'] = str(size2)

        out = b""
        for key in self.header:
            if key == "HEADER_BYTES":
                continue
            out += b"%s = %s;\n" % (key.encode("utf-8"), self.header[key].encode("utf-8"))

        # FIXME: This code do not take into account the size of "HEADER_BYTES"
        if "HEADER_BYTES" in self.header:
            hsize = int(self.header["HEADER_BYTES"])
            pad = hsize - len(out) - MINIMAL_HEADER_SIZE
            if pad < 0:
                logger.warning("HEADER_BYTES have to be patched.")
                minimal_hsize = hsize - pad
                hsize = (minimal_hsize + 512) & ~(512 - 1)
                pad = hsize - minimal_hsize
        else:
            minimal_hsize = len(out) + MINIMAL_HEADER_SIZE
            hsize = (minimal_hsize + 512) & ~(512 - 1)
            pad = hsize - minimal_hsize

        out = HEADER_START + (HEADER_BYTES_TEMPLATE % hsize) + out + HEADER_END + (b' ' * pad)
        assert len(out) % 512 == 0, "Header is not multiple of 512"

        with open(fname, "wb") as outf:
            outf.write(out)
            outf.write(data.tostring())

    def swap_needed(self):
        """
        Returns True if the header does not use the same endianness than the
        system.

        :rtype: bool
        """
        if "BYTE_ORDER" not in self.header:
            logger.warning("No byte order specified, assuming little_endian")
            byte_order = "little_endian"
        else:
            byte_order = self.header["BYTE_ORDER"]

        little_endian = "little" in byte_order
        return little_endian != numpy.little_endian
