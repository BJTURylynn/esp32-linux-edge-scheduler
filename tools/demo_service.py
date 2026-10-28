"""Own a bounded Linux C-service lifetime for tools.project_demo.

stdout contains JSON events; stdin accepts FINISH after capture, or STOP/EOF to
cancel. C output stays in server.log. No pre-existing service is stopped.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time

from .experiment import stop, wait_ready


def emit(event, **values):
    print(json.dumps({'event': event, **values}), flush=True)


def child_guard():
    """Linux: a forcibly killed supervisor must not leave its C child alive."""
    parent = os.getpid()
    libc = ctypes.CDLL(None, use_errno=True)
    def guard():
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            os._exit(125)
        if os.getppid() != parent:
            os.kill(os.getpid(), signal.SIGTERM)
    return guard


def supervise(name, copy_to, *, port, policy, passes, expected, timeout,
              server=None, base=None, control=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('run name must be 1..64 ASCII letters/digits/hyphens/underscores')
    if (not 1 <= port <= 65535 or policy not in ('fifo', 'priority') or
        not 1 <= passes <= 1000000 or not 2 <= expected <= 200 or
        not 1 <= timeout <= 600):
        raise ValueError('invalid service bounds')
    copy_to = Path(copy_to)
    if copy_to.exists():
        raise FileExistsError('service copy destination already exists')
    base = Path(base) if base else Path.home()/'edge-results'
    base.mkdir(parents=True, exist_ok=True)
    output = base/name
    output.mkdir(exist_ok=False)
    repository = Path(__file__).resolve().parents[1]
    process = None
    error = None
    requested_finish = False
    final = None
    result = {'success': False, 'native_output': str(output), 'port': port,
              'expected': expected, 'policy': policy, 'error': None}
    selector = selectors.DefaultSelector()
    try:
        if server is None:
            with (output/'build.log').open('w', encoding='utf-8') as build:
                subprocess.run(['make', '-C', str(repository/'linux')],
                               stdout=build, stderr=subprocess.STDOUT, check=True, timeout=30)
            server = repository/'linux/build/edge_scheduler'
        command = [str(server), '--bind', '0.0.0.0', '--port', str(port),
                   '--policy', policy, '--capacity', str(max(64, expected)),
                   '--window', '8', '--passes', str(passes), '--output', str(output)]
        with (output/'server.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       preexec_fn=child_guard() if sys.platform == 'linux' else None)
            wait_ready(process, output/'status.json')
            ready_deadline = time.monotonic()+5
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() > ready_deadline:
                        raise RuntimeError('C listener did not become ready')
                    time.sleep(.02)
            emit('ready', port=port, native_output=str(output))
            control = sys.stdin.buffer if control is None else control
            selector.register(control, selectors.EVENT_READ)
            pending = b''
            deadline = time.monotonic()+timeout
            while True:
                if process.poll() is not None:
                    raise RuntimeError('C service exited before FINISH completed')
                state = json.loads((output/'status.json').read_text())
                if requested_finish and state['completed']+state['rejected'] == expected and not state['active'] and not state['queue_depth']:
                    break
                if state.get('fatal'):
                    raise RuntimeError('C service reported a fatal error')
                if time.monotonic() >= deadline:
                    raise TimeoutError('service completion deadline reached')
                for key, _ in selector.select(.1):
                    chunk = os.read(key.fd, 256)
                    if not chunk:
                        raise RuntimeError('controller closed before completion')
                    pending += chunk
                    if len(pending) > 128:
                        raise ValueError('oversized controller command')
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        # Windows TextIO pipes translate LF into CRLF. Only
                        # strip its framing CR; arbitrary whitespace is invalid.
                        if line.endswith(b'\r'):
                            line = line[:-1]
                        if line == b'FINISH':
                            requested_finish = True
                        elif line == b'STOP':
                            raise RuntimeError('capture cancelled by controller')
                        else:
                            raise ValueError('unknown controller command')
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        error = type(exc).__name__+': '+str(exc)
    finally:
        selector.close()
        if process is not None:
            try:
                stop(process)
            except (OSError, TimeoutError) as exc:
                error = error or 'cleanup: '+str(exc)
        state_path = output/'status.json'
        if state_path.exists():
            try:
                final = json.loads(state_path.read_text())
            except (ValueError, OSError) as exc:
                error = error or 'final status: '+str(exc)
        result['final_status'] = final
        result['success'] = (error is None and requested_finish and process is not None and
                             process.returncode == 0 and final is not None and
                             final['completed'] == expected and final['accepted'] == expected and
                             final['rejected'] == 0 and not final['running'] and not final.get('fatal'))
        result['error'] = error
        (output/'supervisor.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
        try:
            shutil.copytree(output, copy_to)
        except OSError as exc:
            result['success'] = False
            result['error'] = 'copy results: '+str(exc)
            (output/'supervisor.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
        emit('finished', **result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name', required=True)
    p.add_argument('--copy-to', type=Path, required=True)
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--policy', choices=('fifo', 'priority'), default='priority')
    p.add_argument('--passes', type=int, default=1000000)
    p.add_argument('--expected', type=int, required=True)
    p.add_argument('--timeout', type=float, default=180)
    p.add_argument('--base', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--server', type=Path, help=argparse.SUPPRESS)
    a = p.parse_args()
    try:
        result = supervise(a.name, a.copy_to, port=a.port, policy=a.policy,
                           passes=a.passes, expected=a.expected, timeout=a.timeout,
                           base=a.base, server=a.server)
    except (OSError, ValueError) as exc:
        emit('finished', success=False, error=str(exc))
        return 1
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
