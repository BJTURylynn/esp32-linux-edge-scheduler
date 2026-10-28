"""Run both hardware sources, an owned Linux C service, verification and audit.

Windows uses WSL; Linux can use the same entry directly. ESP32 must already be
configured on the same LAN. No Wi-Fi credentials or firewall settings are changed.
"""
import argparse
import ipaddress
import json
import math
import os
from pathlib import Path, PureWindowsPath
import queue
import re
import socket
import subprocess
import sys
import threading
import time

from .dual_verify import verify
from .experiment import free_port
from .schedule_audit import audit


def windows_to_wsl(path):
    value = PureWindowsPath(str(path))
    if len(value.drive) != 2 or value.drive[1] != ':' or not value.is_absolute():
        raise ValueError('WSL demonstration needs an absolute local drive path, not a UNC path')
    return '/mnt/'+value.drive[0].lower()+'/'+('/'.join(value.parts[1:]))


def validate(args):
    if args.policy not in ('fifo', 'priority') or not 1 <= args.passes <= 1000000:
        raise ValueError('policy fifo|priority and passes 1..1000000 required')
    if not 1 <= args.count <= 100 or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 600:
        raise ValueError('count must be 1..100; timeout must be finite, 1..600 seconds')
    if not args.uno_port or len(args.uno_port) > 64:
        raise ValueError('a valid Uno serial port is required')
    if ipaddress.ip_address(args.esp_host).version != 4:
        raise ValueError('ESP32 IPv4 address required')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', args.output.name):
        raise ValueError('output folder name must be 1..64 ASCII letters/digits/hyphens/underscores')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', args.distribution):
        raise ValueError('invalid WSL distribution name')


def supervisor_command(args, root, port):
    output = args.output.resolve()/'service'
    prefix = [sys.executable, '-u']
    copy_to = str(output)
    if os.name == 'nt':
        prefix = ['wsl.exe', '--distribution', args.distribution,
                  '--cd', windows_to_wsl(root), '--', 'python3', '-u']
        copy_to = windows_to_wsl(output)
    return prefix+['-m', 'tools.demo_service', '--name', args.output.name,
                   '--copy-to', copy_to, '--port', str(port), '--policy', args.policy,
                   '--passes', str(args.passes), '--expected', str(args.count*2),
                   '--timeout', str(args.timeout)]


def read_events(stream, events, log):
    for line in stream:
        log.write(line)
        log.flush()
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get('event') in ('ready', 'finished'):
            events.put(value)
    events.put({'event': 'eof'})


def wait_event(process, events, wanted, timeout):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=min(.2, max(.001, deadline-time.monotonic())))
        except queue.Empty:
            # A reader thread can lag slightly behind process exit; continue
            # until the bounded deadline rather than dropping its final event.
            continue
        if event['event'] == 'eof':
            raise RuntimeError('supervisor closed before '+wanted+'; see supervisor-stderr.log')
        if event['event'] == wanted:
            return event
        if event['event'] == 'finished':
            raise RuntimeError(event.get('error') or 'service ended before readiness')
    raise TimeoutError('supervisor '+wanted+' event timed out; see supervisor.log')


def wait_local_listener(port, timeout=5):
    # WSL's Linux listener can precede the Windows localhost proxy by a moment.
    deadline = time.monotonic()+timeout
    while True:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=.3):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError('Windows/Linux localhost forwarding did not become ready')
            time.sleep(.05)


def collect(args, root, port):
    command = [sys.executable, '-m', 'tools.dual_demo', '--serial-port', args.uno_port,
               '--device-host', args.esp_host, '--count', str(args.count),
               '--duration', str(min(args.timeout, 60)), '--port', str(port),
               '--output', str(args.output/'capture')]
    with (args.output/'capture.log').open('w', encoding='utf-8') as log:
        return subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                              timeout=min(args.timeout, 60)+15).returncode


