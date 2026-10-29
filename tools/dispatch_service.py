"""Own a Linux dispatcher child until STOP/EOF; archive only after stopping it."""
import argparse
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

from .demo_service import child_guard, emit
from .dispatch_client import DispatchClient, DispatchError


def supervise(name, copy_to, *, port, policy='priority', base=None, server=None,
              control=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('invalid run name')
    if type(port) is not int or not 1 <= port <= 65535 or policy not in ('fifo', 'priority'):
        raise ValueError('invalid dispatcher configuration')
    copy_to = Path(copy_to)
    if copy_to.exists():
        raise FileExistsError('archive destination already exists')
    base = Path(base) if base else Path.home()/'edge-dispatch-results'
    base.mkdir(parents=True, exist_ok=True)
    work = base/name
    work.mkdir(exist_ok=False)
    native = work/'service'
    root = Path(__file__).resolve().parents[1]
    result = dict(success=False, error=None, native_output=str(native), port=port,
                  hardware_validated=False, semantics='lifecycle result, not hardware/task validation')
    process = None
    selector = selectors.DefaultSelector()
    stopped = False
    previous = {}
    def stop_signal(*unused):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, stop_signal)
    try:
        if server is None:
            with (work/'build.log').open('w', encoding='utf-8') as log:
                subprocess.run(['make', '-C', str(root/'linux'), 'all'],
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
            server = root/'linux/build/edge_dispatcher'
        command = [str(server), '--bind', '0.0.0.0', '--port', str(port),
                   '--policy', policy, '--capacity', '64', '--output', str(native)]
        with (work/'server.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       preexec_fn=child_guard() if sys.platform == 'linux' else None)
            client = DispatchClient(port=port, timeout=.3)
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None:
                    raise RuntimeError('dispatcher exited before readiness')
                try:
                    ready = client.state()
                    break
                except DispatchError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('dispatcher readiness timed out')
                    time.sleep(.05)
            emit('ready', port=port, native_output=str(native), run_id=ready.get('run_id'))
            control = sys.stdin.buffer if control is None else control
            selector.register(control, selectors.EVENT_READ)
            pending = b''
            while not stopped:
                if process.poll() is not None:
                    raise RuntimeError('dispatcher stopped unexpectedly')
                for key, _ in selector.select(.2):
                    chunk = os.read(key.fd, 128)
                    if not chunk:
                        stopped = True
                        break
                    pending += chunk
                    if len(pending) > 128:
                        raise ValueError('oversized owner control')
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        if line.removesuffix(b'\r') != b'STOP':
                            raise ValueError('unknown owner control')
                        stopped = True
            result['last_live_state'] = client.state()
    except (OSError, ValueError, RuntimeError, DispatchError, subprocess.SubprocessError) as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                result['error'] = result['error'] or 'dispatcher required forced termination'
            if process.returncode != 0:
                result['error'] = result['error'] or f'dispatcher exit {process.returncode}'
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        result['success'] = result['error'] is None and stopped and process is not None
        try:
            native.mkdir(exist_ok=True)
            (native/'supervisor.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
            for filename in ('build.log', 'server.log'):
                if (work/filename).exists():
                    shutil.copy2(work/filename, native/filename)
            shutil.copytree(native, copy_to)
        except OSError as exc:
            result['success'] = False
            result['error'] = 'archive failed: ' + str(exc)
        emit('finished', **result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name', required=True)
    p.add_argument('--copy-to', type=Path, required=True)
    p.add_argument('--port', type=int, required=True)
    p.add_argument('--policy', choices=('fifo', 'priority'), default='priority')
    p.add_argument('--base', type=Path)
    p.add_argument('--server', type=Path)
    args = p.parse_args()
    try:
        result = supervise(**vars(args))
    except (OSError, ValueError) as exc:
        emit('finished', success=False, error=str(exc))
        return 1
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
