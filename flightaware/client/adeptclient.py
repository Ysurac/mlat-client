# -*- mode: python; indent-tabs-mode: nil -*-

"""
The FlightAware adept protocol, client side.
"""

import asyncore
import socket
import errno
import sys
import itertools
import struct

from mlat.client import net, util, stats, version


# UDP protocol submessages
# TODO: This needs merging with mlat-client's variant
# (they are not quite identical so it'll need a new
# udp protocol version - this version has the decoded
# ICAO address at the start of MLAT/SYNC to ease the
# work of the server doing fan-out)

TYPE_SYNC = 1
TYPE_MLAT_SHORT = 2
TYPE_MLAT_LONG = 3
#TYPE_SSYNC = 4
TYPE_REBASE = 5
TYPE_ABS_SYNC = 6

STRUCT_HEADER = struct.Struct(">IHQ")
STRUCT_SYNC = struct.Struct(">B3Bii14s14s")
#STRUCT_SSYNC = struct.Struct(">Bi14s")
STRUCT_MLAT_SHORT = struct.Struct(">B3Bi7s")
STRUCT_MLAT_LONG = struct.Struct(">B3Bi14s")
STRUCT_REBASE = struct.Struct(">BQ")
STRUCT_ABS_SYNC = struct.Struct(">B3BQQ14s14s")


class UdpServerConnection:
    def __init__(self, host, port, key):
        self.host = host
        self.port = port
        self.key = key

        self.base_timestamp = None
        self.header_timestamp = None
        self.buf = bytearray(1500)
        self.used = 0
        self.seq = 0
        self.count = 0
        self.sock = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect((self.host, self.port))

    def prepare_header(self, timestamp):
        self.base_timestamp = timestamp
        STRUCT_HEADER.pack_into(self.buf, 0,
                                self.key, self.seq, self.base_timestamp)
        self.used += STRUCT_HEADER.size

    def rebase(self, timestamp):
        self.base_timestamp = timestamp
        STRUCT_REBASE.pack_into(self.buf, self.used,
                                TYPE_REBASE,
                                self.base_timestamp)
        self.used += STRUCT_REBASE.size

    def send_mlat(self, message):
        if not self.used:
            self.prepare_header(message.timestamp)

        delta = message.timestamp - self.base_timestamp
        if abs(delta) > 0x7FFFFFF0:
            self.rebase(message.timestamp)
            delta = 0

        if len(message) == 7:
            STRUCT_MLAT_SHORT.pack_into(self.buf, self.used,
                                        TYPE_MLAT_SHORT,
                                        message.address >> 16,
                                        (message.address >> 8) & 255,
                                        message.address & 255,
                                        delta, bytes(message))
            self.used += STRUCT_MLAT_SHORT.size

        else:
            STRUCT_MLAT_LONG.pack_into(self.buf, self.used,
                                       TYPE_MLAT_LONG,
                                       message.address >> 16,
                                       (message.address >> 8) & 255,
                                       message.address & 255,
                                       delta, bytes(message))
            self.used += STRUCT_MLAT_LONG.size

        if self.used > 1400:
            self.flush()

    def send_sync(self, em, om):
        if not self.used:
            self.prepare_header(int((em.timestamp + om.timestamp) / 2))

        if abs(em.timestamp - om.timestamp) > 0xFFFFFFF0:
            # use abs sync
            STRUCT_ABS_SYNC.pack_into(self.buf, self.used,
                                      TYPE_ABS_SYNC,
                                      em.address >> 16,
                                      (em.address >> 8) & 255,
                                      em.address & 255,
                                      em.timestamp, om.timestamp, bytes(em), bytes(om))
            self.used += STRUCT_ABS_SYNC.size
        else:
            edelta = em.timestamp - self.base_timestamp
            odelta = om.timestamp - self.base_timestamp
            if abs(edelta) > 0x7FFFFFF0 or abs(odelta) > 0x7FFFFFF0:
                self.rebase(int((em.timestamp + om.timestamp) / 2))
                edelta = em.timestamp - self.base_timestamp
                odelta = om.timestamp - self.base_timestamp

            STRUCT_SYNC.pack_into(self.buf, self.used,
                                  TYPE_SYNC,
                                  em.address >> 16,
                                  (em.address >> 8) & 255,
                                  em.address & 255,
                                  edelta, odelta, bytes(em), bytes(om))
            self.used += STRUCT_SYNC.size

        if self.used > 1400:
            self.flush()

    def flush(self):
        if not self.used:
            return

        try:
            self.sock.send(memoryview(self.buf)[0:self.used])
        except socket.error:
            pass

        stats.global_stats.server_udp_bytes += self.used

        self.used = 0
        self.base_timestamp = None
        self.seq = (self.seq + 1) & 0xffff
        self.count += 1

    def close(self):
        self.used = 0
        if self.sock:
            self.sock.close()

    def __str__(self):
        return '{0}:{1}'.format(self.host, self.port)


