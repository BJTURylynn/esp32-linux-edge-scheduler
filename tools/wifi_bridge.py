"""Pull ESP32-C3 NDJSON over Wi-Fi/TCP and forward it to the Linux service.

Both sockets are outgoing connections: no Windows inbound firewall rule needed.
USB is not used here. sendall is not an application-level acknowledgement.
"""
import argparse
import json
from pathlib import Path
import socket
import time

from .serial_bridge import LineFramer, validated_frame


def run_bridge(device_host, output, *, device_port=9001, linux_host='127.0.0.1',
               linux_port=9000, count=20, timeout=60):
    if not 1 <= count <= 100 or not 0 < timeout <= 600:
        raise ValueError('count must be 1..100 and timeout 0..600 seconds')
    if not device_host or not 1 <= device_port <= 65535 or not 1 <= linux_port <= 65535:
        raise ValueError('valid hosts and ports are required')
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=False)
    stats = dict(device_host=device_host, device_port=device_port,
                 linux_host=linux_host, linux_port=linux_port,
                 requested=count, forwarded=0, received_bytes=0, invalid=0,
                 oversized=0, incomplete=0, uncertain=0, eof=False,
                 success=False, error=None,
                 semantics='forwarded means sendall completed, NOT Linux task completion')
    framer = LineFramer(8192)
    started = time.monotonic()
    deadline = started + timeout
    try:
        with (directory / 'frames.jsonl').open('wb') as capture:
            with socket.create_connection((linux_host, linux_port), timeout=min(5, timeout)) as downstream:
                with socket.create_connection((device_host, device_port), timeout=min(5, timeout)) as upstream:
                    upstream.sendall(f'START {count}\n'.encode('ascii'))
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('total bridge deadline reached')
                        upstream.settimeout(min(5, remaining))
                        try:
                            chunk = upstream.recv(4096)
                        except socket.timeout:
                            continue
                        if not chunk:
                            stats['eof'] = True
                            break
                        stats['received_bytes'] += len(chunk)
                        for line in framer.feed(chunk):
                            try:
                                frame = validated_frame(line)
                            except (UnicodeError, ValueError, TypeError, OverflowError):
                                stats['invalid'] += 1
                                raise ValueError('invalid device frame; session stopped') from None
                            if stats['forwarded'] >= count:
                                raise ValueError('device sent more frames than requested')
                            downstream.settimeout(max(0.001, min(5, deadline-time.monotonic())))
                            try:
                                downstream.sendall(frame)
                            except OSError:
                                stats['uncertain'] += 1
                                raise
                            capture.write(frame)
                            capture.flush()
                            stats['forwarded'] += 1
                    downstream.shutdown(socket.SHUT_WR)
    except (OSError, ValueError) as error:
        stats['error'] = str(error)
    finally:
        stats['oversized'] = framer.oversized
        stats['incomplete'] = framer.finish()
        stats['elapsed_seconds'] = round(time.monotonic()-started, 3)
        stats['success'] = (stats['error'] is None and stats['eof'] and
                            stats['forwarded'] == count and not stats['oversized'] and
                            not stats['incomplete'] and not stats['invalid'])
        (directory / 'stats.json').write_text(json.dumps(stats, indent=2)+'\n', encoding='utf-8')
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device-host', required=True)
    parser.add_argument('--device-port', type=int, default=9001)
    parser.add_argument('--linux-host', default='127.0.0.1')
    parser.add_argument('--linux-port', type=int, default=9000)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--count', type=int, default=20)
    parser.add_argument('--timeout', type=float, default=60)
    args = vars(parser.parse_args())
    result = run_bridge(**args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
