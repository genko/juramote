# Copyright 2017 juramote contributors (see README)
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import serial, time, sys, logging, argparse, json, codecs
from functools import wraps
from enum import IntEnum, Enum
from datetime import datetime, timedelta
from threading import Lock
from collections import namedtuple

log = logging.getLogger(__name__)

class Raw:
    """
    Raw access to Jura coffee maker, no error-checking, minimal decoding
    """

    EEPROM_WORDLENGTH = 2 # bytes
    EEPROM_LINELENGTH = 32 # bytes (decoded)
    EEPROM_LINES = 64

    def __init__ (self, tty):
        # XXX: auto-detect machine type
        self.s = serial.Serial (tty, 9600, timeout=30)
        self._test ()
        self.machine = ImpressaXs90

    def _test (self):
        """
        Simple self-test
        """
        for b in range (256):
            orig = bytes ([b])
            enc = self._encodebyte (orig)
            dec = self._decodebyte (enc)
            assert dec == orig, (orig, enc, dec)
        assert self._decode (self._encode (b'RE:1234')) == b'RE:1234'

    @staticmethod
    def _encodebyte (c):
        """
        Encode a single byte to Jura coding, i.e. stretched to 4 bytes with
        one bit of information distributed to 2nd and 5th output bit each.
        """
        assert len (c) == 1
        c = c[0]
        out = [0xdb]*4
        for i in range (4):
            out[i] |= ((c>>(i*2))&1)<<2
            out[i] |= ((c>>(i*2+1))&1)<<5

        return bytes (out)

    @classmethod
    def _encode (cls, s):
        """
        Encode byte string to Jura coding
        """
        return list (map (lambda x: cls._encodebyte (bytes ([x])), s))

    @staticmethod
    def _decodebyte (b):
        """
        Decode byte received from Jura machine
        """
        assert len (b) == 4

        out = 0
        shift = 0
        for i in range (4):
            out |= ((b[i]>>2)&1)<<shift
            shift += 1
            out |= ((b[i]>>5)&1)<<shift
            shift += 1
        assert shift == 8

        return bytes ([out])

    @classmethod
    def _decode (cls, s):
        return b''.join (map (cls._decodebyte, s))

    def _send (self, command):
        """
        Send single command
        """
        # if commands “time out” there may be a late anwer left behind in the
        # buffers
        self.s.reset_input_buffer ()
        self.s.reset_output_buffer ()

        log.debug ('← {}'.format (command))
        command += b'\r\n'
        enc = self._encode (command)
        for b in enc:
            self.s.write (b)

    def _receive (self):
        """
        Receive single command response
        """
        s = b''
        while True:
            b = self.s.read (4)
            if len (b) != 4:
                raise ValueError ('response too small/timeout')
            s += self._decodebyte (b)
            if s.endswith (b'\r\n'):
                break
        log.debug ('→ {}'.format (s))
        return s.rstrip (b'\r\n')

    def _receiveInt (self, expected):
        """
        Receive hex-encoded integer
        """
        l = self._receive ()
        if not l.startswith (expected):
            raise ValueError ('invalid response')
        # response is big endian, so we are fine
        return int (l[len (expected):], 16)

    def _receiveBool (self):
        """
        Receive boolean response.

        Right now only ok: is recognized. Not sure if there actually is an error response…
        """
        l = self._receive ()
        return l == b'ok:'

    def _receiveBytes (self, expected):
        """
        Receive hex-encoded raw bytes
        """
        l = self._receive ()
        if not l.startswith (expected):
            raise ValueError ('invalid response')
        return codecs.decode (l[len (expected):], 'hex')

    def _receiveString (self, expected):
        """
        Receive latin1 string
        """
        l = self._receive ()
        if not l.startswith (expected):
            raise ValueError ('invalid response')
        return l[len (expected):].decode ('latin1')

    def readEeprom (self, address):
        """
        Read a single word from EEPROM.

        :param address: eeprom *word* address. Words are 16 bit. I.e. 0 ->
            first word, 1 -> second word, …
        """
        self._send ('RE:{:04X}'.format (address).encode ('ascii'))
        return self._receiveInt (b're:')

    def writeEeprom (self, address, value):
        """
        Write a single word into EEPROM.

        :param address: See readEeprom
        :param value: The value
        """
        self._send ('WE:{:04X},{:04X}'.format (address, value).encode ('ascii'))
        return self._receiveBool ()

    def readEepromLine (self, address):
        """
        Read 32 bytes from EEPROM.

        :param address: eeprom *word* start address. Can be any offset. Words are 16 bit.
        """
        self._send ('RT:{:04X}'.format (address).encode ('ascii'))
        return self._receiveBytes (b'rt:')

    def readInput (self):
        self._send (b'IC:')
        return self._receiveInt (b'ic:')

    def pressButton (self, i):
        """
        Press any button on the machine. Second press to abort item in progress
        works.
        """
        self._send ('FA:{:02X}'.format (i).encode ('ascii'))
        return self._receiveBool ()

    def makeComponent (self, i):
        self._send ('FN:{:02X}'.format (i).encode ('ascii'))
        return self._receiveBool ()

    def getType (self):
        """
        Get machine type
        """
        self._send (b'TY:')
        return self._receiveString (b'ty:')

    def getLoader (self):
        """
        Get bootloader(?) version string
        """
        self._send (b'TL:')
        return self._receiveString (b'tl:')

    def getHeaterSensors (self):
        """
        Get heater and brewing sensor/status information
        """
        self._send (b'HZ:')
        v = self._receiveString (b'hz:').split (',')
        for i in (0, 7, 9):
            v[i] = int (v[i], 2)
        for i in list (range (1, 7)) + [8]:
            v[i] = int (v[i], 16)
        return v

    def resetDisplay (self):
        """
        Reset display to default
        """
        self._send (b'DR:')
        return self._receiveBool ()

    def printDisplay (self, s):
        """
        Permanently display a message.

        Display supports ASCII and subset of latin1 (german umlauts)
        """
        self._send ('DA:{}'.format (s).encode ('latin1'))
        return self._receiveBool ()

    def printDisplayDefault (self, s):
        """
        Change the default selection message
        """
        self._send ('DT:{}'.format (s).encode ('latin1'))
        return self._receiveBool ()

    def raw (self, cmd):
        """
        Send raw command
        """
        self._send (cmd.encode ('latin1'))
        return self._receive ().decode ('latin1')

class State (Enum):
    """
    Current machine state
    """
    IDLE = 0
    GRINDING = 1
    BREWING = 2
    FOAMING = 3
    UNKNOWN = 99

MachineState = namedtuple ('MachineState', ['state', 'flow', 'coffeetemp', 'milktemp'])
