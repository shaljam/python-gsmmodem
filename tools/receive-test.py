from gsmmodem import GsmModem
import asyncio
import logging, sys
import binascii

from gsmmodem.exceptions import PinRequiredError, IncorrectPinError, TimeoutException

root = logging.getLogger()
root.setLevel(logging.DEBUG)

ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
root.addHandler(ch)

modem = None
args = None

loop = asyncio.get_event_loop()

async def send_sms(to, message):
    global modem, args

    # await modem.connect()
    await modem.sendSms(to, message)

async def send_ussd(ussd):
    global modem, args

    # await modem.connect()
    response = await modem.sendUssd(binascii.hexlify(ussd.encode('utf-16-be')).upper(), responseTimeout=30)
    print('ussd response: {}'.format(binascii.unhexlify(response.message).decode('utf-16-be')))

async def check_modem():
    global modem, args

    modem = GsmModem(args.port, args.baud, smsReceivedCallbackFunc=sms_received, AT_CNMI=args.CNMI)
    print('Connecting to GSM modem on {0}...'.format(args.port))
    try:
        await modem.connect(args.pin, waitingForModemToStartInSeconds=args.wait)
        print('connected')

        # print('setting encoding to GSM')
        # print('result of setting encoding: {}'.format(await modem.write('AT+CSCS=?')))
        # print('result of setting encoding: {}'.format(await modem.write('AT+CSCS="USC2"')))
    except PinRequiredError:
        sys.stderr.write('Error: SIM card PIN required. Please specify a PIN with the -p argument.\n')
        sys.exit(1)
    except IncorrectPinError:
        sys.stderr.write('Error: Incorrect SIM card PIN entered.\n')
        sys.exit(1)
    print('Checking for network coverage...')
    try:
        await modem.waitForNetworkCoverage(5)
    except TimeoutError:
        print('Network signal strength is not sufficient, please adjust modem position/antenna and try again.')
        await modem.close()
        sys.exit(1)


def sms_received(sms):
    print('received sms {}'.format(sms.text))


async def close_modem():
    await modem.close()


def parseArgs():
    """ Argument parser for Python 2.7 and above """
    from argparse import ArgumentParser
    parser = ArgumentParser(description='Simple script for sending SMS messages')
    parser.add_argument('-i', '--port', metavar='PORT',
                        help='port to which the GSM modem is connected; a number or a device name.')
    parser.add_argument('-b', '--baud', metavar='BAUDRATE', default=115200, help='set baud rate')
    parser.add_argument('-p', '--pin', metavar='PIN', default=None, help='SIM card PIN')
    parser.add_argument('-d', '--deliver', action='store_true', help='wait for SMS delivery report')
    parser.add_argument('-w', '--wait', type=int, default=0, help='Wait for modem to start, in seconds')
    parser.add_argument('--CNMI', default='', help='Set the CNMI of the modem, used for message notifications')
    parser.add_argument('-t', '--text', metavar='TEXT', help='sms text to be sent')
    parser.add_argument('-u', '--ussd', metavar='USSD', help='ussd command to be sent')
    return parser.parse_args()


def main():
    global args

    args = parseArgs()
    if args.port is None:
        sys.stderr.write(
            'Error: No port specified. Please specify the port to which the GSM modem is connected using the -i argument.\n')
        sys.exit(1)

    check_task = asyncio.ensure_future(check_modem())

    loop.run_until_complete(check_task)

    if args.text:
        sms_task = asyncio.ensure_future(send_sms(args.destination, args.text))

        loop.run_until_complete(sms_task)

    if args.ussd:
        ussd_task = asyncio.ensure_future(send_ussd(args.ussd))

        loop.run_until_complete(ussd_task)


    try:
        loop.run_forever()
    except KeyboardInterrupt as e:
        print("Caught keyboard interrupt. Canceling tasks...")
        loop.run_until_complete(close_modem())
    finally:
        loop.close()


if __name__ == '__main__':
    main()

