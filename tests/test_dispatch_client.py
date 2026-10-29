import json
import socket
import threading
import unittest

from tools.dispatch_client import DispatchClient, DispatchError, batch_parameters, decode

RUN = '0123456789abcdef'


class ReplyServer:
    def __init__(self, replies):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(2)
        self.port = self.listener.getsockname()[1]
        self.replies = replies
        self.received = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            for reply in self.replies:
                with self.listener.accept()[0] as channel:
                    data = b''
                    while b'\n' not in data:
                        data += channel.recv(512)
                    self.received.append(json.loads(data))
                    if reply is not None:
                        raw = reply if isinstance(reply, bytes) else json.dumps(reply).encode() + b'\n'
                        for start in range(0, len(raw), 7):
                            channel.sendall(raw[start:start + 7])
        except (OSError, ValueError):
            pass
        finally:
            self.listener.close()

    def close(self):
        self.listener.close()
        self.thread.join(3)


class DispatchClientTests(unittest.TestCase):
    def server(self, replies):
        server = ReplyServer(replies)
        self.addCleanup(server.close)
        return server, DispatchClient(port=server.port)

    def test_batch_defaults_priority_seed_and_partial_admission(self):
        server, client = self.server([{'ok': True, 'task_id': 1},
                                      {'ok': False, 'error': 'queue_full'},
                                      {'ok': True, 'task_id': 2}])
        result = client.submit({'priority': 'urgent', 'seed': 4294967295, 'batch_size': 3, 'run_id': RUN})
        self.assertEqual([r['task_id'] for r in result['accepted']], [1, 2])
        self.assertEqual(result['rejected'], [{'index': 1, 'error': 'queue_full'}])
        self.assertEqual([r['seed'] for r in server.received], [4294967295, 0, 1])
        self.assertTrue(all(r['count'] == 16 and r['passes'] == 100 for r in server.received))

    def test_ambiguous_submission_stops_batch_without_replay(self):
        server, client = self.server([{'ok': True, 'task_id': 11}, None])
        with self.assertRaises(DispatchError) as raised:
            client.submit({'batch_size': 3, 'run_id': RUN})
        receipt = raised.exception.receipts
        self.assertFalse(receipt['ok'])
        self.assertEqual(receipt['accepted'], [{'index': 0, 'task_id': 11}])
        self.assertEqual(receipt['unknown_index'], 1)
        self.assertEqual(receipt['not_submitted'], [2])
        self.assertEqual(len(server.received), 2)

    def test_state_and_manual_retry_have_distinct_commands(self):
        server, client = self.server([{'ok': True, 'run_id': RUN, 'nodes': [], 'tasks': []},
                                      {'ok': True, 'task_id': 12}])
        self.assertEqual(client.state()['tasks'], [])
        self.assertEqual(client.retry(9, run_id=RUN)['task_id'], 12)
        self.assertEqual(server.received, [{'v': 2, 'type': 'state'},
                                          {'v': 2, 'type': 'retry', 'id': 9, 'run_id': RUN}])

    def test_batch_binds_run_once_and_honors_central_stale_rejection(self):
        server, client = self.server([{'ok': True, 'run_id': RUN, 'nodes': [], 'tasks': []},
                                      {'ok': True, 'task_id': 1},
                                      {'ok': False, 'error': 'stale_run'}])
        receipt = client.submit({'batch_size': 2})
        self.assertEqual(receipt['rejected'], [{'index': 1, 'error': 'stale_run'}])
        self.assertEqual([message['type'] for message in server.received], ['state', 'submit', 'submit'])
        self.assertEqual(server.received[1]['run_id'], server.received[2]['run_id'])

    def test_invalid_parameter_bounds_do_not_open_connection(self):
        def forbidden(*args, **kwargs):
            self.fail('invalid task contacted central service')
        client = DispatchClient(socket_factory=forbidden)
        for body in ({'count': 0}, {'count': True}, {'passes': 1001},
                     {'window': 17}, {'seed': -1}, {'batch_size': 33},
                     {'priority': 'shell'}, {'host': 'untrusted'}, {'batch_size': False}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                client.submit(body)
        with self.assertRaises(ValueError):
            client.retry(True)

    def test_malformed_or_nonfinite_responses_fail_honestly(self):
        for payload in (b'{"ok":true,"ok":false}\n', b'{"ok":true,"x":1e999}\n',
                        b'{"ok":true}\nextra', b'not json\n', b'[]\n'):
            with self.subTest(payload=payload):
                _, client = self.server([payload])
                with self.assertRaises(DispatchError):
                    client.request({'v': 2, 'type': 'state'})

    def test_state_missing_lists_and_false_status_are_not_ready(self):
        for value in ({'ok': False, 'error': 'stopping'}, {'ok': True}):
            _, client = self.server([value])
            with self.assertRaises(DispatchError):
                client.state()


if __name__ == '__main__':
    unittest.main()
