#!/usr/bin/env python

""" High-level API classes for an attached GSM modem """

import abc
import asyncio
import logging
import re
import sys
import threading
import time
import weakref
from enum import Enum
import traceback

from gsmmodem.exceptions import EncodingError
# from . import compat # For Python 2.6 compatibility
from gsmmodem.util import lineMatching
from .exceptions import CommandError, InvalidStateException, CmeError, CmsError, InterruptedException, TimeoutException, \
    PinRequiredError, SmscNumberUnknownError
from .pdu import encodeSmsSubmitPdu, decodeSmsPdu, encodeGsm7
from .serial_comms import SerialComms
from .util import lineStartingWith, parseTextModeTimeStr

PYTHON_VERSION = sys.version_info[0]

CTRLZ = chr(26)
TERMINATOR = '\r'

def d(obj):
    return obj.decode() if type(obj) == bytes else obj

if PYTHON_VERSION >= 3:
    xrange = range
    dictValuesIter = dict.values
    dictItemsIter = dict.items
    unicode = str
    TERMINATOR = b'\r'
    CTRLZ = b'\x1a'


else: #pragma: no cover
    dictValuesIter = dict.itervalues
    dictItemsIter = dict.iteritems


class Sms(object):
    """ Abstract SMS message base class """
    __metaclass__ = abc.ABCMeta

    # Some constants to ease handling SMS statuses
    STATUS_RECEIVED_UNREAD = 0
    STATUS_RECEIVED_READ = 1
    STATUS_STORED_UNSENT = 2
    STATUS_STORED_SENT = 3
    STATUS_ALL = 4
    # ...and a handy converter for text mode statuses
    TEXT_MODE_STATUS_MAP = {'REC UNREAD': STATUS_RECEIVED_UNREAD,
                            'REC READ': STATUS_RECEIVED_READ,
                            'STO UNSENT': STATUS_STORED_UNSENT,
                            'STO SENT': STATUS_STORED_SENT,
                            'ALL': STATUS_ALL}

    def __init__(self, number, text, smsc=None):
        self.number = number
        self.text = text
        self.smsc = smsc


class ReceivedSms(Sms):
    """ An SMS message that has been received (MT) """

    def __init__(self, gsmModem, status, number, time, text, smsc=None, udh=[]):
        super(ReceivedSms, self).__init__(number, text, smsc)
        self._gsmModem = weakref.proxy(gsmModem)
        self.status = status
        self.time = time
        self.udh = udh

    def reply(self, message):
        """ Convenience method that sends a reply SMS to the sender of this message """
        return self._gsmModem.sendSms(self.number, message)

    def sendSms(self, dnumber, message):
        """ Convenience method that sends a SMS to someone else """
        return self._gsmModem.sendSms(dnumber, message)

    def getModem(self):
        """ Convenience method that returns the gsm modem instance """
        return self._gsmModem

class SentSms(Sms):
    """ An SMS message that has been sent (MO) """

    ENROUTE = 0 # Status indicating message is still enroute to destination
    DELIVERED = 1 # Status indicating message has been received by destination handset
    FAILED = 2 # Status indicating message delivery has failed

    def __init__(self, number, text, reference, smsc=None):
        super(SentSms, self).__init__(number, text, smsc)
        self.report = None # Status report for this SMS (StatusReport object)
        self.reference = reference

    @property
    def status(self):
        """ Status of this SMS. Can be ENROUTE, DELIVERED or FAILED

        The actual status report object may be accessed via the 'report' attribute
        if status is 'DELIVERED' or 'FAILED'
        """
        if self.report == None:
            return SentSms.ENROUTE
        else:
            return SentSms.DELIVERED if self.report.deliveryStatus == StatusReport.DELIVERED else SentSms.FAILED


class StatusReport(Sms):
    """ An SMS status/delivery report

    Note: the 'status' attribute of this class refers to this status report SM's status (whether
    it has been read, etc). To find the status of the message that caused this status report,
    use the 'deliveryStatus' attribute.
    """

    DELIVERED = 0 # SMS delivery status: delivery successful
    FAILED = 68 # SMS delivery status: delivery failed

    def __init__(self, gsmModem, status, reference, number, timeSent, timeFinalized, deliveryStatus, smsc=None):
        super(StatusReport, self).__init__(number, None, smsc)
        self._gsmModem = weakref.proxy(gsmModem)
        self.status = status
        self.reference = reference
        self.timeSent = timeSent
        self.timeFinalized = timeFinalized
        self.deliveryStatus = deliveryStatus


