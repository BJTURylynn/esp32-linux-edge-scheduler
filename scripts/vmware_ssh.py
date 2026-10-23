#!/usr/bin/env python3
"""Run a command or upload project sources to the isolated edge-linux VM."""
from __future__ import annotations

import argparse
import codecs
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VM = ROOT / '.local' / 'vmware' / 'edge-linux'
sys.path.insert(0, str(ROOT / '.local' / 'vmware' / 'python-deps'))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', help='Override vmrun guest IP detection')
    parser.add_argument('--upload', action='store_true', help='Upload source files to /home/edge/project')
    parser.add_argument('--download', nargs=2, metavar=('REMOTE', 'LOCAL'), help='Download a guest file after running the command')
    parser.add_argument('command', nargs='?', default='(test -f /var/lib/edge-linux-ready || cloud-init status --wait) && gcc --version && cmake --version && python3 --version')
    args = parser.parse_args()
    import paramiko
    host = args.host
    if not host:
        vmrun = Path('C:/Program Files (x86)/VMware/VMware Workstation/vmrun.exe')
        result = subprocess.run([str(vmrun), 'getGuestIPAddress', str(VM / 'edge-linux.vmx'), '-wait'],
                                capture_output=True, text=True, timeout=60, check=True)
        host = result.stdout.strip()
    client = paramiko.SSHClient()
    known_hosts = VM / 'known_hosts'
    if known_hosts.exists():
        client.load_host_keys(str(known_hosts))
    # TOFU is scoped to this fresh local VM and persisted for subsequent checks.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username='edge', key_filename=str(VM / 'id_rsa'),
                   allow_agent=False, look_for_keys=False, timeout=20)
    client.save_host_keys(str(known_hosts))
    try:
        if args.upload:
            sftp = client.open_sftp()
            excludes = {'.git', '.local', '.venv', '__pycache__', '.pytest_cache', 'build', 'results', 'outputs'}
            try:
                sftp.mkdir('/home/edge/project')
            except OSError:
                sftp.stat('/home/edge/project')
            for current, dirs, files in os.walk(ROOT, followlinks=False):
                dirs[:] = [name for name in dirs if name not in excludes and not name.startswith('build-')
                           and not (Path(current) / name).is_symlink()]
                remote_dir = '/home/edge/project/' + Path(current).relative_to(ROOT).as_posix()
                if Path(current) != ROOT:
                    try:
                        sftp.mkdir(remote_dir)
                    except OSError:
                        sftp.stat(remote_dir)
                for name in files:
                    path = Path(current) / name
                    if name not in {'sdkconfig', 'sdkconfig.old'} and not path.is_symlink():
                        sftp.put(str(path), remote_dir + '/' + name)
            sftp.close()
            print('Uploaded project sources to /home/edge/project', flush=True)
        channel = client.get_transport().open_session(timeout=20)
        channel.set_combine_stderr(True)
        channel.settimeout(300)
        channel.exec_command(args.command)
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        while True:
            data = channel.recv(65536)
            if not data:
                break
            sys.stdout.write(decoder.decode(data))
            sys.stdout.flush()
        sys.stdout.write(decoder.decode(b'', final=True))
        sys.stdout.flush()
        status = channel.recv_exit_status()
        channel.close()
        if args.download:
            remote, local = args.download
            local_path = Path(local).resolve()
            if not local_path.is_relative_to(ROOT):
                parser.error('Downloads must stay inside this project directory')
            local_path.parent.mkdir(parents=True, exist_ok=True)
            sftp = client.open_sftp()
            sftp.get(remote, str(local_path))
            sftp.close()
        (VM / 'connection.json').write_text(json.dumps({'host': host, 'user': 'edge',
                                                       'project': '/home/edge/project'}, indent=2))
        raise SystemExit(status)
    finally:
        client.close()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    main()
