"""Owned ESP Wi-Fi setup and relay lifecycle; credentials never enter records.

HTTP handlers enqueue bounded USB operations. GET snapshots do not open ports,
scan networks, reset a board, or infer Wi-Fi loss from a missing TCP handshake.
"""
from contextlib import contextmanager
import ipaddress
from pathlib import Path
import re
import threading
import time

from .dispatch_gateway import run_gateway


class WiFiError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def _address(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        address = ipaddress.IPv4Address(value)
        if (not address.is_private or address.is_loopback or address.is_multicast
                or address.is_unspecified or str(address) == '255.255.255.255'):
            raise ValueError
        return str(address)
    except (ValueError, ipaddress.AddressValueError):
        raise WiFiError(400, '请输入 ESP32 在本地网络中的有效 IPv4 地址。') from None


def _text(value, low, high, label):
    if (not isinstance(value, str) or not low <= len(value.encode('utf-8')) <= high
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise WiFiError(400, label)
    return value


class EspWiFiManager:
    def __init__(self, client, output, host=None, serial_port=None, uno_port=None,
                 gateway_runner=run_gateway, serial_factory=None, port_provider=None):
        self.client = client
        self.output = Path(output)
        self._host = _address(host) if host is not None else None
        self._port, self._uno_port = serial_port, uno_port
        self._gateway_runner = gateway_runner
        self._serial_factory, self._port_provider = serial_factory, port_provider
        self._lock = threading.RLock()
        self._closed, self._started = False, False
        self._cancel = threading.Event()
        self._thread = self._channel = self._gateway_thread = self._gateway_stop = None
        self._attempt, self._operation = 0, 0
        self._phase = 'idle'
        self._message = '尚未检测 Wi-Fi；可通过 USB 配网，或填写设备已有的 IP。'
        self._error = None
        self._observation = dict(state='unknown', source='not_observed', ip=None,
                                 rssi_dbm=None, observed_at=None)

    def snapshot(self):
        with self._lock:
            return dict(supported=True, phase=self._phase,
                        busy=self._busy(),
                        host=self._host, serial_port=self._port, message=self._message,
                        error_code=self._error, observation=dict(self._observation),
                        operation_id=self._operation, closed=self._closed)

    def ports(self):
        try:
            provider = self._port_provider
            if provider is None:
                from serial.tools.list_ports import comports
                provider = comports
            result = []
            for item in provider():
                port = item.get('port') if isinstance(item, dict) else getattr(item, 'device', None)
                description = item.get('description', '') if isinstance(item, dict) else getattr(item, 'description', '')
                if not isinstance(port, str) or not port or len(port) > 128 or port.casefold() == str(self._uno_port).casefold():
                    continue
                if 'bluetooth' in str(description).casefold() or '蓝牙' in str(description) or 'bthenum' in str(description).casefold():
                    continue
                result.append(dict(port=port, description=''.join(c for c in str(description) if ord(c) >= 32)[:160]))
            return dict(ports=result, serial_available=True)
        except (ImportError, OSError, ValueError):
            return dict(ports=[], serial_available=False)

    def _selected_port(self, value):
        _text(value, 1, 128, '请选择本机实际识别到的 ESP32 USB 串口。')
        found = self.ports()
        if not found['serial_available']:
            raise WiFiError(503, '串口支持不可用，请在运行工作台的 Python 环境安装 requirements-device.txt。')
        if value not in {item['port'] for item in found['ports']}:
            raise WiFiError(400, '该串口未被识别，或已用于 Uno。请插好 ESP32 数据线后刷新串口。')
        return value

    @contextmanager
    def task_guard(self):
        # Batch submission holds this lock until all receipts have been read.
        # Configuration acquires the same lock before taking its idle snapshot.
        with self._lock:
            if self._closed:
                raise WiFiError(503, '设备连接管理已停止，请重新启动工作台。')
            if self._busy():
                raise WiFiError(409, '正在检测或连接 ESP32，请等待操作结束后再提交任务。')
            yield

    def _busy(self):
        # An error may already be reported while its USB handle is still being
        # closed. Keep new operations and task mutations out until thread exit.
        return (self._phase in ('probing', 'connecting', 'attaching')
                or bool(self._thread and self._thread.is_alive()))

    def _check_idle(self):
        try:
            state = self.client.state()
        except Exception:
            raise WiFiError(503, '中央服务不可达，无法确认设备是否空闲，请先恢复中央服务。') from None
        nodes = state.get('nodes')
        if not isinstance(nodes, list):
            raise WiFiError(503, '中央设备状态无效，请刷新后重试。')
        node = next((item for item in nodes if item.get('device') == 'esp32c3-01'), None)
        # An offline ESP can be attached to drain an existing queue. Uno work
        # does not prevent this. Never stop an ESP already holding a task.
        if node and (node.get('task_id') is not None or node.get('state') in ('dispatching', 'running')):
            raise WiFiError(409, 'ESP32 正在执行任务，完成后才能更换网络或地址。')
        if any(item.get('device') == 'esp32c3-01' and item.get('state') in ('dispatching', 'running')
               for item in state.get('tasks', [])):
            raise WiFiError(409, 'ESP32 仍有在途任务，请等待完成。')
        queued = state.get('queue_depth', 0)
        if node and node.get('state') == 'idle' and queued:
            raise WiFiError(409, 'ESP32 已在线且中央仍有排队任务，请先等待队列处理完再更换连接。')
        if node and node.get('state') not in ('offline', 'idle', 'error'):
            raise WiFiError(503, '尚不能确认 ESP32 状态，请刷新后重试。')

    def _begin(self, phase, callback, *, check_idle=True, port=None):
        with self._lock:
            if self._closed:
                raise WiFiError(503, '设备连接管理已停止，请重新启动工作台。')
            if self._busy():
                raise WiFiError(409, '已有 Wi-Fi 操作正在进行，请稍后再试。')
            if check_idle:
                self._check_idle()
            self._operation += 1
            self._phase, self._error = phase, None
            self._message = '正在读取板端 Wi-Fi 状态…' if phase == 'probing' else '正在连接 ESP32，请稍候…'
            if port is not None:
                self._port = port
            self._cancel.clear()
            self._thread = threading.Thread(target=self._operate, args=(callback,), daemon=True)
            self._thread.start()
            return self.snapshot()

    def connect(self, body):
        if not isinstance(body, dict) or set(body) != {'port', 'ssid', 'password'}:
            raise WiFiError(400, 'USB 配网只接受串口、Wi-Fi 名称和密码。')
        port = self._selected_port(body['port'])
        ssid = _text(body['ssid'], 1, 32, 'Wi-Fi 名称需要 1～32 个 UTF-8 字节，不能包含换行。')
        password = _text(body['password'], 8, 63, 'Wi-Fi 密码需要 8～63 个 UTF-8 字节，不能包含换行。')
        return self._begin('connecting', lambda: self._configure(port, ssid, password), port=port)

    def probe(self, body):
        if not isinstance(body, dict) or set(body) != {'port'}:
            raise WiFiError(400, '状态检测只接受 ESP32 串口。')
        port = self._selected_port(body['port'])
        return self._begin('probing', lambda: self._probe(port), check_idle=False, port=port)

    def attach(self, body):
        if not isinstance(body, dict) or set(body) != {'host'}:
            raise WiFiError(400, '已有 IP 接入只接受 ESP32 的 IPv4 地址。')
        host = _address(body['host'])
        return self._begin('attaching', lambda: self._attach(host))

    def _operate(self, callback):
        try:
            callback()
        except WiFiError as error:
            with self._lock:
                if not self._closed:
                    self._phase, self._error, self._message = 'error', 'operation_failed', str(error)
        except Exception:
            # Exceptions may contain serial input or a secret; never expose or
            # write their text. No exception repr or USB transcript is retained.
            with self._lock:
                if not self._closed:
                    self._phase, self._error = 'error', 'connection_failed'
                    self._message = '无法访问 ESP32。请检查 USB 数据线、串口占用及 2.4GHz 网络后重试。'
        finally:
            self._close_channel()
            with self._lock:
                if not self._closed and self._phase in ('probing', 'connecting', 'attaching'):
                    self._phase = 'error' if self._error else 'idle'

    def _ready(self, message):
        with self._lock:
            if self._closed or self._cancel.is_set():
                return
            # The operation remains busy until its serial handle is closed in
            # _operate.finally; a new operation cannot race that cleanup.
            if not self._error:
                self._message = message

    def _observe(self, state, source, ip=None, rssi=None):
        with self._lock:
            if not self._closed:
                self._observation = dict(state=state, source=source, ip=ip,
                                         rssi_dbm=rssi, observed_at=time.time())

    def _open_channel(self, port):
        factory = self._serial_factory
        if factory is None:
            import serial
            factory = serial.Serial
        channel = factory(port=None, baudrate=115200, timeout=.2, write_timeout=3)
        try:
            channel.dtr = False
            channel.rts = False
            channel.port = port
            with self._lock:
                if self._closed or self._cancel.is_set():
                    raise WiFiError(503, '设备连接管理已停止。')
                self._channel = channel
            channel.open()
            # USB CDC does not need a reset to read status. Allow a freshly
            # powered board to reach its control loop without toggling DTR.
            if self._cancel.wait(.25):
                raise WiFiError(503, '设备连接管理已停止。')
            channel.reset_input_buffer()
            return channel
        except Exception:
            self._close_channel()
            try:
                channel.close()
            except Exception:
                pass
            raise

    def _close_channel(self):
        with self._lock:
            channel, self._channel = self._channel, None
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass

    def _lines(self, channel, seconds):
        deadline = time.monotonic() + seconds
        pending = bytearray()
        overflow = False
        while not self._cancel.is_set() and time.monotonic() < deadline:
            chunk = channel.read(128)
            if not isinstance(chunk, bytes):
                raise OSError('invalid serial read')
            for value in chunk:
                if value == 10:
                    if not overflow:
                        try:
                            yield bytes(pending).removesuffix(b'\r').decode('ascii')
                        except UnicodeDecodeError:
                            pass
                    pending.clear()
                    overflow = False
                elif len(pending) < 256 and not overflow:
                    pending.append(value)
                else:
                    overflow = True

    @staticmethod
    def _write(channel, value):
        data = value.encode('utf-8') + b'\n'
        if channel.write(data) != len(data):
            raise OSError('serial write incomplete')

    @staticmethod
    def _diagnostic(line, *, status=False):
        prefix = 'WIFI_STATUS ' if status else 'WIFI_READY '
        if not line.startswith(prefix):
            return None
        fields = {}
        for token in line[len(prefix):].split():
            if '=' not in token:
                return None
            key, value = token.split('=', 1)
            if key in fields:
                return None
            fields[key] = value
        if fields.get('port') != '9001':
            return None
        if status and (fields.get('protocol') != '2' or fields.get('state') not in ('connected', 'disconnected')):
            return None
        state = fields.get('state', 'connected')
        ip = _address(fields.get('ip')) if state == 'connected' else None
        rssi = fields.get('rssi_dbm')
        if rssi is not None:
            if not re.fullmatch(r'-?[0-9]{1,3}', rssi) or not -127 <= int(rssi) <= 0:
                return None
            rssi = int(rssi)
        return dict(state=state, ip=ip, rssi=rssi)

    def _probe(self, port):
        channel = self._open_channel(port)
        self._write(channel, 'STATUS')
        for line in self._lines(channel, 5):
            value = self._diagnostic(line, status=True)
            if value:
                self._observe(value['state'], 'usb', value['ip'], value['rssi'])
                self._ready('板端报告 Wi-Fi 已连接；中央接入状态另行确认。' if value['state'] == 'connected'
                            else '板端报告 Wi-Fi 未连接，请填写名称和密码开始连接。')
                return
            if line.startswith('CONTROL_REJECTED'):
                break
        self._observe('unknown', 'usb')
        self._ready('未取得板端状态。旧固件可能不支持 STATUS；仍可直接 USB 配网，或更新 ComputeWiFi 固件。')

    def _configure(self, port, ssid, password):
        self._stop_gateway()
        with self._lock:
            self._host = None
        self._observe('unknown', 'configuration')
        channel = self._open_channel(port)
        self._write(channel, 'CONFIG')
        sent_ssid = sent_password = False
        for line in self._lines(channel, 65):
            if line == 'CONFIG_SSID' and not sent_ssid:
                self._write(channel, ssid)
                sent_ssid = True
            elif line == 'CONFIG_PASSWORD' and sent_ssid and not sent_password:
                self._write(channel, password)
                password = ''
                sent_password = True
            elif line.startswith('WIFI_READY ') and sent_password:
                value = self._diagnostic(line)
                if not value:
                    raise WiFiError(502, '板端返回的地址无效，请重新检测或配网。')
                self._observe('connected', 'configuration', value['ip'], value['rssi'])
                self._close_channel()
                self._start_gateway(value['ip'])
                self._ready('Wi-Fi 连接成功，已按新 IP 启动网关；正在等待中央握手。')
                return
            elif line.startswith(('WIFI_FAILED', 'CONFIG_FAILED', 'SERVER_FAILED')):
                self._observe('disconnected', 'configuration')
                raise WiFiError(400, 'Wi-Fi 连接失败。请检查名称、密码、2.4GHz 频段及是否需要校园网认证。')
        password = ''
        if not self._cancel.is_set():
            raise WiFiError(504, '配网等待超时。请检查串口是否为 ESP32、固件是否为 ComputeWiFi，然后重试。')

    def _attach(self, host):
        self._stop_gateway()
        self._observe('unknown', 'not_observed')
        self._start_gateway(host)
        self._ready('已更新目标 IP 并启动网关，等待设备与中央握手；尚不能单凭地址确认 Wi-Fi 已连接。')

    def _start_gateway(self, host):
        with self._lock:
            if self._closed or self._cancel.is_set():
                return
            self.output.mkdir(parents=True, exist_ok=True)
            while True:
                self._attempt += 1
                directory = self.output / ('gateway-esp32-' + str(self._attempt))
                if not directory.exists():
                    break
            self._host = host
            selected_stop = threading.Event()
            def relay():
                try:
                    self._gateway_runner('esp32c3-01', directory, central_host=self.client.host,
                                         central_port=self.client.port, esp_host=host, stop=selected_stop)
                except Exception:
                    with self._lock:
                        if self._gateway_stop is selected_stop and not self._closed:
                            self._error = 'gateway_failed'
                            self._message = 'ESP32 网关暂不可用，请检查设备 IP、网络可达性及输出目录。'
            self._gateway_stop = selected_stop
            self._gateway_thread = threading.Thread(target=relay, daemon=True)
            self._gateway_thread.start()

    def _stop_gateway(self):
        with self._lock:
            event, thread = self._gateway_stop, self._gateway_thread
            if event is not None:
                event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(10)
            if thread.is_alive():
                raise WiFiError(503, '原 ESP32 网关尚未停止，请稍后重试。')
        with self._lock:
            if self._gateway_thread is thread:
                self._gateway_thread = self._gateway_stop = None

    def start(self):
        with self._lock:
            if self._started or self._closed:
                return
            self._started = True
            if self._host:
                self._start_gateway(self._host)

    def close(self):
        with self._lock:
            self._closed = True
            self._cancel.set()
            worker = self._thread
        self._close_channel()
        self._stop_gateway()
        if worker is not None and worker is not threading.current_thread():
            worker.join(5)