class GsmModem(SerialComms):
    """ Main class for interacting with an attached GSM modem """

    log = logging.getLogger('gsmmodem.modem.GsmModem')

    # Used for parsing AT command errors
    CM_ERROR_REGEX = re.compile(b'^\+(CM[ES]) ERROR: (\d+)$')
    # Used for parsing signal strength query responses
    CSQ_REGEX = re.compile(b'^\+CSQ:\s*(\d+),')
    # Used for parsing caller ID announcements for incoming calls. Group 1 is the number
    CLIP_REGEX = re.compile(b'^\+CLIP:\s*"(\+{0,1}\d+)",(\d+).*$')
    # Used for parsing own number. Group 1 is the number
    CNUM_REGEX = re.compile(b'^\+CNUM:\s*".*?","(\+{0,1}\d+)",(\d+).*$')
    # Used for parsing new SMS message indications
    CMTI_REGEX = re.compile(b'^\+CMTI:\s*"([^"]+)",\s*(\d+)$')
    # Used for parsing SMS message reads (text mode)
    CMGR_SM_DELIVER_REGEX_TEXT = None
    # Used for parsing SMS status report message reads (text mode)
    CMGR_SM_REPORT_REGEXT_TEXT = None
    # Used for parsing SMS message reads (PDU mode)
    CMGR_REGEX_PDU = None
    # Used for parsing USSD event notifications
    CUSD_REGEX = re.compile(b'\+CUSD:\s*(\d),\s*"(.*?)",\s*(\d+)', re.DOTALL)
    # Used for parsing SMS status reports
    CDSI_REGEX = re.compile(b'\+CDSI:\s*"([^"]+)",(\d+)$')
    CDS_REGEX  = re.compile(b'\+CDS:\s*([0-9]+)$')

    def __init__(self, port, baudrate=115200, incomingCallCallbackFunc=None, smsReceivedCallbackFunc=None, smsStatusReportCallback=None, requestDelivery=True, AT_CNMI="", *a, **kw):
        super(GsmModem, self).__init__(port, baudrate, notifyCallbackFunc=self._handleModemNotification, *a, **kw)
        self.incomingCallCallback = incomingCallCallbackFunc or self._placeholderCallback
        self.smsReceivedCallback = smsReceivedCallbackFunc or self._placeholderCallback
        self.smsStatusReportCallback = smsStatusReportCallback or self._placeholderCallback
        self.requestDelivery = requestDelivery
        self.AT_CNMI = AT_CNMI or "2,1,0,2"
        # Flag indicating whether caller ID for incoming call notification has been set up
        self._callingLineIdentification = False
        # Flag indicating whether incoming call notifications have extended information
        self._extendedIncomingCallIndication = False
        # Current active calls (ringing and/or answered), key is the unique call ID (not the remote number)
        self.activeCalls = {}
        # Dict containing sent SMS messages (for auto-tracking their delivery status)
        self.sentSms = weakref.WeakValueDictionary()
        self._ussdSessionFuture = None # asyncio.Future
        self._smsStatusReportEvent = None # threading.Event
        self._dialEvent = None # threading.Event
        self._dialResponse = None # gsmmodem.modem.Call
        self._waitForAtdResponse = True # Flag that controls if we should wait for an immediate response to ATD, or not
        self._waitForCallInitUpdate = True # Flag that controls if we should wait for a ATD "call initiated" message
        self._callStatusUpdates = [] # populated during connect() - contains regexes and handlers for detecting/handling call status updates
        self._mustPollCallStatus = False # whether or not the modem must be polled for outgoing call status updates
        self._pollCallStatusRegex = None # Regular expression used when polling outgoing call status
        self._writeWait = 0 # Time (in seconds to wait after writing a command (adjusted when 515 errors are detected)
        self._smsTextMode = False # Storage variable for the smsTextMode property
        self._gsmBusy = 0 # Storage variable for the GSMBUSY property
        self._smscNumber = None # Default SMSC number
        self._smsRef = 0 # Sent SMS reference counter
        self._smsMemReadDelete = None # Preferred message storage memory for reads/deletes (<mem1> parameter used for +CPMS)
        self._smsMemWrite = None # Preferred message storage memory for writes (<mem2> parameter used for +CPMS)
        self._smsReadSupported = True # Whether or not reading SMS messages is supported via AT commands
        self._smsEncoding = 'GSM' # Default SMS encoding
        self._smsSupportedEncodingNames = None # List of available encoding names
        self._commands = None # List of supported AT commands
        #Pool of detected DTMF
        self.dtmfpool = []
        self.pin = None
        self.last_command = None
        # self.response = []
        self.loop = asyncio.get_event_loop()

        self.read_write_lock = asyncio.Lock()

    def _placeHolderCallback(self, *args):
        """ Does nothing """
        self.log.debug('called with args: {0}'.format(args))

    async def connected(self):
        # Send some initialization commands to the modem
        try:
            await self.write('ATZ')
        except CommandError:
            # Some modems require a SIM PIN at this stage already; unlock it now
            # Attempt to enable detailed error messages (to catch incorrect PIN error)
            # but ignore if it fails
            await self.write('AT+CMEE=1', parseError=False)
            await self._unlockSim(self.pin)
            pinCheckComplete = True
            await self.write('ATZ')  # reset configuration
        else:
            pinCheckComplete = False
        await self.write('ATE0')  # echo off
        try:
            cfun = int(lineStartingWith(b'+CFUN:', await self.write('AT+CFUN?'))[7:])  # example response: +CFUN: 1
            if cfun != 1:
                await self.write('AT+CFUN=1')
        except CommandError:
            pass  # just ignore if the +CFUN command isn't supported

        await self.write('AT+CMEE=1')  # enable detailed error messages (even if it has already been set - ATZ may reset this)
        if not pinCheckComplete:
            await self._unlockSim(self.pin)

        # Get list of supported commands from modem
        commands = await self.get_supportedCommands()
        self._commands = commands

        # Device-specific settings
        callUpdateTableHint = 0  # unknown modem
        enableWind = False
        if commands != None:
            if b'^CVOICE' in commands:
                await self.write('AT^CVOICE=0', parseError=False)  # Enable voice calls
            if b'+VTS' in commands:  # Check for DTMF sending support
                Call.dtmfSupport = True
            elif b'^DTMF' in commands:
                # Huawei modems use ^DTMF to send DTMF tones
                callUpdateTableHint = 1  # Huawei
            if b'^USSDMODE' in commands:
                # Enable Huawei text-mode USSD
                await self.write('AT^USSDMODE=0', parseError=False)
            if b'+WIND' in commands:
                callUpdateTableHint = 2  # Wavecom
                enableWind = True
            elif b'+ZPAS' in commands:
                callUpdateTableHint = 3  # ZTE
        else:
            # Try to enable general notifications on Wavecom-like device
            enableWind = True

        if enableWind:
            try:
                wind = lineStartingWith(b'+WIND:',
                                        await self.write('AT+WIND?'))  # Check current WIND value; example response: +WIND: 63
            except CommandError:
                # Modem does not support +WIND notifications. See if we can detect other known call update notifications
                pass
            else:
                # Enable notifications for call setup, hangup, etc
                if int(wind[7:]) != 50:
                    await self.write('AT+WIND=50')
                callUpdateTableHint = 2  # Wavecom

        # Attempt to identify modem type directly (if not already) - for outgoing call status updates
        if callUpdateTableHint == 0:
            if b'simcom' in (await self.manufacturer).lower():  # simcom modems support DTMF and don't support AT+CLAC
                Call.dtmfSupport = True
                await self.write('AT+DDET=1')  # enable detect incoming DTMF

            if (await self.manufacturer).lower() == 'huawei':
                callUpdateTableHint = 1  # huawei
            else:
                # See if this is a ZTE modem that has not yet been identified based on supported commands
                try:
                    await self.write('AT+ZPAS?')
                except CommandError:
                    pass  # Not a ZTE modem
                else:
                    callUpdateTableHint = 3  # ZTE
        # Load outgoing call status updates based on identified modem features
        if callUpdateTableHint == 1:
            # Use Hauwei's ^NOTIFICATIONs
            self.log.info('Loading Huawei call state update table')
            self._callStatusUpdates = ((re.compile(b'^\^ORIG:(\d),(\d)$'), self._handleCallInitiated),
                                       (re.compile(b'^\^CONN:(\d),(\d)$'), self._handleCallAnswered),
                                       (re.compile(b'^\^CEND:(\d),(\d),(\d)+,(\d)+$'), self._handleCallEnded))
            self._mustPollCallStatus = False
            # Huawei modems use ^DTMF to send DTMF tones; use that instead
            Call.DTMF_COMMAND_BASE = '^DTMF={cid},'
            Call.dtmfSupport = True
        elif callUpdateTableHint == 2:
            # Wavecom modem: +WIND notifications supported
            self.log.info('Loading Wavecom call state update table')
            self._callStatusUpdates = ((re.compile(b'^\+WIND: 5,(\d)$'), self._handleCallInitiated),
                                       (re.compile(b'^OK$'), self._handleCallAnswered),
                                       (re.compile(b'^\+WIND: 6,(\d)$'), self._handleCallEnded))
            self._waitForAtdResponse = False  # Wavecom modems return OK only when the call is answered
            self._mustPollCallStatus = False
            if commands == None:  # older modem, assume it has standard DTMF support
                Call.dtmfSupport = True
        elif callUpdateTableHint == 3:  # ZTE
            # Use ZTE notifications ("CONNECT"/"HANGUP", but no "call initiated" notification)
            self.log.info('Loading ZTE call state update table')
            self._callStatusUpdates = ((re.compile(b'^CONNECT$'), self._handleCallAnswered),
                                       (re.compile(b'^HANGUP:\s*(\d+)$'), self._handleCallEnded),
                                       (re.compile(b'^OK$'), self._handleCallRejected))
            self._waitForAtdResponse = False  # ZTE modems do not return an immediate  OK only when the call is answered
            self._mustPollCallStatus = False
            self._waitForCallInitUpdate = False  # ZTE modems do not provide "call initiated" updates
            if commands == None:  # ZTE uses standard +VTS for DTMF
                Call.dtmfSupport = True
        else:
            # Unknown modem - we do not know what its call updates look like. Use polling instead
            self.log.info('Unknown/generic modem type - will use polling for call state updates')
            self._mustPollCallStatus = True
            self._pollCallStatusRegex = re.compile(b'^\+CLCC:\s+(\d+),(\d),(\d),(\d),([^,]),"([^,]*)",(\d+)$')
            self._waitForAtdResponse = True  # Most modems return OK immediately after issuing ATD

        # General meta-information setup
        await self.write('AT+COPS=3,0', parseError=False)  # Use long alphanumeric name format

        # SMS setup
        await self.write('AT+CMGF={0}'.format(1 if self._smsTextMode else 0))  # Switch to text or PDU mode for SMS messages
        self._compileSmsRegexes()
        if self._smscNumber != None:
            await self.write('AT+CSCA="{0}"'.format(d(self._smscNumber)))  # Set default SMSC number
            currentSmscNumber = self._smscNumber
        else:
            currentSmscNumber = await self.smsc
        # Some modems delete the SMSC number when setting text-mode SMS parameters; preserve it if needed
        if currentSmscNumber != None:
            self._smscNumber = None  # clear cache
        if self.requestDelivery:
            await self.write('AT+CSMP=49,167,0,0', parseError=False)  # Enable delivery reports
        else:
            await self.write('AT+CSMP=17,167,0,0', parseError=False)  # Not enable delivery reports
        # ...check SMSC again to ensure it did not change
        if currentSmscNumber != None and (await self.smsc) != currentSmscNumber:
            await self.set_smsc(currentSmscNumber)

        # Set message storage, but first check what the modem supports - example response: +CPMS: (("SM","BM","SR"),("SM"))
        try:
            cpmsLine = lineStartingWith(b'+CPMS', await self.write('AT+CPMS=?'))
        except CommandError:
            # Modem does not support AT+CPMS; SMS reading unavailable
            self._smsReadSupported = False
            self.log.warning('SMS preferred message storage query not supported by modem. SMS reading unavailable.')
        else:
            cpmsSupport = cpmsLine.split(b' ', 1)[1].split(b'),(')
            # Do a sanity check on the memory types returned - Nokia S60 devices return empty strings, for example
            for memItem in cpmsSupport:
                if len(memItem) == 0:
                    # No support for reading stored SMS via AT commands - probably a Nokia S60
                    self._smsReadSupported = False
                    self.log.warning(
                        'Invalid SMS message storage support returned by modem. SMS reading unavailable. Response was: "%s"',
                        cpmsLine)
                    break
            else:
                # Suppported memory types look fine, continue
                preferredMemoryTypes = (b'"SM"', b'"SM"', b'"SM"')
                cpmsItems = [''] * len(cpmsSupport)
                for i in xrange(len(cpmsSupport)):
                    for memType in preferredMemoryTypes:
                        if memType in cpmsSupport[i]:
                            if i == 0:
                                self._smsMemReadDelete = memType
                            cpmsItems[i] = memType.decode('ascii')
                            break
                await self.write('AT+CPMS={0}'.format(','.join(cpmsItems)))  # Set message storage
            del cpmsSupport
            del cpmsLine

        if self._smsReadSupported and (self.smsReceivedCallback or self.smsStatusReportCallback):
            try:
                await self.write('AT+CNMI=' + self.AT_CNMI)  # Set message notifications
            except CommandError:
                try:
                    await self.write('AT+CNMI=2,1,0,1,0')  # Set message notifications, using TE for delivery reports <ds>
                except CommandError:
                    # Message notifications not supported
                    self._smsReadSupported = False
                    self.log.warning('Incoming SMS notifications not supported by modem. SMS receiving unavailable.')

        # Incoming call notification setup
        try:
            await self.write('AT+CLIP=1')  # Enable calling line identification presentation
        except CommandError as clipError:
            self._callingLineIdentification = False
            self.log.warning(
                'Incoming call calling line identification (caller ID) not supported by modem. Error: {0}'.format(
                    clipError))
        else:
            self._callingLineIdentification = True
            try:
                await self.write('AT+CRC=1')  # Enable extended format of incoming indication (optional)
            except CommandError as crcError:
                self._extendedIncomingCallIndication = False
                self.log.warning(
                    'Extended format incoming call indication not supported by modem. Error: {0}'.format(crcError))
            else:
                self._extendedIncomingCallIndication = True

        # Call control setup
        await self.write('AT+CVHU=0',
                   parseError=False)  # Enable call hang-up with ATH command (ignore if command not supported)

        self.connection_future.set_result(None)

    def process_supported_commands(self, response):
        if len(response) == 2:  # Single-line response, comma separated
            commands = response[0]
            if commands.startswith(b'+CLAC'):
                commands = commands[6:]  # remove the +CLAC: prefix before splitting
            return commands.split(',')
        elif len(response) > 2:  # Multi-line response
            return [cmd.strip() for cmd in response[:-1]]
        else:
            self.log.debug('Unhandled +CLAC response: {0}'.format(response))
            return None


    async def connect(self, pin=None, waitingForModemToStartInSeconds=0):
        """ Opens the port and initializes the modem and SIM card

        :param pin: The SIM card PIN code, if any
        :type pin: str

        :raise PinRequiredError: if the SIM card requires a PIN but none was provided
        :raise IncorrectPinError: if the specified PIN is incorrect
        """
        self.pin = pin
        self.log.info('Connecting to modem on port %s at %dbps', self.port, self.baudrate)
        await super(GsmModem, self).connect()

        # if waitingForModemToStartInSeconds > 0:
        #     while waitingForModemToStartInSeconds > 0:
        #         try:
        #             await self.write('AT', waitForResponse=True, timeout=0.5)
        #             break
        #         except TimeoutException:
        #             waitingForModemToStartInSeconds -= 0.5

    async def _unlockSim(self, pin):
        """ Unlocks the SIM card using the specified PIN (if necessary, else does nothing) """
        # Unlock the SIM card if needed
        try:
            cpinResponse = lineStartingWith(b'+CPIN', await self.write('AT+CPIN?', timeout=15))
        except TimeoutException as timeout:
            # Wavecom modems do not end +CPIN responses with "OK" (github issue #19) - see if just the +CPIN response was returned
            if timeout.data != None:
                cpinResponse = lineStartingWith(b'+CPIN', timeout.data)
                if cpinResponse == None:
                    # No useful response read
                    raise timeout
            else:
                # Nothing read (real timeout)
                raise timeout
        if cpinResponse != b'+CPIN: READY':
            if pin != None:
                await self.write('AT+CPIN="{0}"'.format(d(pin)))
            else:
                raise PinRequiredError('AT+CPIN')

    def cancel(self):
        if self.reply_future and not self.reply_future.done():
            self.reply_future.cancel()

    async def write(self, data, waitForResponse=True, timeout=10, parseError=True, writeTerm=TERMINATOR, expectedResponseTermSeq=None):
        """ Write data to the modem.

        This method adds the ``\\r\\n`` end-of-line sequence to the data parameter, and
        writes it to the modem.

        :param data: Command/data to be written to the modem
        :type data: str
        :param waitForResponse: Whether this method should block and return the response from the modem or not
        :type waitForResponse: bool
        :param timeout: Maximum amount of time in seconds to wait for a response from the modem
        :type timeout: int
        :param parseError: If True, a CommandError is raised if the modem responds with an error (otherwise the response is returned as-is)
        :type parseError: bool
        :param writeTerm: The terminating sequence to append to the written data
        :type writeTerm: str
        :param expectedResponseTermSeq: The expected terminating sequence that marks the end of the modem's response (defaults to ``\\r\\n``)
        :type expectedResponseTermSeq: str

        :raise CommandError: if the command returns an error (only if parseError parameter is True)
        :raise TimeoutException: if no response to the command was received from the modem

        :return: A list containing the response lines from the modem, or None if waitForResponse is False
        :rtype: list
        """
        if isinstance(data, unicode):
            data = bytes(data,"ascii")

        self.log.debug('write: %s', data)
        responseLines = await super(GsmModem, self).write(data + writeTerm, waitForResponse=waitForResponse, timeout=timeout, expectedResponseTermSeq=expectedResponseTermSeq)
        if self._writeWait > 0: # Sleep a bit if required (some older modems suffer under load)
            time.sleep(self._writeWait)
        if waitForResponse:
            cmdStatusLine = responseLines[-1]
            if parseError:
                if b'ERROR' in cmdStatusLine:
                    cmErrorMatch = self.CM_ERROR_REGEX.match(cmdStatusLine)
                    if cmErrorMatch:
                        errorType = cmErrorMatch.group(1)
                        errorCode = int(cmErrorMatch.group(2))
                        if errorCode == 515 or errorCode == 14:
                            # 515 means: "Please wait, init or command processing in progress."
                            # 14 means "SIM busy"
                            self._writeWait += 0.2 # Increase waiting period temporarily
                            # Retry the command after waiting a bit
                            self.log.debug('Device/SIM busy error detected; self._writeWait adjusted to %fs', self._writeWait)
                            time.sleep(self._writeWait)
                            result = await self.write(data, waitForResponse, timeout, parseError, writeTerm, expectedResponseTermSeq)
                            self.log.debug('self_writeWait set to 0.1 because of recovering from device busy (515) error')
                            if errorCode == 515:
                                self._writeWait = 0.1 # Set this to something sane for further commands (slow modem)
                            else:
                                self._writeWait = 0 # The modem was just waiting for the SIM card
                            return result
                        if errorType == b'CME':
                            raise CmeError(data, int(errorCode))
                        else: # CMS error
                            raise CmsError(data, int(errorCode))
                    else:
                        raise CommandError(data)
                elif cmdStatusLine == b'COMMAND NOT SUPPORT': # Some Huawei modems respond with this for unknown commands
                    raise CommandError('{} ({})'.format(data,cmdStatusLine))
            return responseLines

    @property
    async def signalStrength(self):
        """ Checks the modem's cellular network signal strength

        :raise CommandError: if an error occurs

        :return: The network signal strength as an integer between 0 and 99, or -1 if it is unknown
        :rtype: int
        """
        csq = self.CSQ_REGEX.match((await self.write('AT+CSQ'))[0])
        if csq:
            ss = int(csq.group(1))
            return ss if ss != 99 else -1
        else:
            raise CommandError()

    @property
    async def manufacturer(self):
        """ :return: The modem's manufacturer's name """
        return (await self.write('AT+CGMI'))[0]

    @property
    async def model(self):
        """ :return: The modem's model name """
        return (await self.write('AT+CGMM'))[0]

    @property
    async def revision(self):
        """ :return: The modem's software revision, or None if not known/supported """
        try:
            return (await self.write('AT+CGMR'))[0]
        except CommandError:
            return None

    @property
    async def imei(self):
        """ :return: The modem's serial number (IMEI number) """
        return (await self.write('AT+CGSN'))[0]

    @property
    async def imsi(self):
        """ :return: The IMSI (International Mobile Subscriber Identity) of the SIM card. The PIN may need to be entered before reading the IMSI """
        return (await self.write('AT+CIMI'))[0]

    @property
    async def networkName(self):
        """ :return: the name of the GSM Network Operator to which the modem is connected """
        copsMatch = lineMatching(b'^\+COPS: (\d),(\d),"(.+)",{0,1}\d*$', await self.write('AT+COPS?')) # response format: +COPS: mode,format,"operator_name",x
        if copsMatch:
            return copsMatch.group(3)

    async def get_supportedCommands(self):
        # """ :return: list of AT commands supported by this modem (without the AT prefix). Returns None if not known """
        try:
            # AT+CLAC responses differ between modems. Most respond with +CLAC: and then a comma-separated list of commands
            # while others simply return each command on a new line, with no +CLAC: prefix
            return await self.write('AT+CLAC', timeout=10)

        except (TimeoutException, CommandError):
            # Try interactive command recognition
            commands = []
            checkable_commands = ['^CVOICE', '+VTS', '^DTMF', '^USSDMODE', '+WIND', '+ZPAS', '+CSCS', '+CNUM']

            # Check if modem is still alive
            try:
                response = await self.write('AT')
            except:
                raise TimeoutException

            # Check all commands that will by considered
            for command in checkable_commands:
                try:
                    # Compose AT command that will read values under specified function
                    at_command='AT'+command+'=?'
                    response = await self.write(at_command)
                    # If there are values inside response - add command to the list
                    commands.append(command)
                except:
                    continue

            # Return found commands
            return commands

    @property
    def smsTextMode(self):
        """ :return: True if the modem is set to use text mode for SMS, False if it is set to use PDU mode """
        return self._smsTextMode

    async def set_smsTextMode(self, textMode):
        """ Set to True for the modem to use text mode for SMS, or False for it to use PDU mode """
        if textMode != self._smsTextMode:
            if self.alive:
                await self.write('AT+CMGF={0}'.format(1 if textMode else 0))
            self._smsTextMode = textMode
            self._compileSmsRegexes()

    @property
    async def smsSupportedEncoding(self):
        """
        :raise NotImplementedError: If an error occures during AT command response parsing.
        :return: List of supported encoding names. """

        # Check if command is available
        if self._commands == None:
            self._commands = self.supportedCommands

        if not '+CSCS' in self._commands:
            self._smsSupportedEncodingNames = []
            return self._smsSupportedEncodingNames

        # Get available encoding names
        response = await self.write('AT+CSCS=?')

        # Check response length (should be 2 - list of options and command status)
        if len(response) != 2:
            self.log.debug('Unhandled +CSCS response: {0}'.format(response))
            raise NotImplementedError

        # Extract encoding names list
        try:
            enc_list = d(response[0])  # Get the first line
            enc_list = enc_list[6:] # Remove '+CSCS' prefix
            # Extract AT list in format ("str", "str2", "str3")
            enc_list = enc_list.split('(')[1]
            enc_list = enc_list.split(')')[0]
            enc_list = enc_list.split(',')
            enc_list = [x.split('"')[1] for x in enc_list]
        except:
            self.log.debug('Unhandled +CSCS response: {0}'.format(response))
            raise NotImplementedError

        self._smsSupportedEncodingNames = enc_list
        return self._smsSupportedEncodingNames

    @property
    async def smsEncoding(self):
        """ :return: Encoding name if encoding command is available, else GSM. """
        if self._commands == None:
            self._commands = self.supportedCommands

        if '+CSCS' in self._commands:
            response = await self.write('AT+CSCS?')

            if len(response) == 2:
                encoding = response[0]
                if encoding.startswith(b'+CSCS'):
                    encoding = encoding[6:].split('"') # remove the +CSCS: prefix before splitting
                    if len(encoding) == 3:
                        self._smsEncoding = encoding[1]
                    else:
                        self.log.debug('Unhandled +CSCS response: {0}'.format(response))
            else:
                self.log.debug('Unhandled +CSCS response: {0}'.format(response))

        return self._smsEncoding

    async def set_smsEncoding(self, encoding):
        """ Set encoding for SMS inside PDU mode.

        :return: True if encoding successfully set, otherwise False. """

        # Check if command is available
        if self._commands is None:
            await self.get_supportedCommands()
            while self._commands is None:
                await asyncio.sleep(.5)

        if not '+CSCS' in self._commands:
            return False

        # Check if command is available
        if self._smsSupportedEncodingNames == None:
            self.smsSupportedEncoding

        # Check if desired encoding is available
        if encoding in self._smsSupportedEncodingNames:
            # Set encoding
            response = await self.write('AT+CSCS="{0}"'.format(d(encoding)))
            if len(response) == 1:
                if response[0].lower() == 'ok':
                    self._smsEncoding = encoding
                    return True

        return False

    async def _setSmsMemory(self, readDelete=None, write=None):
        """ Set the current SMS memory to use for read/delete/write operations """
        # Switch to the correct memory type if required
        if write != None and write != self._smsMemWrite:
            await self.write()
            readDel = readDelete or self._smsMemReadDelete
            await self.write('AT+CPMS="{0}","{1}"'.format(d(readDel), d(write)))
            self._smsMemReadDelete = readDel
            self._smsMemWrite = write
        elif readDelete != None and readDelete != self._smsMemReadDelete:
            await self.write('AT+CPMS="{0}"'.format(d(readDelete)))
            self._smsMemReadDelete = readDelete

    def _compileSmsRegexes(self):
        """ Compiles regular expression used for parsing SMS messages based on current mode """
        if self._smsTextMode:
            if self.CMGR_SM_DELIVER_REGEX_TEXT == None:
                self.CMGR_SM_DELIVER_REGEX_TEXT = re.compile(b'^\+CMGR: "([^"]+)","([^"]+)",[^,]*,"([^"]+)"$')
                self.CMGR_SM_REPORT_REGEXT_TEXT = re.compile(b'^\+CMGR: ([^,]*),\d+,(\d+),"{0,1}([^"]*)"{0,1},\d*,"([^"]+)","([^"]+)",(\d+)$')
        elif self.CMGR_REGEX_PDU == None:
            self.CMGR_REGEX_PDU = re.compile(b'^\+CMGR:\s*(\d*),\s*"{0,1}([^"]*)"{0,1},\s*(\d+)$')

    @property
    async def gsmBusy(self):
        """ :return: Current GSMBUSY state """
        try:
            response = await self.write('AT+GSMBUSY?')
            response = d(response[0]) # Get the first line
            response = response[10] # Remove '+GSMBUSY: ' prefix
            self._gsmBusy = response
        except:
            pass # If error is related to ME funtionality: +CME ERROR: <error>
        return self._gsmBusy
    @gsmBusy.setter
    async def gsmBusy(self, gsmBusy):
        """ Sete GSMBUSY state """
        if gsmBusy != self._gsmBusy:
            if self.alive:
                await self.write('AT+GSMBUSY="{0}"'.format(d(gsmBusy)))
            self._gsmBusy = gsmBusy

    @property
    async def smsc(self):
        """ :return: The default SMSC number stored on the SIM card """
        if self._smscNumber == None:
            try:
                readSmsc = await self.write('AT+CSCA?')
            except SmscNumberUnknownError:
                pass # Some modems return a CMS 330 error if the value isn't set
            else:
                cscaMatch = lineMatching(b'\+CSCA:\s*"([^,]+)",(\d+)$', readSmsc)
                if cscaMatch:
                    self._smscNumber = cscaMatch.group(1)
        return self._smscNumber

    async def set_smsc(self, smscNumber):
        """ Set the default SMSC number to use when sending SMS messages """
        if smscNumber != self._smscNumber:
            if self.alive:
                await self.write('AT+CSCA="{0}"'.format(d(smscNumber)))
            self._smscNumber = smscNumber

    @property
    async def ownNumber(self):
        """ Query subscriber phone number.

        It must be stored on SIM by operator.
        If is it not stored already, it usually is possible to store the number by user.
        
                :raise TimeoutException: if a timeout was specified and reached


        :return: Subscriber SIM phone number. Returns None if not known
        :rtype: int
        """

        try:
            if "+CNUM" in self._commands:
                response = await self.write('AT+CNUM')
            else:
                # temporarily switch to "own numbers" phonebook, read position 1 and than switch back
                response = await self.write('AT+CPBS?')
                selected_phonebook = response[0][6:].split('"')[1] # first line, remove the +CSCS: prefix, split, first parameter

                if selected_phonebook is not "ON":
                    await self.write('AT+CPBS="ON"')

                response = await self.write("AT+CPBR=1")
                await self.write('AT+CPBS="{0}"'.format(selected_phonebook))

            if response is "OK": # command is supported, but no number is set
                return None
            elif len(response) == 2: # OK and phone number. Actual number is in the first line, second parameter, and is placed inside quotation marks
                cnumLine = response[0]
                cnumMatch = self.CNUM_REGEX.match(cnumLine)
                if cnumMatch:
                    return cnumMatch.group(1)
                else:
                    self.log.debug('Error parse +CNUM response: {0}'.format(response))
                    return None
            elif len(response) > 2: # Multi-line response
                self.log.debug('Unhandled +CNUM/+CPBS response: {0}'.format(response))
                return None

        except (TimeoutException, CommandError):
            raise

    @ownNumber.setter
    async def ownNumber(self, phone_number):
        actual_phonebook = await self.write('AT+CPBS?')
        if actual_phonebook is not "ON":
            await self.write('AT+CPBS="ON"')
        await self.write('AT+CPBW=1,"' + phone_number + '"')

    async def waitForNetworkCoverage(self, timeout=None):
        """ Block until the modem has GSM network coverage.

        This method blocks until the modem is registered with the network
        and the signal strength is greater than 0, optionally timing out
        if a timeout was specified

        :param timeout: Maximum time to wait for network coverage, in seconds
        :type timeout: int or float

        :raise TimeoutException: if a timeout was specified and reached
        :raise InvalidStateException: if the modem is not going to receive network coverage (SIM blocked, etc)

        :return: the current signal strength
        """

        if timeout:
            return await asyncio.wait_for(self._doWaitForNetworkCoverage(), timeout=timeout)

        else:
            return await self._doWaitForNetworkCoverage()

    async def _doWaitForNetworkCoverage(self):
        ss = -1
        checkCreg = True
        while True:
            if checkCreg:
                cregResult = lineMatching(b'^\+CREG:\s*(\d),(\d)(,[^,]*,[^,]*)?$',
                                          await self.write('AT+CREG?', parseError=False))  # example result: +CREG: 0,1
                if cregResult:
                    status = int(cregResult.group(2))
                    if status in (1, 5):
                        # 1: registered, home network, 5: registered, roaming
                        # Now simply check and return network signal strength
                        checkCreg = False
                    elif status == 3:
                        raise InvalidStateException('Network registration denied')
                    elif status == 0:
                        raise InvalidStateException('Device not searching for network operator')
                else:
                    # Disable network registration check; only use signal strength
                    self.log.info('+CREG check disabled due to invalid response or unsupported command')
                    checkCreg = False
            else:
                # Check signal strength
                ss = await self.signalStrength
                if ss > 0:
                    return ss
            await asyncio.sleep(1)


    async def sendSms(self, destination, text, waitForDeliveryReport=False, deliveryTimeout=15, sendFlash=False):
        """ Send an SMS text message

        :param destination: the recipient's phone number
        :type destination: str
        :param text: the message text
        :type text: str
        :param waitForDeliveryReport: if True, this method blocks until a delivery report is received for the sent message
        :type waitForDeliveryReport: boolean
        :param deliveryReport: the maximum time in seconds to wait for a delivery report (if "waitForDeliveryReport" is True)
        :type deliveryTimeout: int or float

        :raise CommandError: if an error occurs while attempting to send the message
        :raise TimeoutException: if the operation times out
        """
        with await self.read_write_lock:
            # Check input text to select appropriate mode (text or PDU)
            # Check encoding
            try:
                encodedText = encodeGsm7(text)
            except ValueError:
                encodedText = None
                await self.set_smsTextMode(False)

            # Check message length
            if len(text) > 160:
                await self.set_smsTextMode(False)

            # Send SMS via AT commands
            if self._smsTextMode:
                await self.write('AT+CMGS="{0}"'.format(d(destination)), timeout=5, expectedResponseTermSeq=b'> ')
                result = lineStartingWith(b'+CMGS:', await self.write(text, timeout=35, writeTerm=CTRLZ))
            else:
                # Set GSM modem SMS encoding format
                # Encode message text and set data coding scheme based on text contents
                if encodedText == None:
                    # Cannot encode text using GSM-7; use UCS2 instead
                    await self.set_smsEncoding('UCS2')
                    # await setattr(self, 'smsEncoding', 'UCS2')
                    # self.smsEncoding = 'UCS2'
                else:
                    await self.set_smsEncoding('GSM')
                    # self.smsEncoding = 'GSM'

                pdus = encodeSmsSubmitPdu(destination, text, reference=self._smsRef, sendFlash=sendFlash)
                for pdu in pdus:
                    await self.write('AT+CMGS={0}'.format(pdu.tpduLength), timeout=5, expectedResponseTermSeq=b'> ')
                    result = lineStartingWith(b'+CMGS:', await self.write(str(pdu), timeout=35, writeTerm=CTRLZ)) # example: +CMGS: xx
            if result == None:
                raise CommandError('Modem did not respond with +CMGS response')
            reference = int(result[7:])
            self._smsRef = reference + 1
            if self._smsRef > 255:
                self._smsRef = 0
            sms = SentSms(destination, text, reference)
            # Add a weak-referenced entry for this SMS (allows us to update the SMS state if a status report is received)
            self.sentSms[reference] = sms
            if waitForDeliveryReport:
                self._smsStatusReportEvent = threading.Event()
                if self._smsStatusReportEvent.wait(deliveryTimeout):
                    self._smsStatusReportEvent = None
                else: # Response timed out
                    self._smsStatusReportEvent = None
                    raise TimeoutException()
            return sms

    async def sendUssd(self, ussdString, responseTimeout=15):
        """ Starts a USSD session by dialing the the specified USSD string, or \
        sends the specified string in the existing USSD session (if any)

        :param ussdString: The USSD access number to dial
        :param responseTimeout: Maximum time to wait a response, in seconds

        :raise TimeoutException: if no response is received in time

        :return: The USSD response message/session (as a Ussd object)
        :rtype: gsmmodem.modem.Ussd
        """
        with await self.read_write_lock:
            self._ussdSessionFuture = asyncio.Future()
            try:
                cusdResponse = await self.write('AT+CUSD=1,"{0}",15'.format(d(ussdString)), timeout=responseTimeout) # Should respond with "OK"
            except Exception:
                self._ussdSessionFuture.set_result(None)
                self._ussdSessionFuture = None # Cancel the thread sync lock
                raise

            # Some modems issue the +CUSD response before the acknowledgment "OK" - check for that
            if len(cusdResponse) > 1:
                cusdResponseFound = lineStartingWith(b'+CUSD', cusdResponse) != None
                if cusdResponseFound:
                    self._ussdSessionFuture = None
                    return self._parseCusdResponse(cusdResponse)
            # Wait for the +CUSD notification message
            # await asyncio.sleep(responseTimeout)

            try:
                ussdResponse = await asyncio.wait_for(self._ussdSessionFuture, timeout=responseTimeout)
                self._ussdSessionFuture = None
                return ussdResponse
            except asyncio.TimeoutError:
                self._ussdSessionFuture = None
                raise TimeoutException()

    async def checkForwarding(self, querytype, responseTimeout=15):
        """ Check forwarding status: 0=Unconditional, 1=Busy, 2=NoReply, 3=NotReach, 4=AllFwd, 5=AllCondFwd
        :param querytype: The type of forwarding to check

        :return: Status
        :rtype: Boolean
        """
        try:
            queryResponse = await self.write('AT+CCFC={0},2'.format(d(querytype)), timeout=responseTimeout) # Should respond with "OK"
        except Exception:
            raise
        return True

    async def setForwarding(self, fwdType, fwdEnable, fwdNumber, responseTimeout=15):
        """ Check forwarding status: 0=Unconditional, 1=Busy, 2=NoReply, 3=NotReach, 4=AllFwd, 5=AllCondFwd
        :param fwdType: The type of forwarding to set
        :param fwdEnable: 1 to enable, 0 to disable, 2 to query, 3 to register, 4 to erase
        :param fwdNumber: Number to forward to

        :return: Success or not
        :rtype: Boolean
        """
        try:
            queryResponse = await self.write('AT+CCFC={0},{1},"{2}"'.format(d(fwdType), d(fwdEnable), d(fwdNumber)), timeout=responseTimeout) # Should respond with "OK"
        except Exception:
            raise
            return False
        return queryResponse

    async def dial(self, number, timeout=5, callStatusUpdateCallbackFunc=None):
        """ Calls the specified phone number using a voice phone call

        :param number: The phone number to dial
        :param timeout: Maximum time to wait for the call to be established
        :param callStatusUpdateCallbackFunc: Callback function that is executed if the call's status changes due to
               remote events (i.e. when it is answered, the call is ended by the remote party)

        :return: The outgoing call
        :rtype: gsmmodem.modem.Call
        """
        if self._waitForCallInitUpdate:
            # Wait for the "call originated" notification message
            self._dialEvent = threading.Event()
            try:
                await self.write('ATD{0};'.format(d(number)), timeout=timeout, waitForResponse=self._waitForAtdResponse)
            except Exception:
                self._dialEvent = None # Cancel the thread sync lock
                raise
        else:
            # Don't wait for a call init update - base the call ID on the number of active calls
            await self.write('ATD{0};'.format(d(number)), timeout=timeout, waitForResponse=self._waitForAtdResponse)
            self.log.debug("Not waiting for outgoing call init update message")
            callId = len(self.activeCalls) + 1
            callType = 0 # Assume voice
            call = Call(self, callId, callType, number, callStatusUpdateCallbackFunc)
            self.activeCalls[callId] = call
            return call

        if self._mustPollCallStatus:
            # Fake a call notification by polling call status until the status indicates that the call is being dialed
            threading.Thread(target=self._pollCallStatus, kwargs={'expectedState': 0, 'timeout': timeout}).start()

        if self._dialEvent.wait(timeout):
            self._dialEvent = None
            callId, callType = self._dialResponse
            call = Call(self, callId, callType, number, callStatusUpdateCallbackFunc)
            self.activeCalls[callId] = call
            return call
        else: # Call establishing timed out
            self._dialEvent = None
            raise TimeoutException()

    def processStoredSms(self, unreadOnly=False):
        """ Process all SMS messages currently stored on the device/SIM card.

        Reads all (or just unread) received SMS messages currently stored on the
        device/SIM card, initiates "SMS received" events for them, and removes
        them from the SIM card.
        This is useful if SMS messages were received during a period that
        python-gsmmodem was not running but the modem was powered on.

        :param unreadOnly: If True, only process unread SMS messages
        :type unreadOnly: boolean
        """
        if self.smsReceivedCallback:
            states = [Sms.STATUS_RECEIVED_UNREAD]
            if not unreadOnly:
                states.insert(0, Sms.STATUS_RECEIVED_READ)
            for msgStatus in states:
                messages = self.listStoredSms(status=msgStatus, delete=True)
                for sms in messages:
                    self.smsReceivedCallback(sms)
        else:
            raise ValueError('GsmModem.smsReceivedCallback not set')

    async def listStoredSms(self, status=Sms.STATUS_ALL, memory=None, delete=False):
        """ Returns SMS messages currently stored on the device/SIM card.

        The messages are read from the memory set by the "memory" parameter.

        :param status: Filter messages based on this read status; must be 0-4 (see Sms class)
        :type status: int
        :param memory: The memory type to read from. If None, use the current default SMS read memory
        :type memory: str or None
        :param delete: If True, delete returned messages from the device/SIM card
        :type delete: bool

        :return: A list of Sms objects containing the messages read
        :rtype: list
        """
        with await self.read_write_lock:
            await self._setSmsMemory(readDelete=memory)
            messages = []
            delMessages = set()
            if self._smsTextMode:
                cmglRegex= re.compile(b'^\+CMGL: (\d+),"([^"]+)","([^"]+)",[^,]*,"([^"]+)"$')
                for key, val in dictItemsIter(Sms.TEXT_MODE_STATUS_MAP):
                    if status == val:
                        statusStr = key
                        break
                else:
                    raise ValueError('Invalid status value: {0}'.format(status))
                result = await self.write('AT+CMGL="{0}"'.format(d(statusStr)))
                msgLines = []
                msgIndex = msgStatus = number = msgTime = None
                for line in result:
                    cmglMatch = cmglRegex.match(line)
                    if cmglMatch:
                        # New message; save old one if applicable
                        if msgIndex != None and len(msgLines) > 0:
                            msgText = '\n'.join(msgLines)
                            msgLines = []
                            messages.append(ReceivedSms(self, Sms.TEXT_MODE_STATUS_MAP[msgStatus], number, parseTextModeTimeStr(msgTime), msgText))
                            delMessages.add(int(msgIndex))
                        msgIndex, msgStatus, number, msgTime = cmglMatch.groups()
                        msgLines = []
                    else:
                        if line != b'OK':
                            msgLines.append(line)
                if msgIndex != None and len(msgLines) > 0:
                    msgText = '\n'.join(msgLines)
                    msgLines = []
                    messages.append(ReceivedSms(self, Sms.TEXT_MODE_STATUS_MAP[msgStatus], number, parseTextModeTimeStr(msgTime), msgText))
                    delMessages.add(int(msgIndex))
            else:
                cmglRegex = re.compile(b'^\+CMGL:\s*(\d+),\s*(\d+),.*$')
                readPdu = False
                result = await self.write('AT+CMGL={0}'.format(d(status)))
                for line in result:
                    if not readPdu:
                        cmglMatch = cmglRegex.match(line)
                        if cmglMatch:
                            self.log.debug('CMGL line matched {} for line {}'.format(cmglMatch, line))
                            msgIndex = int(cmglMatch.group(1))
                            msgStat = int(cmglMatch.group(2))
                            readPdu = True
                        else:
                            self.log.debug('CMGL line did NOT match {} for line {}'.format(cmglMatch, line))
                    else:
                        try:
                            smsDict = decodeSmsPdu(line)
                        except EncodingError:
                            self.log.debug('Discarding line from +CMGL response: %s', line)
                        except:
                            self.log.debug('Unknown exception when decoding line from +CMGL response: {}\n{}', line, traceback.format_exc())
                            pass
                            # dirty fix warning: https://github.com/yuriykashin/python-gsmmodem/issues/1
                            # todo: make better fix
                        else:
                            self.log.debug('PDU decoded for line from +CMGL response {} with type {}'.format(line, smsDict['type']))
                            if smsDict['type'] == 'SMS-DELIVER':
                                sms = ReceivedSms(self, int(msgStat), smsDict['number'], smsDict['time'], smsDict['text'], smsDict['smsc'], smsDict.get('udh', []))
                            elif smsDict['type'] == 'SMS-STATUS-REPORT':
                                sms = StatusReport(self, int(msgStat), smsDict['reference'], smsDict['number'], smsDict['time'], smsDict['discharge'], smsDict['status'])
                            else:
                                raise CommandError('Invalid PDU type for readStoredSms(): {0}'.format(smsDict['type']))
                            messages.append(sms)
                            delMessages.add(msgIndex)
                            readPdu = False
            if delete:
                if status == Sms.STATUS_ALL:
                    # Delete all messages
                    await self.deleteMultipleStoredSms()
                else:
                    for msgIndex in delMessages:
                        await self.deleteStoredSms(msgIndex)
            return messages

    async def _handleModemNotification(self, lines):
        """ Handler for unsolicited notifications from the modem

        This method simply spawns a separate thread to handle the actual notification
        (in order to release the read thread so that the handlers are able to write back to the modem, etc)

        :param lines The lines that were read
        """
        # threading.Thread(target=self.__threadedHandleModemNotification, kwargs={'lines': lines}).start()
        await self.__threadedHandleModemNotification(lines)

    async def __threadedHandleModemNotification(self, lines):
        """ Implementation of _handleModemNotification() to be run in a separate thread

        :param lines The lines that were read
        """
        next_line_is_te_statusreport = False
        for line in lines:
            if b'RING' in line:
                # Incoming call (or existing call is ringing)
                await self._handleIncomingCall(lines)
                return
            elif line.startswith(b'+CMTI'):
                # New SMS message indication
                await self._handleSmsReceived(line)
                return
            elif line.startswith(b'+CUSD'):
                # USSD notification - either a response or a MT-USSD ("push USSD") message
                self._handleUssd(lines)
                return
            elif line.startswith(b'+CDSI'):
                # SMS status report
                await self._handleSmsStatusReport(line)
                return
            elif line.startswith(b'+CDS'):
                # SMS status report at next line
                next_line_is_te_statusreport = True
                cdsMatch = self.CDS_REGEX.match(line)
                if cdsMatch:
                    next_line_is_te_statusreport_length = int(cdsMatch.group(1))
                else:
                    next_line_is_te_statusreport_length = -1
            elif next_line_is_te_statusreport:
                self._handleSmsStatusReportTe(next_line_is_te_statusreport_length, line)
                return
            elif line.startswith(b'+DTMF'):
                # New incoming DTMF
                self._handleIncomingDTMF(line)
                return
            else:
                # Check for call status updates
                for updateRegex, handlerFunc in self._callStatusUpdates:
                    match = updateRegex.match(line)
                    if match:
                        # Handle the update
                        handlerFunc(match)
                        return
        # If this is reached, the notification wasn't handled
        self.log.debug('Unhandled unsolicited modem notification: %s', lines)

    #Simcom modem able detect incoming DTMF
    def _handleIncomingDTMF(self,line):
        self.log.debug('Handling incoming DTMF')

        try:
            dtmf_num=line.split(':')[1].replace(" ","")
            self.dtmfpool.append(dtmf_num)
            self.log.debug('DTMF number is {0}'.format(dtmf_num))
        except:
            self.log.debug('Error parse DTMF number on line {0}'.format(line))
    def GetIncomingDTMF(self):
        if (len(self.dtmfpool)==0):
            return None
        else:
            return self.dtmfpool.pop(0)

    async def _handleIncomingCall(self, lines):
        with await self.read_write_lock:
            self.log.debug('Handling incoming call')
            ringLine = d(lines.pop(0))
            if self._extendedIncomingCallIndication:
                try:
                    callType = ringLine.split(' ', 1)[1]
                except IndexError:
                    # Some external 3G scripts modify incoming call indication settings (issue #18)
                    self.log.debug('Extended incoming call indication format changed externally; re-enabling...')
                    callType = None
                    try:
                        # Re-enable extended format of incoming indication (optional)
                        await self.write('AT+CRC=1')
                    except CommandError:
                        self.log.warn('Extended incoming call indication format changed externally; unable to re-enable')
                        self._extendedIncomingCallIndication = False
            else:
                callType = None
            if self._callingLineIdentification and len(lines) > 0:
                clipLine = lines.pop(0)
                clipMatch = self.CLIP_REGEX.match(clipLine)
                if clipMatch:
                    callerNumber = clipMatch.group(1)
                    ton = clipMatch.group(2)
                    #TODO: re-add support for this
                    callerName = None
                    #callerName = clipMatch.group(3)
                    #if callerName != None and len(callerName) == 0:
                    #    callerName = None
                else:
                    callerNumber = ton = callerName = None
            else:
                callerNumber = ton = callerName = None

            call = None
            for activeCall in dictValuesIter(self.activeCalls):
                if activeCall.number == callerNumber:
                    call = activeCall
                    call.ringCount += 1
            if call == None:
                callId = len(self.activeCalls) + 1;
                call = IncomingCall(self, callerNumber, ton, callerName, callId, callType)
                self.activeCalls[callId] = call
            self.incomingCallCallback(call)

    def _handleCallInitiated(self, regexMatch, callId=None, callType=1):
        """ Handler for "outgoing call initiated" event notification line """
        if self._dialEvent:
            if regexMatch:
                groups = regexMatch.groups()
                # Set self._dialReponse to (callId, callType)
                if len(groups) >= 2:
                    self._dialResponse = (int(groups[0]) , int(groups[1]))
                else:
                    self._dialResponse = (int(groups[0]), 1) # assume call type: VOICE
            else:
                self._dialResponse = callId, callType
            self._dialEvent.set()

    def _handleCallAnswered(self, regexMatch, callId=None):
        """ Handler for "outgoing call answered" event notification line """
        if regexMatch:
            groups = regexMatch.groups()
            if len(groups) > 1:
                callId = int(groups[0])
                self.activeCalls[callId].answered = True
            else:
                # Call ID not available for this notificition - check for the first outgoing call that has not been answered
                for call in dictValuesIter(self.activeCalls):
                    if call.answered == False and type(call) == Call:
                        call.answered = True
                        return
        else:
            # Use supplied values
            self.activeCalls[callId].answered = True

    def _handleCallEnded(self, regexMatch, callId=None, filterUnanswered=False):
        if regexMatch:
            groups = regexMatch.groups()
            if len(groups) > 0:
                callId = int(groups[0])
            else:
                # Call ID not available for this notification - check for the first outgoing call that is active
                for call in dictValuesIter(self.activeCalls):
                    if type(call) == Call:
                        if not filterUnanswered or (filterUnanswered == True and call.answered == False):
                            callId = call.id
                            break
        if callId and callId in self.activeCalls:
            self.activeCalls[callId].answered = False
            self.activeCalls[callId].active = False
            del self.activeCalls[callId]

    def _handleCallRejected(self, regexMatch, callId=None):
        """ Handler for rejected (unanswered calls being ended)

        Most modems use _handleCallEnded for handling both call rejections and remote hangups.
        This method does the same, but filters for unanswered calls only.
        """
        return self._handleCallEnded(regexMatch, callId, True)

    async def _handleSmsReceived(self, notificationLine):
        """ Handler for "new SMS" unsolicited notification line """
        with await self.read_write_lock:
            self.log.debug('SMS message received')
            if self.smsReceivedCallback is not None:
                cmtiMatch = self.CMTI_REGEX.match(notificationLine)
                if cmtiMatch:
                    msgMemory = cmtiMatch.group(1)
                    msgIndex = cmtiMatch.group(2)
                    sms = await self.readStoredSms(msgIndex, msgMemory)
                    try:
                        self.smsReceivedCallback(sms)
                    except Exception:
                        self.log.error('error in smsReceivedCallback', exc_info=True)
                    else:
                        await self.deleteStoredSms(msgIndex)

    async def _handleSmsStatusReport(self, notificationLine):
        """ Handler for SMS status reports """
        self.log.debug('SMS status report received')
        cdsiMatch = self.CDSI_REGEX.match(notificationLine)
        if cdsiMatch:
            msgMemory = cdsiMatch.group(1)
            msgIndex = cdsiMatch.group(2)
            report = await self.readStoredSms(msgIndex, msgMemory)
            await self.deleteStoredSms(msgIndex)
            # Update sent SMS status if possible
            if report.reference in self.sentSms:
                self.sentSms[report.reference].report = report
            if self._smsStatusReportEvent:
                # A sendSms() call is waiting for this response - notify waiting thread
                self._smsStatusReportEvent.set()
            elif self.smsStatusReportCallback:
                # Nothing is waiting for this report directly - use callback
                try:
                    self.smsStatusReportCallback(report)
                except Exception:
                    self.log.error('error in smsStatusReportCallback', exc_info=True)


                self.smsStatusReportCallback(report)

    def _handleSmsStatusReportTe(self, length, notificationLine):
        """ Handler for TE SMS status reports """
        self.log.debug('TE SMS status report received')
        try:
            smsDict = decodeSmsPdu(notificationLine)
        except EncodingError:
            self.log.debug('Discarding notification line from +CDS response: %s', notificationLine)
        else:
            if smsDict['type'] == 'SMS-STATUS-REPORT':
                report = StatusReport(self, int(smsDict['status']), smsDict['reference'], smsDict['number'], smsDict['time'], smsDict['discharge'], smsDict['status'])
            else:
                raise CommandError('Invalid PDU type for readStoredSms(): {0}'.format(smsDict['type']))
        # Update sent SMS status if possible
        if report.reference in self.sentSms:
            self.sentSms[report.reference].report = report
        if self._smsStatusReportEvent:
            # A sendSms() call is waiting for this response - notify waiting thread
            self._smsStatusReportEvent.set()
        else:
            # Nothing is waiting for this report directly - use callback
            self.smsStatusReportCallback(report)

    async def readStoredSms(self, index, memory=None):
        """ Reads and returns the SMS message at the specified index

        :param index: The index of the SMS message in the specified memory
        :type index: int
        :param memory: The memory type to read from. If None, use the current default SMS read memory
        :type memory: str or None

        :raise CommandError: if unable to read the stored message

        :return: The SMS message
        :rtype: subclass of gsmmodem.modem.Sms (either ReceivedSms or StatusReport)
        """
        # Switch to the correct memory type if required
        await self._setSmsMemory(readDelete=memory)
        msgData = await self.write('AT+CMGR={0}'.format(d(index)))
        # Parse meta information
        if self._smsTextMode:
            cmgrMatch = self.CMGR_SM_DELIVER_REGEX_TEXT.match(msgData[0])
            if cmgrMatch:
                msgStatus, number, msgTime = cmgrMatch.groups()
                msgText = '\n'.join(msgData[1:-1])
                return ReceivedSms(self, Sms.TEXT_MODE_STATUS_MAP[msgStatus], number, parseTextModeTimeStr(msgTime), msgText)
            else:
                # Try parsing status report
                cmgrMatch = self.CMGR_SM_REPORT_REGEXT_TEXT.match(msgData[0])
                if cmgrMatch:
                    msgStatus, reference, number, sentTime, deliverTime, deliverStatus = cmgrMatch.groups()
                    if msgStatus.startswith('"'):
                        msgStatus = msgStatus[1:-1]
                    if len(msgStatus) == 0:
                        msgStatus = "REC UNREAD"
                    return StatusReport(self, Sms.TEXT_MODE_STATUS_MAP[msgStatus], int(reference), number, parseTextModeTimeStr(sentTime), parseTextModeTimeStr(deliverTime), int(deliverStatus))
                else:
                    raise CommandError('Failed to parse text-mode SMS message +CMGR response: {0}'.format(msgData))
        else:
            cmgrMatch = self.CMGR_REGEX_PDU.match(msgData[0])
            if not cmgrMatch:
                raise CommandError('Failed to parse PDU-mode SMS message +CMGR response: {0}'.format(msgData))
            stat, alpha, length = cmgrMatch.groups()
            try:
                stat = int(stat)
            except Exception:
                # Some modems (ZTE) do not always read return status - default to RECEIVED UNREAD
                stat = Sms.STATUS_RECEIVED_UNREAD
            pdu = msgData[1]
            smsDict = decodeSmsPdu(pdu)
            if smsDict['type'] == 'SMS-DELIVER':
                return ReceivedSms(self, int(stat), smsDict['number'], smsDict['time'], smsDict['text'], smsDict['smsc'], smsDict.get('udh', []))
            elif smsDict['type'] == 'SMS-STATUS-REPORT':
                return StatusReport(self, int(stat), smsDict['reference'], smsDict['number'], smsDict['time'], smsDict['discharge'], smsDict['status'])
            else:
                raise CommandError('Invalid PDU type for readStoredSms(): {0}'.format(smsDict['type']))

    async def deleteStoredSms(self, index, memory=None):
        """ Deletes the SMS message stored at the specified index in modem/SIM card memory

        :param index: The index of the SMS message in the specified memory
        :type index: int
        :param memory: The memory type to delete from. If None, use the current default SMS read/delete memory
        :type memory: str or None

        :raise CommandError: if unable to delete the stored message
        """
        await self._setSmsMemory(readDelete=memory)
        await self.write('AT+CMGD={0},0'.format(d(index)))
        # TODO: make a check how many params are supported by the modem and use the right command. For example, Siemens MC35, TC35 take only one parameter.
        #await self.write('AT+CMGD={0}'.format(index))

    async def deleteMultipleStoredSms(self, delFlag=4, memory=None):
        """ Deletes all SMS messages that have the specified read status.

        The messages are read from the memory set by the "memory" parameter.
        The value of the "delFlag" paramater is the same as the "DelFlag" parameter of the +CMGD command:
        1: Delete All READ messages
        2: Delete All READ and SENT messages
        3: Delete All READ, SENT and UNSENT messages
        4: Delete All messages (this is the default)

        :param delFlag: Controls what type of messages to delete; see description above.
        :type delFlag: int
        :param memory: The memory type to delete from. If None, use the current default SMS read/delete memory
        :type memory: str or None
        :param delete: If True, delete returned messages from the device/SIM card
        :type delete: bool

        :raise ValueErrror: if "delFlag" is not in range [1,4]
        :raise CommandError: if unable to delete the stored messages
        """
        if 0 < delFlag <= 4:
            await self._setSmsMemory(readDelete=memory)
            await self.write('AT+CMGD=1,{0}'.format(d(delFlag)))
        else:
            raise ValueError('"delFlag" must be in range [1,4]')

    def _handleUssd(self, lines):
        """ Handler for USSD event notification line(s) """
        if self._ussdSessionFuture and not self._ussdSessionFuture.done():
            # A sendUssd() call is waiting for this response - parse it
            ussdResponse = self._parseCusdResponse(lines)
            self.loop.call_soon_threadsafe(self._ussdSessionFuture.set_result, ussdResponse)

    def _parseCusdResponse(self, lines):
        """ Parses one or more +CUSD notification lines (for USSD)
        :return: USSD response object
        :rtype: gsmmodem.modem.Ussd
        """
        if len(lines) > 1:
            # Issue #20: Some modem/network combinations use \r\n as in-message EOL indicators;
            # - join lines to compensate for that (thanks to davidjb for the fix)
            # Also, look for more than one +CUSD response because of certain modems' strange behaviour
            cusdMatches = list(self.CUSD_REGEX.finditer('\r\n'.join(lines)))
        else:
            # Single standard +CUSD response
            cusdMatches = [self.CUSD_REGEX.match(lines[0])]
        message = None
        sessionActive = True
        if len(cusdMatches) > 1:
            self.log.debug('Multiple +CUSD responses received; filtering...')
            # Some modems issue a non-standard "extra" +CUSD notification for releasing the session
            for cusdMatch in cusdMatches:
                if cusdMatch.group(1) == b'2':
                    # Set the session to inactive, but ignore the message
                    self.log.debug('Ignoring "session release" message: %s', cusdMatch.group(2))
                    sessionActive = False
                else:
                    # Not a "session release" message
                    message = cusdMatch.group(2)
                    if sessionActive and cusdMatch.group(1) != b'1':
                        sessionActive = False
        else:
            sessionActive = cusdMatches[0].group(1) == b'1'
            message = cusdMatches[0].group(2)
        return Ussd(self, sessionActive, message)

    def _placeHolderCallback(self, *args):
        """ Does nothing """
        self.log.debug('called with args: {0}'.format(args))

    async def _pollCallStatus(self, expectedState, callId=None, timeout=None):
        """ Poll the status of outgoing calls.
        This is used for modems that do not have a known set of call status update notifications.

        :param expectedState: The internal state we are waiting for. 0 == initiated, 1 == answered, 2 = hangup
        :type expectedState: int

        :raise TimeoutException: If a timeout was specified, and has occurred
        """
        callDone = False
        timeLeft = timeout or 999999
        while self.alive and not callDone and timeLeft > 0:
            time.sleep(0.5)
            if expectedState == 0: # Only call initializing can timeout
                timeLeft -= 0.5
            try:
                clcc = self._pollCallStatusRegex.match(await self.write('AT+CLCC')[0])
            except TimeoutException as timeout:
                # Can happend if the call was ended during our time.sleep() call
                clcc = None
            if clcc:
                direction = int(clcc.group(2))
                if direction == 0: # Outgoing call
                    # Determine call state
                    stat = int(clcc.group(3))
                    if expectedState == 0: # waiting for call initiated
                        if stat == 2 or stat == 3: # Dialing or ringing ("alerting")
                            callId = int(clcc.group(1))
                            callType = int(clcc.group(4))
                            self._handleCallInitiated(None, callId, callType) # if self_dialEvent is None, this does nothing
                            expectedState = 1 # Now wait for call answer
                    elif expectedState == 1: # waiting for call to be answered
                        if stat == 0: # Call active
                            callId = int(clcc.group(1))
                            self._handleCallAnswered(None, callId)
                            expectedState = 2 # Now wait for call hangup
            elif expectedState == 2 : # waiting for remote hangup
                # Since there was no +CLCC response, the call is no longer active
                callDone = True
                self._handleCallEnded(None, callId=callId)
            elif expectedState == 1: # waiting for call to be answered
                # Call was rejected
                callDone = True
                self._handleCallRejected(None, callId=callId)
        if timeLeft <= 0:
            raise TimeoutException()


