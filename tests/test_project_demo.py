import argparse
import io
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tools.experiment import free_port
from tools.project_demo import run_demo, validate, windows_to_wsl, write_result
from tools.protocol import encode, synthetic

ROOT = Path(__file__).resolve().parents[1]


def arguments(output, **changes):
    values = dict(esp_host='192.0.2.10', uno_port='COM10', output=output,
                  policy='priority', passes=1, count=1, timeout=5,
                  distribution='Ubuntu-24.04')
    values.update(changes)
    return argparse.Namespace(**values)


class ProjectDemoTests(unittest.TestCase):
    def test_windows_path_conversion_preserves_spaces_without_shell(self):
        self.assertEqual(windows_to_wsl(r'D:\My Projects\edge'), '/mnt/d/My Projects/edge')
        with self.assertRaises(ValueError): windows_to_wsl(r'\\server\share\edge')

    def test_localhost_proxy_startup_race_is_retried(self):
        from tools.project_demo import wait_local_listener
        from unittest.mock import MagicMock
        with patch('tools.project_demo.socket.create_connection',
                   side_effect=[ConnectionRefusedError(), MagicMock()]) as connect, \
             patch('tools.project_demo.time.sleep'):
            wait_local_listener(12345)
            self.assertEqual(connect.call_count,2)

    def test_existing_output_is_not_touched_and_no_process_started(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/'demo-run'
            output.mkdir()
            (output/'keep.txt').write_text('keep')
            with patch('tools.project_demo.subprocess.Popen') as spawn:
                with self.assertRaises(FileExistsError): run_demo(arguments(output))
                spawn.assert_not_called()
            self.assertEqual((output/'keep.txt').read_text(), 'keep')

    def test_invalid_bounds_rejected_before_output_created(self):
        for changes in ({'count':0}, {'timeout':float('nan')}, {'passes':1000001},
                        {'esp_host':'::1'}, {'distribution':'--bad'}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as temp:
                output = Path(temp)/'demo-run'
                with self.assertRaises(ValueError): validate(arguments(output, **changes))
                self.assertFalse(output.exists())

    def test_start_failure_is_reported_without_success_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)/'demo-run'
            with patch('tools.project_demo.subprocess.Popen', side_effect=OSError('missing WSL')):
                result = run_demo(arguments(output))
            self.assertFalse(result['success'])
            self.assertIn('missing WSL', result['error'])
            saved = json.loads((output/'result.json').read_text())
            self.assertFalse(saved['success'])
            self.assertIn('未通过', (output/'report.md').read_text())

    def test_no_contention_report_does_not_claim_priority_advantage(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            result = dict(success=True, error=None, verification={'verified':True,'completed':2,'rejected':0},
                          audit={'policy_ok':True,'priority_behavior_observed':False,'urgent_overtake_decisions':0})
            write_result(output, result)
            self.assertIn('实际观察到紧急优先取出：False', (output/'report.md').read_text())
            self.assertIn('不作性能提升结论', (output/'report.md').read_text())


@unittest.skipUnless(os.name != 'nt' and os.environ.get('EDGE_SERVER'), 'needs Linux compiled EDGE_SERVER')
class DemoServiceLifecycleTests(unittest.TestCase):
    def launch(self, temporary, timeout=5):
        root = Path(temporary)
        port = free_port()
        process = subprocess.Popen([
            sys.executable,'-u','-m','tools.demo_service','--name','demo-test',
            '--copy-to',str(root/'service'),'--base',str(root/'native'),
            '--server',str(Path(os.environ['EDGE_SERVER']).resolve()),
            '--port',str(port),'--expected','2','--passes','1','--timeout',str(timeout)],
            cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,encoding='utf-8')
        def cleanup():
            if process.poll() is None:
                try: process.communicate('STOP\n',timeout=10)
                except (OSError,ValueError,subprocess.TimeoutExpired): process.kill();process.communicate()
        self.addCleanup(cleanup)
        lines = queue.Queue()
        def reader():
            for line in process.stdout:lines.put(line)
        thread = threading.Thread(target=reader,daemon=True)
        thread.start()
        event = json.loads(lines.get(timeout=10))
        self.assertEqual(event['event'],'ready',event)
        return process, lines, root, port

    def finish(self, process, lines, root, port, command):
        if command is not None:
            process.stdin.write(command)
            process.stdin.flush()
        process.stdin.close()
        process.wait(timeout=10)
        event = json.loads(lines.get(timeout=3))
        self.assertEqual(event['event'],'finished')
        self.assertFalse(json.loads((root/'service/status.json').read_text())['running'])
        with self.assertRaises(OSError): socket.create_connection(('127.0.0.1',port),timeout=.1)
        process.stdout.close()
        process.stderr.close()
        return event

    def test_stop_cancels_and_releases_owned_listener(self):
        with tempfile.TemporaryDirectory(prefix='edge-demo-') as temp:
            proc, lines, root, port = self.launch(temp)
            event = self.finish(proc,lines,root,port,'STOP\n')
            self.assertFalse(event['success'])
            self.assertIn('cancelled',event['error'])

    def test_crlf_stop_cancels_and_releases_owned_listener(self):
        with tempfile.TemporaryDirectory(prefix='edge-demo-') as temp:
            proc, lines, root, port = self.launch(temp)
            event = self.finish(proc,lines,root,port,'STOP\r\n')
            self.assertFalse(event['success'])
            self.assertIn('cancelled',event['error'])

    def test_eof_cleans_up_without_claiming_completion(self):
        with tempfile.TemporaryDirectory(prefix='edge-demo-') as temp:
            proc, lines, root, port = self.launch(temp)
            event = self.finish(proc,lines,root,port,None)
            self.assertFalse(event['success'])
            self.assertIn('closed',event['error'])

    def test_finish_waits_for_both_records_and_copies_final_csv(self):
        with tempfile.TemporaryDirectory(prefix='edge-demo-') as temp:
            proc, lines, root, port = self.launch(temp)
            with socket.create_connection(('127.0.0.1',port),timeout=1) as conn:
                conn.sendall(encode(synthetic(0,16))+encode(synthetic(1,16)))
            # Leave stdin open until the FINISH event: EOF is cancellation.
            proc.stdin.write('FINISH\n');proc.stdin.flush()
            event = json.loads(lines.get(timeout=10))
            proc.wait(timeout=5)
            self.assertTrue(event['success'],event)
            self.assertEqual(event['final_status']['completed'],2)
            self.assertFalse(event['final_status']['running'])
            self.assertEqual(len((root/'service/tasks.csv').read_text().splitlines()),3)
            proc.stdin.close();proc.stdout.close();proc.stderr.close()

    def test_crlf_finish_waits_for_both_records_and_copies_final_csv(self):
        with tempfile.TemporaryDirectory(prefix='edge-demo-') as temp:
            proc, lines, root, port = self.launch(temp)
            with socket.create_connection(('127.0.0.1',port),timeout=1) as conn:
                conn.sendall(encode(synthetic(0,16))+encode(synthetic(1,16)))
            # Leave stdin open until the FINISH event: EOF is cancellation.
            proc.stdin.write('FINISH\r\n');proc.stdin.flush()
            event = json.loads(lines.get(timeout=10))
            proc.wait(timeout=5)
            self.assertTrue(event['success'],event)
            self.assertEqual(event['final_status']['completed'],2)
            self.assertFalse(event['final_status']['running'])
            self.assertEqual(len((root/'service/tasks.csv').read_text().splitlines()),3)
            proc.stdin.close();proc.stdout.close();proc.stderr.close()

    def command_for_test(self, args, root, port):
        from tools.project_demo import supervisor_command
        return supervisor_command(args,root,port)+[
            '--base',str(args.output.parent/'native'),
            '--server',str(Path(os.environ['EDGE_SERVER']).resolve())]

    def test_wrapper_capture_failure_cleans_real_child_and_reports_failure(self):
        with tempfile.TemporaryDirectory(prefix='edge-demo-') as temp:
            output=Path(temp)/'demo-run'
            result=run_demo(arguments(output),capture_runner=lambda *a:1,
                            command_factory=self.command_for_test)
            self.assertFalse(result['success'])
            self.assertIn('capture failed',result['error'])
            self.assertFalse(json.loads((output/'service/status.json').read_text())['running'])
            self.assertFalse(json.loads((output/'result.json').read_text())['success'])

    def test_wrapper_real_service_verified_without_inventing_contention(self):
        def capture(args, root, port):
            directory=args.output/'capture'
            directory.mkdir()
            sources={}
            with socket.create_connection(('127.0.0.1',port),timeout=1) as conn:
                for source,identity in [('arduino','uno-01'),('esp32','esp32c3-01')]:
                    target=directory/source;target.mkdir()
                    message=dict(device_id=identity,seq=0,source='synthetic',priority='normal',
                                 samples=[(i%201)-100 for i in range(256)])
                    frame=encode(message)
                    (target/'frames.jsonl').write_bytes(frame)
                    stats=dict(forwarded=1,success=True,invalid=0,oversized=0,
                               incomplete=0,uncertain=0,dropped=0)
                    (target/'stats.json').write_text(json.dumps(stats))
                    sources[source]=stats
                    conn.sendall(frame)
            (directory/'summary.json').write_text(json.dumps(dict(
                success=True,requested_each=1,forwarded_total=2,sources=sources)))
            return 0
        with tempfile.TemporaryDirectory(prefix='edge-demo-') as temp:
            output=Path(temp)/'demo-run'
            result=run_demo(arguments(output),capture_runner=capture,
                            command_factory=self.command_for_test)
            self.assertTrue(result['success'],result)
            self.assertTrue(result['verification']['verified'])
            self.assertFalse(result['audit']['priority_behavior_observed'])
            self.assertEqual(result['verification']['completed'],2)
            self.assertFalse(json.loads((output/'service/status.json').read_text())['running'])

    def test_deadline_terminates_owned_service(self):
        with tempfile.TemporaryDirectory(prefix='edge-demo-') as temp:
            proc, lines, root, port = self.launch(temp,timeout=1)
            event = json.loads(lines.get(timeout=5))
            proc.wait(timeout=5)
            self.assertFalse(event['success'])
            self.assertIn('deadline',event['error'])
            self.assertFalse(json.loads((root/'service/status.json').read_text())['running'])
            proc.stdin.close();proc.stdout.close();proc.stderr.close()


if __name__=='__main__':unittest.main()
