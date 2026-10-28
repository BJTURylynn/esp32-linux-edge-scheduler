# 粉色 ColorEasyDuino 接入 Linux 调度器

当前实物是 LCKFB-ColorEasyDuino-Pink：ATmega328P，兼容 Arduino Uno，CH340 USB 转串口。它不是 GD32 梁山派，也不是运行 Linux 的树莓派。仓库地址保持原名，默认设备端已改为此板；Linux C 调度器、现有线协议与只读 Agent 继续使用。

```text
粉色板（合成数据） → USB / CH340 → Windows Python 串口桥接
                                      ↓ TCP / localhost
                             WSL Ubuntu C 调度器 → CSV / 状态 → Agent
```

## 1. 编译与上传

Arduino IDE 选择 Arduino Uno，打开 `firmware/coloreasyduino/EdgeDevice/EdgeDevice.ino`，选择设备管理器里实际的 CH340 端口。本机目前是 COM10，换接口后可能变化。上传前先停止串口桥接或 Serial Monitor。

本机已准备 Arduino IDE 2.3.10、Arduino CLI 1.5.1、AVR Boards 1.8.8；在 PowerShell 的项目根目录：

```powershell
$cli = 'D:\Codex\ColorEasyDuino\tools\ArduinoIDE\resources\app\lib\backend\resources\arduino-cli.exe'
$config = 'D:\Codex\ColorEasyDuino\work\arduino-cli.yaml'
& $cli --config-file $config compile --fqbn arduino:avr:uno --build-path D:/Codex/esp32-linux-edge-scheduler/work/uno-build firmware/coloreasyduino/EdgeDevice
& $cli --config-file $config upload --fqbn arduino:avr:uno --port COM10 --build-path D:/Codex/esp32-linux-edge-scheduler/work/uno-build firmware/coloreasyduino/EdgeDevice
```

程序每次发送完至少间隔250ms，普通256样本、紧急16样本，每五帧一帧紧急。只输出协议JSON，不再输出旧灯光测试的调试文字。13口维持约1000ms心跳，与外接灯/板载LED同步；串口发送可能阻塞，它不表示Linux任务完成，也不保证精确实时周期。

## 2. 启动 Linux C 服务

在 PowerShell 进入项目对应的 Ubuntu 目录：

```powershell
wsl -d Ubuntu-24.04 --cd /mnt/d/Projects/esp32-linux-edge-scheduler
```

然后在 Ubuntu 内执行：

```bash
make -C linux
./linux/build/edge_scheduler --bind 0.0.0.0 --port 9000 \
  --policy priority --capacity 32 --window 8 --passes 1 \
  --output "$HOME/edge-results/uno-live"
```

输出目录必须没有旧 `tasks.csv`，下一次使用 `uno-live-v2` 等新目录。运行时状态和CSV放在Ubuntu原生文件系统（本机虚拟磁盘实际在D盘），避免WSL对Windows挂载目录并发原子更新的读写差异。以上绑定用于WSL的Windows localhost转发；本机不需要新增公网端口或防火墙规则。桥接和C服务端口必须一致。

## 3. 启动串口桥接

另一个 PowerShell，先进入仓库根目录。其他电脑安装 Python 3.10+ 后 `python -m pip install -r requirements-device.txt`；本机已经准备好独立环境：

```powershell
$python = 'D:\Codex\esp32-linux-edge-scheduler\tools\device-venv\Scripts\python.exe'
& $python -m tools.serial_bridge --port COM10 --host 127.0.0.1 --tcp-port 9000 --output D:/Codex/esp32-linux-edge-scheduler/outputs/uno-live --limit 20 --duration 60
```

默认115200波特率，串口空闲等待最多0.5s，有数据立即读取，单次最多4096字节；内存有界且不积累任务队列。`--limit` 是sendall成功数，达到后关闭桥接；`--duration` 防止演示一直等待。指定limit却未达到时退出码1。去掉两者会持续运行，Ctrl+C停止。再次运行使用新的输出目录。

输出 `frames.jsonl` 是校验后的已发送帧；`stats.json` 周期原子更新及退出时保存，记录invalid、oversized、incomplete、serial_errors、tcp_errors、dropped、uncertain等。TCP/串口失败会尝试重连，但不确定的发送不重放。打开串口可能通过DTR复位Uno，序号重新从0开始，不具备全局去重语义。桥接阻塞/离线期间UART可能丢字节，拒绝记录不能证明设备端无损。

## 4. 查询与公平对照

在 Ubuntu 同一项目目录：

```bash
python3 -m tools.agent status --status "$HOME/edge-results/uno-live/status.json"
```

C服务Ctrl+C后停止接收并排空任务。优先级在队列中选择下一任务，不抢占当前任务。实时板上串口帧不等于可重复的策略实验：本次先捕获真实板生成的数据，再为FIFO/priority使用同一组20帧和合成2ms到达间隔，各运行三次、相同CPU绑定与计算参数。见 [实物接入及回放证据](evidence/coloreasyduino-usb/README.md)。这组压测使用passes=20000；日常演示用passes=1。

## 5. 自动测试

```bash
make test
make sanitize
EDGE_SERVER="$PWD/linux/build-sanitize/edge_scheduler" EDGE_FRAME_TEST="$PWD/build/firmware-host/test_frame" python3 -m unittest discover -s tests -v
```

串口桥接单测与桥接→真实C服务测试通过依赖注入运行，不要求CI插入板子或安装pyserial；额外Uno固件CI验证AVR编译。ESP32仍是可选实现，见原firmware文档。USB拔插恢复、断电重启、真实传感器采集需要另外实测；本次没有把它们写成已通过。