class Call(object):
    """ A voice call """

    DTMF_COMMAND_BASE = '+VTS='
    dtmfSupport = False # Indicates whether or not DTMF tones can be sent in calls

    def __init__(self, gsmModem, callId, callType, number, callStatusUpdateCallbackFunc=None):
        """
        :param gsmModem: GsmModem instance that created this object
        :param number: The number that is being called
        """
        self._gsmModem = weakref.proxy(gsmModem)
        self._callStatusUpdateCallbackFunc = callStatusUpdateCallbackFunc
        # Unique ID of this call
        self.id = callId
        # Call type (VOICE == 0, etc)
        self.type = callType
        # The remote number of this call (destination or origin)
        self.number = number
        # Flag indicating whether the call has been answered or not (backing field for "answered" property)
        self._answered = False
        # Flag indicating whether or not the call is active
        # (meaning it may be ringing or answered, but not ended because of a hangup event)
        self.active = True

    @property
    def answered(self):
        return self._answered
    @answered.setter
    def answered(self, answered):
        self._answered = answered
        if self._callStatusUpdateCallbackFunc:
            self._callStatusUpdateCallbackFunc(self)

    def sendDtmfTone(self, tones):
        """ Send one or more DTMF tones to the remote party (only allowed for an answered call)

        Note: this is highly device-dependent, and might not work

        :param digits: A str containining one or more DTMF tones to play, e.g. "3" or "\*123#"

        :raise CommandError: if the command failed/is not supported
        :raise InvalidStateException: if the call has not been answered, or is ended while the command is still executing
        """
        if self.answered:
            dtmfCommandBase = self.DTMF_COMMAND_BASE.format(cid=self.id)
            toneLen = len(tones)
            for tone in list(tones):
              try:
                 self._gsmModem.write('AT{0}{1}'.format(d(dtmfCommandBase),d(tone)), timeout=(5 + toneLen))

              except CmeError as e:
                if e.code == 30:
                    # No network service - can happen if call is ended during DTMF transmission (but also if DTMF is sent immediately after call is answered)
                    raise InterruptedException('No network service', e)
                elif e.code == 3:
                    # Operation not allowed - can happen if call is ended during DTMF transmission
                    raise InterruptedException('Operation not allowed', e)
                else:
                    raise e
        else:
            raise InvalidStateException('Call is not active (it has not yet been answered, or it has ended).')

    def hangup(self):
        """ End the phone call.

        Does nothing if the call is already inactive.
        """
        if self.active:
            self._gsmModem.write('ATH')
            self.answered = False
            self.active = False
        if self.id in self._gsmModem.activeCalls:
            del self._gsmModem.activeCalls[self.id]


