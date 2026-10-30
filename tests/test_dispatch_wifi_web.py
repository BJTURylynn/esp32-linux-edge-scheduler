"""HTTP integration for explicit Wi-Fi controls; no serial or model calls."""
from contextlib import contextmanager
import copy
import time
import unittest

from tools.dispatch_wifi import WiFiError
import test_dispatch_web as transport


class WiFi:
    def __init__(self):
        self.data = {"supported": True, "busy": False, "phase": "idle", "host": None,
                     "observation": {"state": "unknown"}}
        self.calls = []

    def snapshot(self):
        return copy.deepcopy(self.data)

    def ports(self):
        return {"ports": [{"port": "COM11", "description": "USB Serial"}], "serial_available": True}

    def connect(self, body):
        if set(body) != {"port", "ssid", "password"}:
            raise WiFiError(400, "配网字段无效。")
        self.calls.append("connect")
        return {"phase": "connecting", "busy": True, "operation_id": 1}

    def probe(self, body):
        self.calls.append("probe")
        return {"phase": "probing", "busy": True, "operation_id": 2}

    def attach(self, body):
        self.calls.append("attach")
        return {"phase": "attaching", "busy": True, "operation_id": 3}

    @contextmanager
    def task_guard(self):
        if self.data["busy"]:
            raise WiFiError(409, "正在配置连接，请稍后提交。")
        yield


class DispatchWiFiHTTPTests(unittest.TestCase):
    close = transport.DispatchWebTests.close
    request = transport.DispatchWebTests.request

    def setUp(self):
        transport.DispatchWebTests.setUp(self)
        self.wifi = WiFi()
        self.app.wifi_controller = self.wifi

    def test_offline_central_does_not_mean_wifi_disconnected(self):
        self.app.dispatch_client.unavailable = True
        status, data = self.request("GET", "/api/dispatch/wifi")
        self.assertEqual(status, 200)
        self.assertFalse(data["central_available"])
        self.assertEqual(data["central_state"], "unknown")
        self.assertEqual(data["wifi_state"], "unknown")
        self.assertEqual(data["csrf_token"], self.app.csrf_token)

    def test_usb_observation_is_independent_and_expires(self):
        self.wifi.data["observation"] = {"state": "disconnected", "source": "usb", "observed_at": time.time()}
        self.assertEqual(self.request("GET", "/api/dispatch/wifi")[1]["wifi_state"], "disconnected")
        self.wifi.data["observation"]["observed_at"] = time.time()-60
        self.assertEqual(self.request("GET", "/api/dispatch/wifi")[1]["wifi_state"], "unknown")

    def test_current_central_handshake_is_wifi_transport_evidence(self):
        self.app.dispatch_client.state = lambda: {"nodes": [{"device": "esp32c3-01", "state": "idle"}]}
        data = self.request("GET", "/api/dispatch/wifi")[1]
        self.assertEqual(data["wifi_state"], "connected")
        self.assertEqual(data["wifi_source"], "central_tcp")

    def test_connect_probe_attach_are_explicit_csrf_protected_and_nonblocking(self):
        for operation, body in (("connect", {"port": "COM11", "ssid": "test", "password": "private-pass"}),
                                ("probe", {"port": "COM11"}), ("attach", {"host": "192.168.1.42"})):
            path = "/api/dispatch/wifi/"+operation
            self.assertEqual(self.request("POST", path, body, token=False)[0], 403)
            status, result = self.request("POST", path, body)
            self.assertEqual(status, 202)
            self.assertTrue(result["busy"])
            self.assertNotIn("private-pass", str(result))
        self.assertEqual(self.wifi.calls, ["connect", "probe", "attach"])

    def test_wifi_changes_and_secrets_cannot_be_put_in_url(self):
        self.assertEqual(self.request("GET", "/api/dispatch/wifi?host=192.168.1.42")[0], 400)
        self.assertEqual(self.request("GET", "/api/dispatch/wifi/ports?password=secret")[0], 400)
        self.assertEqual(self.request("POST", "/api/dispatch/wifi/connect?password=secret", {})[0], 400)
        self.assertEqual(self.wifi.calls, [])

    def test_port_enumeration_and_disabled_standalone_mode(self):
        self.assertEqual(self.request("GET", "/api/dispatch/wifi/ports")[1]["ports"][0]["port"], "COM11")
        self.app.wifi_controller = None
        self.assertFalse(self.request("GET", "/api/dispatch/wifi")[1]["supported"])
        self.assertEqual(self.request("POST", "/api/dispatch/wifi/probe", {"port": "COM11"})[0], 409)

    def test_no_task_submission_or_retry_during_reconfiguration(self):
        self.wifi.data["busy"] = True
        self.assertEqual(self.request("POST", "/api/dispatch/tasks", {"run_id": transport.RUN})[0], 409)
        self.assertEqual(self.request("POST", "/api/dispatch/tasks/1/retry", {"run_id": transport.RUN})[0], 409)
        self.assertEqual(self.app.dispatch_client.calls, [])


if __name__ == "__main__":
    unittest.main()
