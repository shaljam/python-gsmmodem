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
    ussd_response = await modem.sendUssd(binascii.hexlify(ussd.encode('utf-16-be')).upper(), responseTimeout=30)
    response = binascii.unhexlify(ussd_response.message).decode('utf-16-be')
    print('ussd response: {}'.format(response))

    while True:
        command = input('command>>>\n')
        if command == 'quit':
            break

        print('replying {}'.format(command))
        ussd_response = await ussd_response.reply(binascii.hexlify(command.encode('utf-16-be')).upper())

        response = binascii.unhexlify(ussd_response.message).decode('utf-16-be')
        print('ussd response {}'.format(response))


async def enteghale_etebar(number):
    global modem, args

    # await modem.connect()
    ussd_response = await modem.sendUssd(binascii.hexlify('*133#'.encode('utf-16-be')).upper(), responseTimeout=30)
    response = binascii.unhexlify(ussd_response.message).decode('utf-16-be')
    print('ussd response: {}'.format(response))

    seq = ['1', str(number), '100000', '1942', '1', 'quit']
    index = 0

    while True:
        command = seq[index]
        index += 1
        if command == 'quit':
            break

        print('replying {}'.format(command))
        ussd_response = await ussd_response.reply(binascii.hexlify(command.encode('utf-16-be')).upper())

        response = binascii.unhexlify(ussd_response.message).decode('utf-16-be')
        print('ussd response {}'.format(response))


async def check_modem():
    global modem, args

    modem = GsmModem(args.port, args.baud, AT_CNMI=args.CNMI)
    print('Connecting to GSM modem on {0}...'.format(args.port))
    try:
        await modem.connect(args.pin, waitingForModemToStartInSeconds=args.wait)
        print('connected')

        # print('setting encoding to GSM')
        print('result of setting encoding: {}'.format(await modem.write('AT+CSCS=?')))
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
    parser.add_argument('destination', metavar='DESTINATION', help='destination mobile number')
    parser.add_argument('-t', '--text', metavar='TEXT', help='sms text to be sent')
    parser.add_argument('-u', '--ussd', metavar='USSD', help='ussd command to be sent')
    parser.add_argument('-e', '--enteghal', metavar='ENTEGHAL', help='number to enteghal etebar')
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

    if args.enteghal:
        ussd_task = asyncio.ensure_future(enteghale_etebar(args.enteghal))

        loop.run_until_complete(ussd_task)

    # try:
    #     loop.run_until_complete(task)
    # except KeyboardInterrupt as e:
    #     print("Caught keyboard interrupt. Canceling tasks...")
    #     loop.run_until_complete(modem.close())
    #     task.cancel()
    #     loop.run_forever()
    #     task.exception()
    # finally:
    #     loop.close()


if __name__ == '__main__':
    main()

