#!/usr/bin/env python

""" Low-level serial communications handling """

import asyncio
import sys, threading, logging

import re
import serial # pyserial: http://pyserial.sourceforge.net
import serial_asyncio

from .exceptions import TimeoutException
from . import compat # For Python 2.6 compatibility

class SerialComms(object):
    """ Wraps all low-level serial communications (actual read/write operations) """
    
    log = logging.getLogger('gsmmodem.serial_comms.SerialComms')
    
    # End-of-line read terminator
    RX_EOL_SEQ = b'\r\n'
    # End-of-response terminator
    RESPONSE_TERM = re.compile(b'^OK|ERROR|(\+CM[ES] ERROR: \d+)|(COMMAND NOT SUPPORT)$')
    # Default timeout for serial port reads (in seconds)
    timeout = 1
        
    def __init__(self, port, baudrate=115200, notifyCallbackFunc=None, fatalErrorCallbackFunc=None, *args, **kwargs):
        """ Constructor
         
        :param fatalErrorCallbackFunc: function to call if a fatal error occurs in the serial device reading thread
        :type fatalErrorCallbackFunc: func
        """     
        self.alive = False
        self.port = port
        self.baudrate = baudrate
        
        self._responseEvent = None # threading.Event()
        self._expectResponseTermSeq = None # expected response terminator sequence
        self._response = None # Buffer containing response to a written command
        self._notification = [] # Buffer containing lines from an unsolicited notification from the modem
        # Reentrant lock for managing concurrent write access to the underlying serial port
        self._txLock = threading.RLock()
        
        # self.notifyCallback = notifyCallbackFunc or self._placeholderCallback
        # self.fatalErrorCallback = fatalErrorCallbackFunc or self._placeholderCallback
        
        self.com_args = args
        self.com_kwargs = kwargs

        self.protocol = None
        self.reply_future = None

    def _placeholderCallback(self, *args, **kwargs):
        """ Placeholder callback function (does nothing) """

    async def connect(self):
        _, self.protocol = await serial_asyncio.create_serial_connection(
            asyncio.get_event_loop(),
            lambda: Output(self),
            self.port,
            baudrate=self.baudrate
        )

        self.alive = True
        self.connection_future = asyncio.Future()
        await self.connection_future

    async def close(self):
        print('closing...')
        if self.protocol:
            self.protocol.close()
        self.protocol = None
        self.alive = False

    async def write(self, data, waitForResponse=True, timeout=5, expectedResponseTermSeq=None):
        if isinstance(data, str):
            data = data.encode()

        if waitForResponse:
            if expectedResponseTermSeq:
                self._expectResponseTermSeq = bytearray(expectedResponseTermSeq)
            self._response = []
            self.reply_future = TimeoutFuture(timeout)
            self.protocol.send(data)
            try:
                return await self.reply_future
            except asyncio.CancelledError:
                self.reply_future = None
                self._expectResponseTermSeq = False
                if len(self._response) > 0:
                    # Add the partial response to the timeout exception
                    raise TimeoutException(self._response)
                else:
                    raise TimeoutException()
        else:
            self.protocol.send(data)

    async def connected(self):
        print('hi')
        # raise NotImplementedError

    async def data(self, data):
        # print('##### got data {}'.format(data))
        if self.reply_future and not self.reply_future.done():
            # A response event has been set up (another thread is waiting for this response)
            last_line = None
            seen_expected = False
            for line in data.split(b'\r\n'):
                if line:
                    self._response.append(line)
                    last_line = line
                    if self._expectResponseTermSeq == line:
                        seen_expected = True

            if seen_expected or self.RESPONSE_TERM.match(last_line):
                # End of response reached; notify waiting thread
                self.log.debug('response: %s', self._response)
                if self.reply_future and not self.reply_future.done():
                    self.reply_future.set_result(self._response)
        else:
            # Nothing was waiting for this - treat it as a notification
            for line in data.split(b'\r\n'):
                if line:
                    self._notification.append(line)
            # if self.serial.inWaiting() == 0:
            # No more chars on the way for this notification - notify higher-level callback
            #print 'notification:', self._notification
            self.log.debug('notification: %s', self._notification)
            self.notifyCallback(self._notification)
            self._notification = []


class Output(asyncio.Protocol):
    def __init__(self, connector):
        super().__init__()
        self.connector = connector
        self.transport = None
        self.connection_lost_future = asyncio.Future()

    def connection_made(self, transport):
        print('port opened', transport)
        super().connection_made(transport)
        self.transport = transport
        if self.connection_lost_future.done():
            self.connection_lost_future = asyncio.Future()

        transport.serial.rts = False  # You can manipulate Serial object via transport
        asyncio.ensure_future(self.connector.connected())

    def data_received(self, data):
        print('data received', repr(data))
        asyncio.ensure_future(self.connector.data(data))
        # if b'\n' in data:
        #     self.transport.close()

    def connection_lost(self, exc):
        print('port closed')
        super().connection_lost(exc)
        self.transport = None
        if not self.connection_lost_future.done():
            self.connection_lost_future.set_result(None)

    def pause_writing(self):
        print('pause writing')
        print(self.transport.get_write_buffer_size())

    def resume_writing(self):
        print(self.transport.get_write_buffer_size())
        print('resume writing')

    def send(self, data):
        self.transport.write(data)

    def close(self):
        if self.transport:
            self.transport.close()
            self.transport = None

    async def wait_disconnect(self):
        await self.connection_lost_future


class TimeoutFuture(asyncio.Future):
    def __init__(self, timeout):
        super().__init__()
        if timeout is not None:
            asyncio.get_event_loop().call_later(timeout, self.cancel)