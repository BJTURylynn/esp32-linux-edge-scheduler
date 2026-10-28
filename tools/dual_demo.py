"""Concurrently bridge an Arduino USB source and ESP32 Wi-Fi source to one C service.

Start the Linux service first. Source data is synthetic; forwarded is not an ACK.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from .serial_bridge import BridgeConfig, run_bridge as run_serial
from .wifi_bridge import run_bridge as run_wifi


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial-port',required=True)
    parser.add_argument('--device-host',required=True)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=9000)
    parser.add_argument('--count',type=int,default=20)
    parser.add_argument('--duration',type=float,default=60)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if not 1<=args.count<=100 or not 0<args.duration<=600 or not 1<=args.port<=65535:
        parser.error('count 1..100, duration 0..600 seconds, valid port required')
    import serial
    args.output.mkdir(parents=True,exist_ok=False)
    config=BridgeConfig(port=args.serial_port,output=args.output/'arduino',host=args.host,
                        tcp_port=args.port,limit=args.count,duration=args.duration)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs={
          'arduino':pool.submit(run_serial,config,serial.Serial),
          'esp32':pool.submit(run_wifi,args.device_host,args.output/'esp32',
                              linux_host=args.host,linux_port=args.port,
                              count=args.count,timeout=args.duration)}
        results={name:future.result() for name,future in jobs.items()}
    ok=(results['esp32']['success'] and results['arduino']['forwarded']==args.count
        and all(results['arduino'][key]==0 for key in ('invalid','oversized','incomplete','uncertain','dropped')))
    summary={'success':ok,'requested_each':args.count,
             'forwarded_total':sum(result['forwarded'] for result in results.values()),
             'sources':results,'semantics':'forwarded is sendall only; verify Linux CSV separately'}
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,indent=2))
    return 0 if ok else 1


if __name__=='__main__':
    raise SystemExit(main())