def write_result(directory, result):
    (directory/'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    verified = result.get('verification') or {}
    scheduling = result.get('audit') or {}
    lines = ['# 双设备一键演示结果', '',
             '验收：'+('通过' if result['success'] else '未通过'),
             '路径：ESP32 Wi-Fi + Arduino USB → Windows转发 → Linux C统一队列。',
             '所有样本均为synthetic合成数据。', '',
             f"- 完成任务：{verified.get('completed', '尚未验证')}",
             f"- 拒绝任务：{verified.get('rejected', '尚未验证')}",
             f"- 数值参考验证：{verified.get('verified', False)}",
             f"- 调度规则审计通过：{scheduling.get('policy_ok', False)}",
             f"- 实际观察到紧急优先取出：{scheduling.get('priority_behavior_observed', False)}",
             f"- 紧急优先选择次数：{scheduling.get('urgent_overtake_decisions', 0)}", '',
             '调度规则通过不代表本次一定出现竞争；未观察到紧急优先行为时，不作性能提升结论。',
             '已开始的计算不被抢占，优先级只决定下一个任务。',
             'CSV时间从Linux完整帧接收开始，不是设备端到端延迟。',
             '本命令会停止自己启动的C服务并归档结果，不停止其他服务。']
    if result.get('error'):
        lines.extend(['', '失败原因：'+result['error']])
    (directory/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def run_demo(args, *, capture_runner=collect, command_factory=supervisor_command):
    validate(args)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    port = free_port()
    result = {'success': False, 'policy': args.policy, 'passes': args.passes,
              'requested_each': args.count, 'port': port, 'error': None,
              'verification': None, 'audit': None, 'service': None,
              'data_source': 'synthetic', 'stage': 'starting'}
    process = None
    reader = None
    events = queue.Queue(maxsize=8)
    with (args.output/'supervisor.log').open('w', encoding='utf-8') as log, \
         (args.output/'supervisor-stderr.log').open('wb') as errors:
        try:
            command = command_factory(args, root, port)
            process = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=errors,
                                       text=True, encoding='utf-8', errors='replace', bufsize=1)
            reader = threading.Thread(target=read_events,
                                      args=(process.stdout, events, log), daemon=True)
            reader.start()
            ready = wait_event(process, events, 'ready', 45)
            if ready.get('port') != port:
                raise RuntimeError('supervisor port differs from requested port')
            # Linux readiness is not sufficient proof of Windows localhost forwarding.
            wait_local_listener(port)
            result['stage'] = 'capture'
            if capture_runner(args, root, port) != 0:
                raise RuntimeError('hardware capture failed; see capture.log and capture/*/stats.json')
            result['stage'] = 'completion'
            process.stdin.write('FINISH\n')
            process.stdin.flush()
            final = wait_event(process, events, 'finished', args.timeout+25)
            result['service'] = final
            if not final.get('success'):
                raise RuntimeError(final.get('error') or 'Linux task totals did not pass')
            result['stage'] = 'verify'
            result['verification'] = verify(args.output/'capture', args.output/'service')
            result['audit'] = audit(args.output/'service/tasks.csv', args.policy)
            if not result['audit']['policy_ok']:
                raise RuntimeError('scheduling policy audit failed')
            result['success'] = True
            result['stage'] = 'complete'
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            result['error'] = type(exc).__name__+': '+str(exc)
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        process.stdin.write('STOP\n')
                        process.stdin.flush()
                    except (OSError, ValueError):
                        pass
                try:
                    process.stdin.close()
                    process.wait(timeout=30)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    process.kill()
                    process.wait(timeout=5)
                    result['success'] = False
                    result['error'] = result['error'] or 'supervisor cleanup: '+str(exc)
                if process.returncode and result['success']:
                    result['success'] = False
                    result['error'] = f'supervisor exited with {process.returncode}'
                if reader:
                    reader.join(timeout=2)
                if process.stdout:
                    process.stdout.close()
    write_result(args.output, result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--esp-host', required=True)
    p.add_argument('--uno-port', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--policy', choices=('fifo', 'priority'), default='priority')
    p.add_argument('--passes', type=int, default=1000000)
    p.add_argument('--count', type=int, default=20)
    p.add_argument('--timeout', type=float, default=180)
    p.add_argument('--distribution', default='Ubuntu-24.04')
    args = p.parse_args()
    try:
        result = run_demo(args)
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
