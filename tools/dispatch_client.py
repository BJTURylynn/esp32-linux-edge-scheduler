"""Bounded client for the C dispatcher. No task computation or automatic retry."""
import json
import math
import re
import socket
import time


class DispatchError(RuntimeError):
    def __init__(self, message, receipts=None):
        super().__init__(message)
        self.receipts = receipts


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}..{high}')
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('non-finite JSON constant')


def finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite JSON number')
    return result


def decode(line):
    result = json.loads(line.decode('ascii'), object_pairs_hook=unique_object,
                        parse_constant=reject_constant, parse_float=finite_float)
    if not isinstance(result, dict):
        raise ValueError('expected JSON object')
    return result


def batch_parameters(body):
    allowed = {'priority', 'seed', 'count', 'window', 'passes', 'batch_size', 'run_id'}
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError('unsupported task fields')
    priority = body.get('priority', 'normal')
    if priority not in ('normal', 'urgent'):
        raise ValueError('priority must be normal or urgent')
    result = dict(priority=priority,
                seed=integer(body.get('seed', 0), 0, 4294967295, 'seed'),
                count=integer(body.get('count', 16 if priority == 'urgent' else 256), 1, 256, 'count'),
                window=integer(body.get('window', 8), 1, 16, 'window'),
                passes=integer(body.get('passes', 100), 1, 1000, 'passes'),
                batch_size=integer(body.get('batch_size', 1), 1, 32, 'batch_size'))
    if 'run_id' in body:
        result['run_id'] = run_identity(body['run_id'])
    return result


def run_identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{16}', value):
        raise ValueError('run_id must be 16 lowercase hexadecimal characters')
    return value


class DispatchClient:
    def __init__(self, host='127.0.0.1', port=9100, timeout=3,
                 socket_factory=socket.create_connection):
        if not isinstance(host, str) or not host or len(host) > 255:
            raise ValueError('invalid dispatcher host')
        integer(port, 1, 65535, 'port')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError('timeout must be finite and within 0..30 seconds')
        self.host, self.port, self.timeout = host, port, timeout
        self.socket_factory = socket_factory

    def request(self, command):
        payload = json.dumps(command, separators=(',', ':'), allow_nan=False).encode('ascii') + b'\n'
        if len(payload) > 513:
            raise ValueError('management request too long')
        try:
            deadline = time.monotonic() + self.timeout
            with self.socket_factory((self.host, self.port), timeout=self.timeout) as channel:
                channel.settimeout(max(.001, deadline - time.monotonic()))
                channel.sendall(payload)
                data = bytearray()
                # Management connections contain one response and then EOF.
                # Read through EOF so trailing bytes cannot escape validation
                # merely by arriving in a later TCP segment.
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('dispatcher request deadline')
                    channel.settimeout(remaining)
                    chunk = channel.recv(min(4096, 1048577 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > 1048576:
                        raise DispatchError('中央响应超过大小限制。')
                if b'\n' not in data:
                    raise DispatchError('中央服务未返回完整结果；任务可能已收到，请先刷新状态。')
                line, extra = bytes(data).split(b'\n', 1)
                if extra.strip():
                    raise DispatchError('中央响应包含多余内容。')
                result = decode(line.removesuffix(b'\r'))
                if type(result.get('ok')) is not bool:
                    raise DispatchError('中央响应格式无效。')
                return result
        except DispatchError:
            raise
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise DispatchError('中央服务不可达或响应无效；请刷新状态，勿盲目重复提交。') from None

    def state(self):
        result = self.request({'v': 2, 'type': 'state'})
        if not result['ok']:
            raise DispatchError('中央服务未能返回状态。')
        if not isinstance(result.get('nodes'), list) or not isinstance(result.get('tasks'), list):
            raise DispatchError('中央状态缺少设备或任务列表。')
        try:
            run_identity(result.get('run_id'))
        except ValueError:
            raise DispatchError('中央状态缺少有效运行标记。') from None
        return result

    def submit(self, body):
        parameters = batch_parameters(body)
        size = parameters.pop('batch_size')
        parameters['run_id'] = parameters.get('run_id') or self.state()['run_id']
        seed = parameters['seed']
        receipts = {'ok': True, 'accepted': [], 'rejected': []}
        for index in range(size):
            parameters['seed'] = (seed + index) % 4294967296
            try:
                result = self.request(dict(v=2, type='submit', **parameters))
                if result['ok']:
                    integer(result.get('task_id'), 1, 4294967295, 'task_id')
                    receipts['accepted'].append({'index': index, 'task_id': result['task_id']})
                else:
                    error = result.get('error')
                    receipts['rejected'].append({'index': index, 'error': error if isinstance(error, str) else 'rejected'})
            except (DispatchError, ValueError) as exc:
                receipts['ok'] = False
                receipts['unknown_index'] = index
                receipts['not_submitted'] = list(range(index + 1, size))
                raise DispatchError(str(exc), receipts) from None
        return receipts

    def retry(self, task_id, run_id=None):
        integer(task_id, 1, 4294967295, 'task_id')
        identity = run_identity(run_id) if run_id is not None else self.state()['run_id']
        result = self.request({'v': 2, 'type': 'retry', 'id': task_id, 'run_id': identity})
        if result['ok']:
            integer(result.get('task_id'), 1, 4294967295, 'task_id')
        return result
