#!/usr/bin/env python3
"""Create an isolated Ubuntu development VM under .local/vmware.

Requires VMware Workstation, paramiko and pycdlib; never edits existing VMs.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local' / 'vmware'
sys.path.insert(0, str(LOCAL / 'python-deps'))
IMAGE = 'noble-server-cloudimg-amd64.vmdk'
IMAGE_BASE = 'https://cloud-images.ubuntu.com/noble/20261017/'
IMAGE_SHA256 = '9a1e3959c9f4ad9d4752101fd4ff9a43191fb7d72e61fa20e033f9fd235ce794'
VMWARE = Path('C:/Program Files (x86)/VMware/VMware Workstation')


def verify_image(path: Path, sums: Path) -> None:
    records = [line.split() for line in sums.read_text().splitlines()]
    expected = next(row[0] for row in records if row[-1].lstrip('*') == IMAGE)
    with path.open('rb') as file:
        digest = hashlib.file_digest(file, 'sha256').hexdigest()
    if expected != IMAGE_SHA256 or digest != IMAGE_SHA256:
        raise RuntimeError('Ubuntu image SHA256 mismatch; refusing to create VM')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--memory-mb', type=int, default=1024)
    parser.add_argument('--cpus', type=int, default=2)
    parser.add_argument('--apt-mirror', help='Optional HTTPS Ubuntu mirror URL; defaults to official Ubuntu archives')
    args = parser.parse_args()
    import paramiko
    import pycdlib
    if not 768 <= args.memory_mb <= 16384 or not 1 <= args.cpus <= 8:
        parser.error('Use 768..16384 MB memory and 1..8 CPUs')
    if args.apt_mirror:
        mirror = urlparse(args.apt_mirror)
        if mirror.scheme != 'https' or not mirror.hostname or mirror.username or mirror.password or mirror.query or mirror.fragment:
            parser.error('--apt-mirror must be an HTTPS repository URL without credentials, query or fragment')
    vm_dir = LOCAL / 'edge-linux'
    vmx = vm_dir / 'edge-linux.vmx'
    if vmx.exists() or (vm_dir / 'disk.vmdk').exists():
        parser.error('This VM already exists. Use vmrun start; creation never overwrites it.')
    images = LOCAL / 'images'
    images.mkdir(parents=True, exist_ok=True)
    image = images / IMAGE
    sums = images / 'SHA256SUMS'
    if not sums.exists():
        urllib.request.urlretrieve(IMAGE_BASE + 'SHA256SUMS', sums)
    if not image.exists():
        print('Downloading Ubuntu cloud image (~563 MiB)...', flush=True)
        partial = image.with_suffix('.partial')
        urllib.request.urlretrieve(IMAGE_BASE + IMAGE, partial)
        partial.replace(image)
    verify_image(image, sums)
    vm_dir.mkdir(parents=True, exist_ok=True)
    private_key = vm_dir / 'id_rsa'
    if not private_key.exists():
        key = paramiko.RSAKey.generate(3072)
        key.write_private_key_file(str(private_key))
        private_key.with_suffix('.pub').write_text(f'{key.get_name()} {key.get_base64()} edge-linux-local\n')
    public_key = private_key.with_suffix('.pub').read_text().strip()
    config = {
        'hostname': 'edge-linux', 'manage_etc_hosts': True,
        'users': [{'name': 'edge', 'groups': ['sudo'], 'shell': '/bin/bash',
                   'sudo': 'ALL=(ALL) NOPASSWD:ALL', 'lock_passwd': True,
                   'ssh_authorized_keys': [public_key]}],
        'ssh_pwauth': False, 'disable_root': True,
        'package_update': True, 'package_upgrade': False,
        'apt': {'conf': 'Acquire::Languages "none";\nAcquire::Retries "2";\n'},
        'packages': ['build-essential', 'cmake', 'gdb', 'python3', 'python3-venv', 'open-vm-tools',
                     'ninja-build', 'flex', 'bison', 'gperf', 'python3-pip', 'libffi-dev', 'libssl-dev', 'libusb-1.0-0'],
        'runcmd': [['systemctl', 'enable', '--now', 'open-vm-tools'],
                   ['sh', '-c', 'mkdir -p /home/edge/project && chown edge:edge /home/edge/project'],
                   ['sh', '-c', 'gcc --version && python3 --version && touch /var/lib/edge-linux-ready']],
        'final_message': 'EDGE_CLOUD_INIT_FINISHED: check /var/lib/edge-linux-ready and cloud-init status; uptime $UPTIME seconds',
    }
    if args.apt_mirror:
        repo = args.apt_mirror.rstrip('/')
        config['apt'].update({'primary': [{'arches': ['default'], 'uri': repo}],
                              'security': [{'arches': ['default'], 'uri': repo}]})
    seed_files = {
        'user-data': '#cloud-config\n' + json.dumps(config, indent=2) + '\n',
        'meta-data': 'instance-id: edge-linux-local-v1\nlocal-hostname: edge-linux\n',
    }
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge='1.09', vol_ident='CIDATA')
    for name, value in seed_files.items():
        payload = value.encode()
        iso.add_fp(io.BytesIO(payload), len(payload),
                   iso_path='/' + name.replace('-', '_').upper() + ';1',
                   rr_name=name, joliet_path='/' + name)
    iso.write(str(vm_dir / 'seed.iso'))
    iso.close()
    disk = vm_dir / 'disk.vmdk'
    subprocess.run([str(VMWARE / 'vmware-vdiskmanager.exe'), '-r', str(image), '-t', '0', str(disk)], check=True)
    subprocess.run([str(VMWARE / 'vmware-vdiskmanager.exe'), '-x', '16GB', str(disk)], check=True)
    # Relative file paths keep the entire VM portable with its directory.
    settings = {
        '.encoding': 'UTF-8', 'config.version': '8', 'virtualHW.version': '20',
        'displayName': 'ESP32 Linux Edge Scheduler', 'guestOS': 'ubuntu-64',
        'firmware': 'efi', 'memsize': str(args.memory_mb), 'numvcpus': str(args.cpus),
        'cpuid.coresPerSocket': str(args.cpus),
        'scsi0.present': 'TRUE', 'scsi0.virtualDev': 'lsilogic',
        'scsi0:0.present': 'TRUE', 'scsi0:0.fileName': 'disk.vmdk',
        'ide1:0.present': 'TRUE', 'ide1:0.deviceType': 'cdrom-image', 'ide1:0.fileName': 'seed.iso',
        'ethernet0.present': 'TRUE', 'ethernet0.connectionType': 'nat',
        'ethernet0.virtualDev': 'e1000', 'ethernet0.addressType': 'generated',
        'ethernet0.startConnected': 'TRUE', 'usb.present': 'FALSE', 'sound.present': 'FALSE',
        'serial0.present': 'TRUE', 'serial0.fileType': 'file', 'serial0.fileName': 'serial.log',
        'serial0.yieldOnMsrRead': 'TRUE', 'tools.syncTime': 'TRUE',
        'msg.autoAnswer': 'TRUE', 'uuid.action': 'create',
    }
    vmx.write_text(''.join(f'{key} = "{value}"\n' for key, value in settings.items()), encoding='utf-8')
    (vm_dir / 'image-source.json').write_text(json.dumps({'url': IMAGE_BASE + IMAGE,
        'sha256': next(line.split()[0] for line in sums.read_text().splitlines() if line.split()[-1].lstrip('*') == IMAGE)}, indent=2))
    print(f'Created {vmx}')
    print('Local SSH identity is in .local/vmware/edge-linux/id_rsa; never commit it.')
    if args.start:
        subprocess.run([str(VMWARE / 'vmrun.exe'), '-T', 'ws', 'start', str(vmx), 'nogui'], check=True)


if __name__ == '__main__':
    main()