class AdeptReader(asyncore.file_dispatcher, net.LoggingMixin):
    """Reads tab-separated key-value messages from stdin and dispatches them."""

    def __init__(self, connection, coordinator):
        super().__init__(sys.stdin)

        self.connection = connection
        self.coordinator = coordinator
        self.partial_line = b''
        self.closed = False

        self.handlers = {
            'mlat_wanted': self.process_wanted_message,
            'mlat_unwanted': self.process_unwanted_message,
            'mlat_result': self.process_result_message,
            'mlat_status': self.process_status_message
        }

    def readable(self):
        return True

    def writable(self):
        return False

    def handle_read(self):
        try:
            moredata = self.recv(16384)
        except socket.error as e:
            if e.errno == errno.EAGAIN:
                return
            raise

        if not moredata:
            self.close()
            return

        stats.global_stats.server_rx_bytes += len(moredata)

        data = self.partial_line + moredata
        lines = data.split(b'\n')
        for line in lines[:-1]:
            try:
                self.process_line(line.decode('ascii'))
            except IOError:
                raise
            except Exception:
                util.log_exc('Unexpected exception processing adept message')

        self.partial_line = lines[-1]

    def handle_close(self):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            super().close()
            self.connection.disconnect()

    def process_line(self, line):
        fields = line.split('\t')
        message = dict(zip(fields[0::2], fields[1::2]))

        handler = self.handlers.get(message['type'])
        if handler:
            handler(message)

    def process_wanted_message(self, message):
        if message['hexids'] == '':
            wanted = set()
        else:
            wanted = {int(x, 16) for x in message['hexids'].split(' ')}
        self.coordinator.server_start_sending(wanted)

    def process_unwanted_message(self, message):
        if message['hexids'] == '':
            unwanted = set()
        else:
            unwanted = {int(x, 16) for x in message['hexids'].split(' ')}
        self.coordinator.server_stop_sending(unwanted)

    def process_result_message(self, message):
        self.coordinator.server_mlat_result(timestamp=None,
                                            addr=int(message['hexid'], 16),
                                            lat=float(message['lat']),
                                            lon=float(message['lon']),
                                            alt=float(message['alt']),
                                            nsvel=float(message['nsvel']),
                                            ewvel=float(message['ewvel']),
                                            vrate=float(message['fpm']),
                                            callsign=None,
                                            squawk=None,
                                            error_est=None,
                                            nstations=None)

    def process_status_message(self, message):
        s = message.get('status', 'unknown')
        r = int(message.get('receiver_sync_count', 0))

        if s == 'ok':
            self.connection.state = "synchronized with {} nearby receivers".format(r)
        elif s == 'unstable':
            self.connection.state = "clock unstable"
        elif s == 'no_sync':
            self.connection.state = "not synchronized with any nearby receivers"
        else:
            self.connection.state = "{} {}".format(s, r)