class IncomingCall(Call):

    CALL_TYPE_MAP = {'VOICE': 0}

    """ Represents an incoming call, conveniently allowing access to call meta information and -control """
    def __init__(self, gsmModem, number, ton, callerName, callId, callType):
        """
        :param gsmModem: GsmModem instance that created this object
        :param number: Caller number
        :param ton: TON (type of number/address) in integer format
        :param callType: Type of the incoming call (VOICE, FAX, DATA, etc)
        """
        if type(callType) == str:
            callType = self.CALL_TYPE_MAP[callType]
        super(IncomingCall, self).__init__(gsmModem, callId, callType, number)
        # Type attribute of the incoming call
        self.ton = ton
        self.callerName = callerName
        # Flag indicating whether the call is ringing or not
        self.ringing = True
        # Amount of times this call has rung (before answer/hangup)
        self.ringCount = 1

    def answer(self):
        """ Answer the phone call.
        :return: self (for chaining method calls)
        """
        if self.ringing:
            self._gsmModem.write('ATA')
            self.ringing = False
            self.answered = True
        return self

    def hangup(self):
        """ End the phone call. """
        self.ringing = False
        super(IncomingCall, self).hangup()

class Ussd(object):
    """ Unstructured Supplementary Service Data (USSD) message.

    This class contains convenient methods for replying to a USSD prompt
    and to cancel the USSD session
    """

    def __init__(self, gsmModem, sessionActive, message):
        self._gsmModem = weakref.proxy(gsmModem)
        # Indicates if the session is active (True) or has been closed (False)
        self.sessionActive = sessionActive
        self.message = message

    async def reply(self, message):
        """ Sends a reply to this USSD message in the same USSD session

        :raise InvalidStateException: if the USSD session is not active (i.e. it has ended)

        :return: The USSD response message/session (as a Ussd object)
        """
        if self.sessionActive:
            return await self._gsmModem.sendUssd(message)
        else:
            raise InvalidStateException('USSD session is inactive')

    async def cancel(self):
        """ Terminates/cancels the USSD session (without sending a reply)

        Does nothing if the USSD session is inactive.
        """
        if self.sessionActive:
            await self._gsmModem.write('AT+CUSD=2')


class CommandEnum(Enum):
    reset = 0
    echo_off = 1
    supported_commands = 2


