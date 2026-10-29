"""Configure EdgeWiFi over USB; device task payloads are never read here.

Password comes from EDGE_WIFI_PASSWORD or an interactive hidden prompt.
It is not written to source code, command-line arguments or output logs.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--ssid', default='Shino')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--monitor-seconds', type=float, default=0)
    args = parser.parse_args()
    if not args.ssid or len(args.ssid.encode()) > 32 or '\n' in args.ssid or '\r' in args.ssid:
        parser.error('SSID must contain 1..32 UTF-8 bytes, no CR/LF')
    if not 0 <= args.monitor_seconds <= 600:
        parser.error('monitor-seconds must be 0..600')
    password = os.environ.get('EDGE_WIFI_PASSWORD')
    if password is None:
        password = getpass.getpass('Wi-Fi password (hidden): ')
    if not 8 <= len(password.encode()) <= 63 or '\n' in password or '\r' in password:
        parser.error('password must contain 8..63 UTF-8 bytes, no CR/LF')
    import serial
    args.output.mkdir(parents=True, exist_ok=False)
    ready = None
    online = False
    with (args.output/'serial.log').open('x', encoding='utf-8') as log:
        with serial.Serial(args.port, 115200, timeout=0.5, write_timeout=3) as channel:
            channel.dtr = False
            channel.rts = False
            time.sleep(2)
            channel.reset_input_buffer()
            channel.write(b'CONFIG\n')
            deadline = time.monotonic()+65
            while time.monotonic() < deadline:
                line = channel.readline(512).decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                if line == 'CONFIG_SSID':
                    channel.write((args.ssid+'\n').encode())
                elif line == 'CONFIG_PASSWORD':
                    channel.write((password+'\n').encode())
                    password = ''
                else:
                    # Firmware diagnostic lines contain no credential echo.
                    print(line, flush=True)
                    log.write(line+'\n')
                    log.flush()
                    if line.startswith('WIFI_READY '):
                        ready = dict(re.findall(r'(\w+)=([^ ]+)', line))
                        online = True
                        deadline = time.monotonic()+args.monitor_seconds
                    elif line.startswith(('WIFI_LOST', 'READY ')):
                        online = False
                    elif line.startswith(('WIFI_FAILED', 'CONFIG_FAILED', 'SERVER_FAILED')):
                        online = False
                        break
    result = {'configured': ready is not None and online, 'online_at_exit':online, 'port':args.port,
              'device':ready, 'password_saved':False,
              'usb_role':'configuration and diagnostics only, no task data forwarding'}
    (args.output/'connection.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return 0 if ready and online else 1


if __name__ == '__main__':
    raise SystemExit(main())
