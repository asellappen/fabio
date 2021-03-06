#!/usr/bin/env python
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
#  Permission is hereby granted, free of charge, to any person
#  obtaining a copy of this software and associated documentation files
#  (the "Software"), to deal in the Software without restriction,
#  including without limitation the rights to use, copy, modify, merge,
#  publish, distribute, sublicense, and/or sell copies of the Software,
#  and to permit persons to whom the Software is furnished to do so,
#  subject to the following conditions:
#
#  The above copyright notice and this permission notice shall be
#  included in all copies or substantial portions of the Software.
#
#  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
#  EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
#  OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
#  NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
#  HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
#  WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
#  FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
#  OTHER DEALINGS IN THE SOFTWARE.

"""Portable image converter based on FabIO library
to export Eiger frames (including te one from LImA)
to a set of esperanto frames which can be imported 
into CrysalisPro.
"""

__author__ = "Jerome Kieffer"
__copyright__ = "European Synchrotron Radiation Facility, Grenoble, France"
__licence__ = "MIT"
__date__ = "01/12/2020"
__status__ = "production"

FOOTER = """To import your files as a project:
* Start CrysalisPro and open any project
* press "F5" to open the console
* Type `esperanto createrunlist` and select your first and last frame
"""

import logging
logging.basicConfig()
logger = logging.getLogger("eiger2crysalis")
import codecs
import sys
import os
import glob
from itertools import takewhile
from .. import esperantoimage, eigerimage, limaimage
from ..openimage import openimage as fabio_open
from .._version import version as fabio_version
from ..nexus import get_isotime
import numpy
import argparse
try:
    import hdf5plugin
except ImportError:
    pass

try:
    import numexpr
except:
    logger.error("Numexpr is needed to interpret formula ...")

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_ARGUMENT_FAILURE = 2

try:
    from scipy import constants
except ImportError:
    # Updated with scipy 1.4
    CONST_hc = 12.398419843320026
else:
    CONST_hc = constants.c * constants.h / constants.e * 1e7


class ProgressBar:
    """
    Progress bar in shell mode
    """

    def __init__(self, title, max_value, bar_width):
        """
        Create a progress bar using a title, a maximum value and a graphical size.

        The display is done with stdout using carriage return to to hide the
        previous progress. It is not possible to use stdout for something else
        whill a progress bar is in use.

        The result looks like:

        .. code-block:: none

            Title [■■■■■■      ]  50%  Message

        :param str title: Title displayed before the progress bar
        :param float max_value: The maximum value of the progress bar
        :param int bar_width: Size of the progressbar in the screen
        """
        self.title = title
        self.max_value = max_value
        self.bar_width = bar_width
        self.last_size = 0
        self._message = ""
        self._value = 0.0

        encoding = None
        if hasattr(sys.stdout, "encoding"):
            # sys.stdout.encoding can't be used in unittest context with some
            # configurations of TestRunner. It does not exists in Python2
            # StringIO and is None in Python3 StringIO.
            encoding = sys.stdout.encoding
        if encoding is None:
            # We uses the safer aproch: a valid ASCII character.
            self.progress_char = '#'
        else:
            try:
                import datetime
                if str(datetime.datetime.now())[5:10] == "02-14":
                    self.progress_char = u'\u2665'
                else:
                    self.progress_char = u'\u25A0'
                _byte = codecs.encode(self.progress_char, encoding)
            except (ValueError, TypeError, LookupError):
                # In case the char is not supported by the encoding,
                # or if the encoding does not exists
                self.progress_char = '#'

    def clear(self):
        """
        Remove the progress bar from the display and move the cursor
        at the beginning of the line using carriage return.
        """
        sys.stdout.write('\r' + " " * self.last_size + "\r")
        sys.stdout.flush()

    def display(self):
        """
        Display the progress bar to stdout
        """
        self.update(self._value, self._message)

    def update(self, value, message="", max_value=None):
        """
        Update the progrss bar with the progress bar's current value.

        Set the progress bar's current value, compute the percentage
        of progress and update the screen with. Carriage return is used
        first and then the content of the progress bar. The cursor is
        at the begining of the line.

        :param float value: progress bar's current value
        :param str message: message displayed after the progress bar
        :param float max_value: If not none, update the maximum value of the
            progress bar
        """
        if max_value is not None:
            self.max_value = max_value
        self._message = message
        self._value = value

        if self.max_value == 0:
            coef = 1.0
        else:
            coef = (1.0 * value) / self.max_value
        percent = round(coef * 100)
        bar_position = int(coef * self.bar_width)
        if bar_position > self.bar_width:
            bar_position = self.bar_width

        # line to display
        line = '\r%15s [%s%s] % 3d%%  %s' % (self.title, self.progress_char * bar_position, ' ' * (self.bar_width - bar_position), percent, message)

        # trailing to mask the previous message
        line_size = len(line)
        clean_size = self.last_size - line_size
        if clean_size < 0:
            clean_size = 0
        self.last_size = line_size

        sys.stdout.write(line + " " * clean_size + "\r")
        sys.stdout.flush()