class AdeptWriter(asyncore.file_dispatcher, net.LoggingMixin):
    """Writes tab-separated key-value messages to stdout."""

    def __init__(self, connection):
        super().__init__(sys.stdout)
        self.connection = connection
        self.writebuf = bytearray()
        self.closed = False

    def readable(self):
        return False

    def writable(self):
        return bool(self.writebuf)

    def handle_write(self):
        if self.writebuf:
            sent = self.send(self.writebuf)
            del self.writebuf[:sent]
            stats.global_stats.server_tx_bytes += sent
            if len(self.writebuf) > 65536:
                raise IOError('Server write buffer overflow (too much unsent data)')

    def handle_close(self):
        self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            super().close()
            self.connection.disconnect()

    def send_message(self, **kwargs):
        line = '\t'.join(itertools.chain.from_iterable(kwargs.items())) + '\n'
        self.writebuf += line.encode('ascii')

    # mlat/sync directly format the message rather than using
    # send_message, as these on the hot path.

    def send_mlat(self, message):
        if message.df <= 15:  # DF 0..15 are 56-bit messages
            line = 'type\tmlat_mlat\thexid\t{a:06X}\tm_short\t{t:012x} {m}\n'.format(
                a=message.address,
                t=message.timestamp,
                m=str(message))
        else:  # DF 16..31 are 112-bit messages
            line = 'type\tmlat_mlat\thexid\t{a:06X}\tm_long\t{t:012x} {m}\n'.format(
                a=message.address,
                t=message.timestamp,
                m=str(message))

        self.writebuf += line.encode('ascii')

    def send_sync(self, em, om):
        line = 'type\tmlat_sync\thexid\t{a:06X}\tm_sync\t{et:012x} {em} {ot:012x} {om}\n'.format(
            a=em.address,
            et=em.timestamp,
            em=str(em),
            ot=om.timestamp,
            om=str(om))
        self.writebuf += line.encode('ascii')

    def send_seen(self, aclist):
        self.send_message(type='mlat_seen',
                          hexids=' '.join('{0:06X}'.format(icao) for icao in aclist))

    def send_lost(self, aclist):
        self.send_message(type='mlat_lost',
                          hexids=' '.join('{0:06X}'.format(icao) for icao in aclist))

    def send_rate_report(self, report):
        self.send_message(type='mlat_rates',
                          rates=' '.join('{0:06X} {1:.2f}'.format(icao, rate) for icao, rate in report.items()))

    def send_ready(self):
        self.send_message(type='mlat_event', event='ready', mlat_client_version=version.CLIENT_VERSION)

    def send_input_connected(self):
        self.send_message(type='mlat_event', event='connected')

    def send_input_disconnected(self):
        self.send_message(type='mlat_event', event='disconnected')

    def send_clock_reset(self):
        self.send_message(type='mlat_event', event='clock_reset')

    def send_udp_report(self, count):
        self.send_message(type='mlat_udp_report', messages_sent=str(count))


class AdeptConnection:
    UDP_REPORT_INTERVAL = 60.0

    def __init__(self, udp_transport=None):
        self.reader = None
        self.writer = None
        self.coordinator = None
        self.closed = False
        self.udp_transport = udp_transport
        self.state = 'init'

    def start(self, coordinator):
        self.coordinator = coordinator

        self.reader = AdeptReader(self, coordinator)
        self.writer = AdeptWriter(self)

        if self.udp_transport:
            self.udp_transport.start()
            self.send_mlat = self.udp_transport.send_mlat
            self.send_sync = self.udp_transport.send_sync
        else:
            self.send_mlat = self.writer.send_mlat
            self.send_sync = self.writer.send_sync

        self.send_split_sync = None
        self.send_seen = self.writer.send_seen
        self.send_lost = self.writer.send_lost
        self.send_rate_report = self.writer.send_rate_report
        self.send_clock_reset = self.writer.send_clock_reset
        self.send_input_connected = self.writer.send_input_connected
        self.send_input_disconnected = self.writer.send_input_disconnected

        self.state = 'connected'
        self.writer.send_ready()
        self.next_udp_report = util.monotonic_time() + self.UDP_REPORT_INTERVAL
        self.coordinator.server_connected()

    def disconnect(self, why=None):
        if not self.closed:
            self.closed = True
            self.state = 'closed'
            if self.reader:
                self.reader.close()
            if self.writer:
                self.writer.close()
            if self.udp_transport:
                self.udp_transport.close()
            if self.coordinator:
                self.coordinator.server_disconnected()

    def heartbeat(self, now):
        if self.udp_transport:
            self.udp_transport.flush()

            if now > self.next_udp_report:
                self.next_udp_report = now + self.UDP_REPORT_INTERVAL
                self.writer.send_udp_report(self.udp_transport.count)
