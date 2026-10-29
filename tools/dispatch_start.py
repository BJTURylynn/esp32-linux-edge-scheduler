"""Start the owned C dispatcher, hardware relays and local web workbench.

ESP32 Wi-Fi must be configured first. Foreground lifetime: Ctrl+C, STOP or stdin
EOF stops only this invocation's service/relays and archives C output on D:.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time

from .dispatch_client import DispatchClient, DispatchError
from .dispatch_gateway import run_gateway
from .experiment import free_port
from .project_demo import read_events, wait_event, windows_to_wsl


ROOT = Path(__file__).resolve().parents[1]


def validate(args):
    if ipaddress.ip_address(args.esp_host).version != 4:
        raise ValueError('ESP32 IPv4 required')
    if not isinstance(args.uno_port, str) or not 1 <= len(args.uno_port) <= 64:
        raise ValueError('actual Uno serial port required')
    if args.policy not in ('fifo', 'priority'):
        raise ValueError('invalid policy')
    if type(args.web_port) is not int or not 1 <= args.web_port <= 65535:
        raise ValueError('invalid web port')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', args.output.name):
        raise ValueError('output name must be 1..64 ASCII letters/digits/hyphens/underscores')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', args.distribution):
        raise ValueError('invalid WSL distribution')


def supervisor_command(args, root, port):
    prefix = [sys.executable, '-u']
    archive = str(args.output/'service')
    if os.name == 'nt':
        prefix = ['wsl.exe', '--distribution', args.distribution,
                  '--cd', windows_to_wsl(root), '--', 'python3', '-u']
        archive = windows_to_wsl(args.output/'service')
    return prefix + ['-m', 'tools.dispatch_service', '--name', args.output.name,
                     '--copy-to', archive, '--port', str(port), '--policy', args.policy]


def run(args, *, stop=None, gateway_runner=run_gateway,
        command_factory=supervisor_command, control=None):
    from .web_agent import WebApp, make_server
    validate(args)
    args.output = args.output.resolve()
    if args.output.exists():
        raise FileExistsError('choose a new output directory')
    port = free_port()
    app = WebApp(getattr(args, 'status', None) or ROOT/'results/live/status.json',
                 getattr(args, 'report', None) or ROOT/'results/comparison/report.json',
                 api_key=os.environ.pop('EDGE_AGENT_API_KEY', None),
                 dispatch_host='127.0.0.1', dispatch_port=port)
    # Reserve web port before creating output or launching any service/hardware.
    web = make_server(app, port=args.web_port)
    try:
        args.output.mkdir(parents=True, exist_ok=False)
    except OSError:
        web.server_close()
        raise
    stop = stop or threading.Event()
    result = dict(success=False, error=None, stage='starting', central_port=port,
                  web_url=f'http://127.0.0.1:{args.web_port}/#dispatch', hardware_validated=False,
                  semantics='startup/lifecycle only; inspect tasks and verify numerical results')
    process = None
    threads = []
    reader = None
    web_thread = None
    events = queue.Queue(maxsize=8)
    failures = queue.Queue()
    prior = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            prior[sig] = signal.signal(sig, lambda *unused: stop.set())

    def watch_owner():
        selected = sys.stdin if control is None else control
        try:
            for line in selected:
                if line.strip() == 'STOP':
                    stop.set()
                    return
            stop.set()
        except (OSError, ValueError):
            stop.set()

    def relay(device, **values):
        try:
            gateway_runner(device, args.output/('gateway-uno' if device == 'uno-01' else 'gateway-esp32'),
                           central_port=port, stop=stop, **values)
        except Exception as exc:
            failures.put(f'{device} gateway stopped: {type(exc).__name__}')

    try:
        with (args.output/'supervisor.log').open('w', encoding='utf-8') as log, \
             (args.output/'supervisor-stderr.log').open('wb') as errors:
            try:
                process = subprocess.Popen(command_factory(args, ROOT, port), cwd=ROOT,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                    text=True, encoding='utf-8', errors='replace', bufsize=1)
                reader = threading.Thread(target=read_events, args=(process.stdout, events, log), daemon=True)
                reader.start()
                ready = wait_event(process, events, 'ready', 80)
                if ready.get('port') != port:
                    raise RuntimeError('dispatcher readiness port mismatch')
                deadline = time.monotonic() + 8
                while True:
                    try:
                        state = DispatchClient(port=port, timeout=.5).state()
                        if state.get('run_id') != ready.get('run_id'):
                            raise RuntimeError('wrong dispatcher at forwarded port')
                        break
                    except DispatchError:
                        if time.monotonic() > deadline:
                            raise TimeoutError('Windows localhost forwarding unavailable')
                        time.sleep(.1)
                for device, values in [('uno-01', dict(serial_port=args.uno_port)),
                                       ('esp32c3-01', dict(esp_host=args.esp_host))]:
                    thread = threading.Thread(target=relay, args=(device,), kwargs=values, daemon=True)
                    thread.start()
                    threads.append(thread)
                web_thread = threading.Thread(target=web.serve_forever, daemon=True)
                web_thread.start()
                threading.Thread(target=watch_owner, daemon=True).start()
                result['stage'] = 'running'
                print(json.dumps(dict(event='ready', web_url=result['web_url'], central_port=port,
                                      run_id=ready.get('run_id'))), flush=True)
                while not stop.wait(.2):
                    if process.poll() is not None:
                        raise RuntimeError('owned dispatcher supervisor stopped')
                    if not failures.empty():
                        raise RuntimeError(failures.get())
                result['last_live_state'] = DispatchClient(port=port).state()
                result['success'] = True
                result['stage'] = 'stopped'
            except (OSError, ValueError, RuntimeError, DispatchError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
                result['error'] = type(exc).__name__ + ': ' + str(exc)
            finally:
                stop.set()
                if web_thread:
                    web.shutdown()
                    web_thread.join(3)
                web.server_close()
                for thread in threads:
                    thread.join(8)
                    if thread.is_alive():
                        result['success'] = False
                        result['error'] = result['error'] or 'gateway shutdown timed out'
                if process is not None:
                    if process.poll() is None:
                        try:
                            process.stdin.write('STOP\n')
                            process.stdin.flush()
                        except (OSError, ValueError):
                            pass
                    try:
                        process.stdin.close()
                        process.wait(timeout=45)
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill()
                        process.wait(timeout=5)
                        result['success'] = False
                        result['error'] = result['error'] or 'supervisor forced termination'
                    if process.returncode:
                        result['success'] = False
                        result['error'] = result['error'] or f'supervisor exit {process.returncode}'
                    if reader:
                        reader.join(2)
                    process.stdout.close()
    finally:
        web.server_close()
        for sig, handler in prior.items():
            signal.signal(sig, handler)
        (args.output/'startup.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--esp-host', required=True)
    p.add_argument('--uno-port', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--policy', choices=('fifo', 'priority'), default='priority')
    p.add_argument('--distribution', default='Ubuntu-24.04')
    p.add_argument('--web-port', type=int, default=8765)
    p.add_argument('--status', type=Path, help='optional legacy analysis status, separate from dispatcher')
    p.add_argument('--report', type=Path, help='optional legacy analysis comparison report')
    args = p.parse_args()
    try:
        result = run(args)
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