def expand_args(args):
    """
    Takes an argv and expand it (under Windows, cmd does not convert *.tif into
    a list of files.

    :param list args: list of files or wildcards
    :return: list of actual args
    """
    new = []
    for afile in args:
        if glob.has_magic(afile):
            new += glob.glob(afile)
        else:
            new.append(afile)
    return new


class Converter:

    def __init__(self, options):
        self.options = options
        self.mask = None
        if not self.options.verbose:
            self.progress = ProgressBar("HDF5 --> Esperanto", len(options.images), 30)
        self.succeeded = True
        
        prefix =  os.path.commonprefix([os.path.abspath(i) for i in self.options.images])
        if "{dirname}" in self.options.output: 
            self.dirname = os.path.dirname(prefix)
        else:
            self.dirname = os.path.dirname(os.path.abspath(self.options.output))
        if "{prefix}" in self.options.output:
            self.prefix = os.path.basename(prefix)
        else:
            self.prefix = os.path.basename(os.path.abspath(self.options.output)).split("{")[0]
        self.headers = None

    def geometry_transform(self, image):
        "Transforms an image according to the requested command line options"
        if self.options.rotation:
            image = numpy.rot90(image, k=self.options.rotation // 90)
        if self.options.transpose:
            image = image.T
        if self.options.flip_ud:
            image = numpy.flipud(image)
        if self.options.flip_lr:
            image = numpy.fliplr(image)
        return image

    def new_beam_center(self, x, y, shape):
        """Calculate the position of the beam after all transformations:
        
        :param x, y: position in the initial image
        :shape: shape of the input image
        :return: x, y, coordinated of the new beam center within the esperanto frame.
        """
        dummy = 123
        m = numpy.zeros(shape, dtype=numpy.int32)
        m[int(y + 0.5), int(x + 0.5)] = dummy

        f = esperantoimage.EsperantoImage(data=m)
        n = self.geometry_transform(f.data)
        w = numpy.argmin(abs(n.ravel() - dummy))
        return w % n.shape[-1], w // n.shape[-1]

    def convert_all(self):
        self.succeeded = True
        start_at = 0
        self.headers = self.common_headers()
        for filename in self.options.images:
            finish_at = self.convert_one(filename, start_at)
            self.succeeded = self.succeeded and (finish_at > 0)
            start_at += finish_at
    def finish(self):
        if not self.succeeded:
            if not self.options.verbose:
                self.progress.clear()
            print("Conversion or part of it failed. You can try with --debug to have more output information.")
            return EXIT_FAILURE
        else:
            if not self.options.verbose:
                self.progress.clear()
            print(FOOTER)
            return EXIT_SUCCESS

    def common_headers(self):
        headers = {
                    # SPECIAL_CCD_1
                    "delectronsperadu": 1,
                    "ldarkcorrectionswitch": 0,
                    "lfloodfieldcorrectionswitch/mode": 0,
                    "dsystemdcdb2gain": 1.0,
                    "ddarksignal": 0,
                    "dreadnoiserms": 0,
                    # SPECIAL_CCD_2
                    "ioverflowflag":0 ,
                    "ioverflowafterremeasureflag" :0,
                    "inumofdarkcurrentimages" :0,
                    "inumofmultipleimages" :0,
                    "loverflowthreshold": 1000000,
                    # SPECIAL_CCD_3
                    # SPECIAL_CCD_4
                    # SPECIAL_CCD_5
                    # TIME
                    # "dexposuretimeinsec": 0.2,
                    "doverflowtimeinsec": 0 ,
                    "doverflowfilter":0,
                    # MONITOR
                    # PIXELSIZE
                    # "drealpixelsizex": 0.075,
                    # "drealpixelsizey": 0.075,
                    "dsithicknessmmforpixeldetector": 1,
                # TIMESTAMP
                "timestampstring": get_isotime(),
                # GRIDPATTERN
                # STARTANGLESINDEG
    #             "dom_s":-180 + i,
    #             "dth_s":0,
    #             "dka_s":0,
    #             "dph_s":0,
                # ENDANGLESINDEG
    #             "dom_e":-179 + i,
    #             "dth_e": 0,
    #             "dka_e": 0,
    #             "dph_e": 0,
                # GONIOMODEL_1
                "dbeam2indeg":0,
                "dbeam3indeg":0,
                "detectorrotindeg_x":0,
                "detectorrotindeg_y":0,
                "detectorrotindeg_z":0,
    #             "dxorigininpix":  img.data.shape[1] - (img.data.shape[1] - data.shape[1]) / 2 - center_x,
    #             "dyorigininpix": img.data.shape[0] - center_y,
                "dalphaindeg": 50,
                "dbetaindeg": 0,
#                 "ddistanceinmm": 117,
                # GONIOMODEL_2
                # WAVELENGTH
                # "dalpha1": wl,
                # "dalpha2": wl,
                # "dalpha12": wl,
                # "dbeta1": wl,
                # MONOCHROMATOR
                "ddvalue-prepolfac": 0.98,
                "orientation-type": "SYNCHROTRON",
                # ABSTORUN
                }

        with fabio_open(self.options.images[0]) as source:
            shape = source.data.shape
            dtype = source.data.dtype
            if self.progress is not None:
                self.progress.max_value = source.nframes*len(self.options.images)
            if isinstance(source, limaimage.LimaImage):
                # Populate the Pilatus header from the Lima
                entry_name = source.h5.attrs.get("default")
                if entry_name:
                    entry = source.h5.get(entry_name)
                    if entry:
                        data_name = entry.attrs["default"]
                        if data_name:
                            data_grp = entry.get(data_name)
                            if data_grp:
                                nxdetector = data_grp.parent
                                try:
                                    headers["drealpixelsizex"] = nxdetector["detector_information/pixel_size/xsize"][()] * 1e3
                                    headers["drealpixelsizey"] = nxdetector["detector_information/pixel_size/ysize"][()] * 1e3
                                except Exception as e:
                                    logger.warning("Error in searching for pixel size (%s): %s", type(e), e)
                                try:
                                    t1 = nxdetector["acquisition/exposure_time"][()]
                                    headers["dexposuretimeinsec"] = t1
                                except Exception as e:
                                    logger.warning("Error in searching for exposure time (%s): %s", type(e), e)
            elif isinstance(source, eigerimage.EigerImage):
                raise NotImplementedError("Please implement Eiger detector data format parsing or at least open an issue")
            else:
                raise NotImplementedError("Unsupported format: %s" % source.__class__.__name__)
        if self.mask is None:
            self.mask = numpy.zeros(shape, dtype=dtype)
        # Parse option for headers
        if self.options.energy:
            wavelength = CONST_hc / self.options.energy
        elif self.options.wavelength:
            wavelength = self.options.wavelength
        headers["dalpha1"] = headers["dalpha2"] = headers["dalpha12"] = headers["dbeta1"] = wavelength
        if self.options.distance:
            headers["ddistanceinmm"] = self.options.distance
        if self.options.beam:
            x, y = self.options.beam
            x, y = self.new_beam_center(x, y, shape)
            headers["dxorigininpix"] = x
            headers["dyorigininpix"] = y
        if self.options.alpha:
            headers["dalphaindeg"] = self.options.alpha
        if self.options.kappa is not None:
            try:
                value = float(self.options.kappa)
            except ValueError:  # Handle the string
                value = numexpr.NumExpr(self.options.kappa)
            headers["dka_s"] = headers["dka_e"] = value
        if self.options.theta is not None:
            try:
                value = float(self.options.theta)
            except ValueError:  # Handle the string
                value = numexpr.NumExpr(self.options.theta)
            headers["dth_s"] = headers["dth_e"] = value
        if self.options.phi is not None:
            try:
                value = float(self.options.phi)
            except ValueError:  # Handle the string
                value = numexpr.NumExpr(self.options.phi)
            headers["dph_s"] = headers["dph_e"] = value
        if self.options.omega is not None:
            try:
                value = float(self.options.omega)
            except ValueError:
                # Handle the string
                value = numexpr.NumExpr(self.options.omega)
            headers["dom_s"] = headers["dom_e"] = value
        return headers

    def convert_one(self, input_filename, start_at=0):
        """
        Convert a single file using options
    
        :param str input_filename: The input filename
        :param object options: List of options provided from the command line
        :param start_at: index to start at for given file
        :rtype: int
        :returns: the number of frames processed
        """
        self.progress.update(start_at + 0.5, input_filename)
        input_filename = os.path.abspath(input_filename)
        input_exists = os.path.exists(input_filename)

        if self.options.verbose:
            print("Converting file '%s'" % (input_filename))

        if not input_exists:
            logger.error("Input file '%s' do not exists. Conversion skipped.", input_filename)
            return -1

        try:
            logger.debug("Load '%s'", input_filename)
            source = fabio_open(input_filename)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error("Loading input file '%s' failed cause: \"%s\". Conversion skipped.", input_filename, e.message)
            logger.debug("Backtrace", exc_info=True)
            return -1

        for i, frame in enumerate(source):
            idx = i + start_at
            self.progress.update(idx + 0.5, input_filename+" - "+str(idx))
            input_data = frame.data
            numpy.maximum(self.mask, input_data, out=self.mask)
            input_data = input_data.astype(numpy.int32)
            input_data[input_data == numpy.iinfo(frame.data.dtype).max] = self.options.dummy
            converted = esperantoimage.EsperantoImage(data=input_data)  # This changes the shape
            converted.data = self.geometry_transform(converted.data) 
            for k, v in self.headers.items():
                if callable(v):
                    if k.endswith("s"):
                        converted.header[k] = v(idx)
                    else:  # k.endswith("e"):
                        converted.header[k] = v(idx + 1)
                else:
                    converted.header[k] = v

            output_filename = self.options.output.format(index=((idx + self.options.offset)), 
                                                         prefix=self.prefix, 
                                                         dirname=self.dirname)
            os.makedirs(os.path.dirname(output_filename), exist_ok=True)
            try:
                logger.debug("Write '%s'", output_filename)
                if not self.options.dry_run:
                    converted.write(output_filename)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error("Saving output file '%s' failed cause: \"%s: %s\". Conversion skipped.", output_filename, type(e), e)
                logger.debug("Backtrace", exc_info=True)
                return -1
        return source.nframes

    def treat_mask(self):
        if self.progress:
            self.progress.update(self.progress.max_value-1, "Generate mask")
        try:
            from pyFAI.ext import dynamic_rectangle
        except ImportError:
            print("A recent version of pyFAI is needed to export the mask in a format compatible wit CrysalisPro")
        else:
            mask = self.mask == numpy.iinfo(self.mask.dtype).max
            esperantoimage.EsperantoImage.DUMMY=1
            new_mask = self.geometry_transform(esperantoimage.EsperantoImage(data=mask).data)
            rectangles =  dynamic_rectangle.decompose_mask(new_mask.astype(numpy.int8))
            self.progress.update(self.progress.max_value-0.5, f"Exporting {len(rectangles)} rectangles as mask")
            with open(os.path.join(self.dirname,self.prefix+".set"), mode="w") as maskfile:
                for r in rectangles:
                    if r.area == 1:
                        maskfile.write(f"CHIP BADPOINT {r.col} {r.row} IGNORE {r.col} {r.row} {r.col} {r.row}\r\n")
                    else:
                        maskfile.write(f"CHIP BADRECTANGLE {r.col} {r.col+r.width-1} {r.row} {r.row+r.height-1}\r\n")            
            
        
def main():

    epilog = """return codes: 0 means a success. 1 means the conversion
                contains a failure, 2 means there was an error in the
                arguments"""

    parser = argparse.ArgumentParser(prog="eiger2crysalis",
                                     description=__doc__,
                                     epilog=epilog)
    parser.add_argument("IMAGE", nargs="*",
                        help="File with input images")
    parser.add_argument("-V", "--version", action='version', version=fabio_version,
                        help="output version and exit")
    parser.add_argument("-v", "--verbose", action='store_true', dest="verbose", default=False,
                        help="show information for each conversions")
    parser.add_argument("--debug", action='store_true', dest="debug", default=False,
                        help="show debug information")
    group = parser.add_argument_group("main arguments")
    group.add_argument("-l", "--list", action="store_true", dest="list", default=None,
                       help="show the list of available formats and exit")
    group.add_argument("-o", "--output", default='{dirname}/{prefix}/{prefix}_1_{index}.esperanto', type=str,
                       help="output directory and filename template")
    group.add_argument("-O", "--offset", type=int, default=1,
                       help="index offset, CrysalisPro likes indexes to start at 1, Python starts at 0")
    group.add_argument("-D", "--dummy", type=int, default=-1,
                       help="Set masked values to this dummy value")

    group = parser.add_argument_group("optional behaviour arguments")
#     group.add_argument("-f", "--force", dest="force", action="store_true", default=False,
#                        help="if an existing destination file cannot be" +
#                        " opened, remove it and try again (this option" +
#                        " is ignored when the -n option is also used)")
#     group.add_argument("-n", "--no-clobber", dest="no_clobber", action="store_true", default=False,
#                        help="do not overwrite an existing file (this option" +
#                        " is ignored when the -i option is also used)")
#     group.add_argument("--remove-destination", dest="remove_destination", action="store_true", default=False,
#                        help="remove each existing destination file before" +
#                        " attempting to open it (contrast with --force)")
#     group.add_argument("-u", "--update", dest="update", action="store_true", default=False,
#                        help="copy only when the SOURCE file is newer" +
#                        " than the destination file or when the" +
#                        " destination file is missing")
#     group.add_argument("-i", "--interactive", dest="interactive", action="store_true", default=False,
#                        help="prompt before overwrite (overrides a previous -n" +
#                        " option)")
    group.add_argument("--dry-run", dest="dry_run", action="store_true", default=False,
                       help="do everything except modifying the file system")
    group.add_argument("--calc-mask", dest="calc_mask", type=bool, default=True,
                       help="Generate a mask from pixels marked as invalid, set to false to speed-up")

    group = parser.add_argument_group("Experimental setup options")
    group.add_argument("-e", "--energy", type=float, default=None,
                       help="Energy of the incident beam in keV")
    group.add_argument("-w", "--wavelength", type=float, default=None,
                       help="Wavelength of the incident beam in Å")
    group.add_argument("-d", "--distance", type=float, default=None,
                       help="Detector distance in millimeters")
    group.add_argument("-b", "--beam", nargs=2, type=float, default=None,
                       help="Direct beam in pixels x, y")

    group = parser.add_argument_group("Goniometer setup")
#     group.add_argument("--axis", type=str, default=None,
#                        help="Goniometer angle used for scanning: 'omega', 'phi' or 'kappa'")
    group.add_argument("--alpha", type=float, default=50,
                       help="Goniometer angle alpha value in deg. Constant, angle between kappa/omega.")
    group.add_argument("--kappa", type=str, default=0,
                       help="Goniometer angle kappa value in deg or formula f(index).")
#     group.add_argument("--chi", type=str, default=0,
#                        help="Goniometer angle chi value in deg. or formula f(index).")
    group.add_argument("--phi", type=str, default=0,
                       help="Goniometer angle phi value in deg. or formula f(index). Inner-most rotation.")
    group.add_argument("--omega", type=str, default=0,
                       help="Goniometer angle omega value in deg. or formula f(index). Outer-most rotation.")
    group.add_argument("--theta", type=str, default=0,
                       help="Goniometer angle theta value in deg. or formula f(index). Tilt angle of the detector.")

    group = parser.add_argument_group("Image preprocessing (Important: applied in this order!)")
    group.add_argument("--rotation", type=int, default=180,
                       help="Rotate the initial image by this value in degrees. Must be a multiple of 90°. By default 180 deg (flip_up with origin=lower and flip_lr because the image is seen from the sample).")
    group.add_argument("--transpose", default=False, action="store_true",
                       help="Flip the x/y axis")
    group.add_argument("--flip-ud", dest="flip_ud", default=False, action="store_true",
                       help="Flip the image upside-down")
    group.add_argument("--flip-lr", dest="flip_lr", default=False, action="store_true",
                       help="Flip the image left-right")

    try:
        args = parser.parse_args()

        if args.debug:
            logger.setLevel(logging.DEBUG)

        if args.list:
            print("Supported formats: LimaImage, EigerImage")
            return

        if len(args.IMAGE) == 0:
            raise argparse.ArgumentError(None, "No input file specified.")

        # the upper case IMAGE is used for the --help auto-documentation
        args.images = expand_args(args.IMAGE)
        args.images.sort()
    except argparse.ArgumentError as e:
        logger.error(e.message)
        logger.debug("Backtrace", exc_info=True)
        return EXIT_ARGUMENT_FAILURE
    esperantoimage.EsperantoImage.DUMMY = args.dummy

    converter = Converter(args)
    converter.convert_all()
    converter.treat_mask()
    return converter.finish()


if __name__ == "__main__":
    result = main()
    sys.exit(result)
