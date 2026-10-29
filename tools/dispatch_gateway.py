"""Bidirectional device relay. It neither selects devices nor computes tasks."""
import argparse
import json
import queue
import socket
import threading
import time
from pathlib import Path

from .dispatch_client import decode
from .serial_bridge import LineFramer


class Endpoint:
    def __init__(self, channel, serial=False):
        self.channel, self.serial = channel, serial

    def read(self):
        if self.serial:
            return self.channel.read(min(512, max(1, self.channel.in_waiting)))
        return self.channel.recv(512)

    def write(self, data):
        if not self.serial:
            self.channel.sendall(data)
            return
        offset = 0
        deadline = time.monotonic() + 3
        while offset < len(data):
            if time.monotonic() >= deadline:
                raise TimeoutError('serial write deadline')
            written = self.channel.write(data[offset:])
            if not written:
                raise OSError('serial write made no progress')
            offset += written

    def close(self):
        try:
            self.channel.close()
        except OSError:
            pass


def packet(line, direction, device):
    """A relay envelope whitelist excludes serial diagnostics and credentials."""
    value = decode(line)
    allowed = {'device': {'hello', 'accept', 'result', 'error', 'pong'},
               'central': {'welcome', 'task', 'ping'}}
    if value.get('v') != 2 or value.get('type') not in allowed[direction]:
        raise ValueError('unexpected v2 relay message')
    if any(isinstance(item, (dict, list)) for item in value.values()):
        raise ValueError('worker messages must be flat')
    if direction == 'device' and value.get('device') != device:
        raise ValueError('wrong device identity')
    data = json.dumps(value, separators=(',', ':'), allow_nan=False).encode('ascii') + b'\n'
    if len(data) > 257:
        raise ValueError('worker frame exceeds 256 bytes')
    return value, data


def relay_session(device_endpoint, central_endpoint, device, stop, emit):
    """Bounded readers work for Windows COM ports as well as sockets."""
    events = queue.Queue(maxsize=64)
    done = threading.Event()
    overflow = threading.Event()

    def reader(name, endpoint):
        framer = LineFramer(256)
        try:
            while not done.is_set() and not stop.is_set():
                try:
                    data = endpoint.read()
                except (socket.timeout, TimeoutError):
                    continue
                if not data:
                    if endpoint.serial:
                        continue
                    raise EOFError('connection closed')
                for line in framer.feed(data):
                    events.put_nowait((name, line))
                if framer.oversized:
                    raise ValueError('oversized worker frame')
        except queue.Full:
            overflow.set()
        except (OSError, ValueError, EOFError) as exc:
            try:
                events.put_nowait(('failure', f'{name}: {type(exc).__name__}'))
            except queue.Full:
                overflow.set()

    readers = [threading.Thread(target=reader, args=(name, endpoint), daemon=True)
               for name, endpoint in (('device', device_endpoint), ('central', central_endpoint))]
    for thread in readers:
        thread.start()
    welcomed = False
    sent_hello = False
    handshake_deadline = time.monotonic() + 12
    next_probe = 0
    try:
        while not stop.is_set():
            if overflow.is_set():
                raise ValueError('relay event buffer full')
            now = time.monotonic()
            if not welcomed and now >= handshake_deadline:
                raise TimeoutError('device handshake timed out')
            if device_endpoint.serial and not sent_hello and now >= next_probe:
                device_endpoint.write(b'{"v":2,"type":"probe"}\n')
                next_probe = now + 1
            try:
                direction, raw = events.get(timeout=.1)
            except queue.Empty:
                continue
            if direction == 'failure':
                raise OSError(raw)
            value, data = packet(raw, direction, device)
            if direction == 'device' and value['type'] == 'hello':
                # Periodic pre-WELCOME hello can already be in the UART buffer.
                if sent_hello:
                    continue
                sent_hello = True
            if direction == 'central' and value['type'] == 'welcome':
                welcomed = True
            if not sent_hello or (not welcomed and value['type'] not in ('hello', 'welcome')):
                raise ValueError('message before handshake')
            target = central_endpoint if direction == 'device' else device_endpoint
            target.write(data)
            emit(direction, value)
    finally:
        done.set()
        device_endpoint.close()
        central_endpoint.close()
        for thread in readers:
            thread.join(timeout=1)


def run_gateway(device, output, *, central_host='127.0.0.1', central_port=9100,
                esp_host=None, esp_port=9001, serial_port=None, stop=None,
                serial_factory=None, socket_factory=socket.create_connection):
    if device not in ('uno-01', 'esp32c3-01'):
        raise ValueError('unknown device')
    if device == 'uno-01' and not serial_port:
        raise ValueError('Uno serial port required')
    if device == 'esp32c3-01' and not esp_host:
        raise ValueError('ESP32 host required')
    for port in (central_port, esp_port):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('invalid TCP port')
    stop = stop or threading.Event()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    stats = dict(device=device, connections=0, forwarded_device=0, forwarded_central=0,
                 errors=0, running=True, last_error=None,
                 semantics='forwarded messages are NOT proof of task completion')
    with (output/'frames.jsonl').open('w', encoding='utf-8') as log:
        def save():
            tmp = output/'stats.json.tmp'
            tmp.write_text(json.dumps(stats, indent=2)+'\n', encoding='utf-8')
            tmp.replace(output/'stats.json')

        def emit(direction, value):
            stats['forwarded_'+direction] += 1
            log.write(json.dumps(dict(direction=direction, message=value,
                                      monotonic_ns=time.monotonic_ns()), separators=(',', ':'))+'\n')
            log.flush()
            save()
        save()
        try:
            while not stop.is_set():
                dev = central = None
                try:
                    raw_central = socket_factory((central_host, central_port), timeout=3)
                    raw_central.settimeout(.2)
                    central = Endpoint(raw_central)
                    if device == 'uno-01':
                        if serial_factory is None:
                            import serial
                            serial_factory = serial.Serial
                        dev = Endpoint(serial_factory(serial_port, baudrate=115200,
                                                      timeout=.2, write_timeout=3), serial=True)
                        # Opening Uno normally resets it. Give its bootloader time;
                        # startup hello is also solicited, so no buffers are trusted.
                        if stop.wait(2):
                            break
                    else:
                        raw_dev = socket_factory((esp_host, esp_port), timeout=3)
                        raw_dev.settimeout(.2)
                        dev = Endpoint(raw_dev)
                    stats['connections'] += 1
                    save()
                    relay_session(dev, central, device, stop, emit)
                except (OSError, ValueError, EOFError, TimeoutError, ImportError):
                    stats['errors'] += 1
                    stats['last_error'] = 'link unavailable or protocol invalid; reconnecting without replay'
                    save()
                finally:
                    if dev:
                        dev.close()
                    if central:
                        central.close()
                stop.wait(1)
        finally:
            stats['running'] = False
            save()
    return stats


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--device', choices=('uno-01', 'esp32c3-01'), required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--central-host', default='127.0.0.1')
    p.add_argument('--central-port', type=int, default=9100)
    p.add_argument('--esp-host')
    p.add_argument('--esp-port', type=int, default=9001)
    p.add_argument('--serial-port')
    args = p.parse_args()
    event = threading.Event()
    import signal
    signal.signal(signal.SIGINT, lambda *unused: event.set())
    signal.signal(signal.SIGTERM, lambda *unused: event.set())
    try:
        run_gateway(**vars(args), stop=event)
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
