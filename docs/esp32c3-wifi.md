# ESP32-C3 与 WSL Ubuntu 无线 TCP 联通

本入口复用现有 Linux C 调度器，增加 ESP32-C3 Arduino 设备端。与粉色板 USB 串口方案分开，两个设备端可以保留。

## 实际数据路径

```text
ESP32-C3（TCP服务器9001，生成合成任务）
       ⇅ Wi-Fi / Shino 2.4GHz 热点
Windows电脑（TCP客户端，接收并校验NDJSON）
       ↓ 另一条TCP连接 / 127.0.0.1:9000
WSL2 Ubuntu（C调度器）→ tasks.csv / status.json
```

电脑和开发板必须连接同一局域网。Windows连接开发板9001，并向WSL的9000主动发起连接；无需Windows入站防火墙规则、端口映射或修改WSL网络模式。WSL2默认NAT下，这不是开发板直接访问WSL私有IP；Windows转发是显式的一段链路。任务数据真实走Wi-Fi，USB仅用于下载、临时配置和诊断，不承担任务转发。

热点外网限速不会直接决定同一局域网链路的速度；本入口用于验证正确收发和计算，不是吞吐量压测。

## 1. 烧录与连接配置

Arduino打开 `firmware/esp32c3-arduino/EdgeWiFi/EdgeWiFi.ino`。已在本块8MB Flash实物验证的设置：ESP32C3 Dev Module，Flash Mode DIO、40MHz、8MB、USB CDC Disabled。默认QIO在这块实物上曾无法正确读取分区表，必须完整选择DIO重新构建，不能只修改镜像头。

本机PowerShell，在项目根目录：

```powershell
$cli = 'D:\Codex\ColorEasyDuino\tools\ArduinoIDE\resources\app\lib\backend\resources\arduino-cli.exe'
$config = 'D:\Codex\ColorEasyDuino\work\arduino-cli.yaml'
$target = 'esp32:esp32:esp32c3:FlashMode=dio,FlashFreq=40,FlashSize=8M'
& $cli --config-file $config compile --fqbn $target --build-path D:/Codex/ESP32C3/work/edge-wifi-build firmware/esp32c3-arduino/EdgeWiFi
& $cli --config-file $config upload --fqbn $target --port COM11 --input-dir D:/Codex/ESP32C3/work/edge-wifi-build firmware/esp32c3-arduino/EdgeWiFi
$python = 'D:\Codex\esp32-linux-edge-scheduler\tools\device-venv\Scripts\python.exe'
& $python -m tools.wifi_configure --port COM11 --ssid Shino --output D:/Codex/esp32-linux-edge-scheduler/outputs/wifi-config
```

配置脚本会隐藏输入密码，也可从 `EDGE_WIFI_PASSWORD` 环境变量读取。不要把密码写入源码或作为命令行参数。成功输出 `WIFI_READY ip=... port=9001`，记下实际IP；DHCP和串口号可能变化。输出目录必须新建，重复运行请用有意义的v2名称，避免覆盖证据。脚本仅需要可选的pyserial依赖（requirements-device.txt）；Wi-Fi桥接程序本身只有Python标准库依赖。

不启动配置脚本时，可用串口115200手动输入 `CONFIG`，在 `CONFIG_SSID` 提示后输入SSID，在 `CONFIG_PASSWORD` 后输入密码，每行回车。每项15秒绝对超时，Wi-Fi连接最多30秒。密码不trim，凭据仅在内存保存。SCAN命令仅扫描当前目标热点，默认Shino。

## 2. 启动WSL的C服务

```powershell
wsl -d Ubuntu-24.04 --cd /mnt/d/Projects/esp32-linux-edge-scheduler -- bash scripts/run_wsl_edge.sh
```

保持终端运行。脚本显示本次Linux输出目录（默认 `$HOME/edge-results/esp32c3-wifi-link`；存在时自动选择v2/v3），监听9000，window=8，priority，capacity=32，passes=1。运行状态和CSV在Ubuntu原生文件系统，本机VHD实际位于D盘。按Ctrl+C排空队列后退出。

## 3. 用Wi-Fi接收并转发20个任务

Windows先连到Shino热点；不要仅让开发板连热点而电脑仍在校园网。另开PowerShell，在项目根目录：

```powershell
$python = 'D:\Codex\esp32-linux-edge-scheduler\tools\device-venv\Scripts\python.exe'
# 用步骤1实际输出的开发板IPv4替换占位符
& $python -m tools.wifi_bridge --device-host <开发板IP> --count 20 --output D:/Codex/esp32-linux-edge-scheduler/outputs/wifi-capture
```

Windows主动连接开发板TCP9001，发送 `START 20\n`。设备返回20帧并关闭该TCP连接；Linux收到的仍然是严格五字段NDJSON。每帧包含device_id=esp32c3-01、seq、priority、source=synthetic、samples。普通帧256个样本，紧急帧16个，每第五帧紧急；样本值 `((i + seq % 201) % 201) - 100`。这些是合成数据，不是真实传感器采集。

`frames.jsonl`记录校验后sendall成功的帧，`stats.json`记录数量和错误；forwarded只表示转发完成，不表示Linux已经接受或计算完成。转发遇到不确定的TCP发送、非法帧或超时后结束，不自动重放。接收缓冲上限8192字节，最多100帧/批，整个批次默认60秒超时。该上限适用于本设备小帧，不改变Linux262144字节协议上限。

## 4. 验证完成情况

读取Linux本次输出中的 `status.json` 和 `tasks.csv`，核对accepted/completed=20、rejected=0，seq和设备编号逐项对应。使用 `tools.protocol.reference(samples, window=8)` 核对每帧mean/rms，容差1e-8。CSV的total_ms只计算完整帧到达Linux之后的处理时间，不是ESP32到Linux的端到端延迟。

本机已完成真实无线验收：20帧（16普通、4紧急）经Wi-Fi转发到WSL，Linux完成20、拒绝0；样本、序号和mean/RMS逐项参考验证通过。配置脚本关闭后，另传5帧（seq20..24）并在新C服务中全部完成及验证，Wi-Fi任务传输独立于USB配置程序。实测是Windows转发进入WSL，不是直接访问WSL私网IP。证据位于 `D:/Codex/esp32-linux-edge-scheduler/outputs/esp32c3-wifi-link`。

若要重复数值核对，将本次Linux结果复制到D盘后运行：

```powershell
& $python -m tools.wifi_verify --capture <本次WiFi捕获目录> --service <本次Linux结果目录>
```

验证器要求服务只包含当前批次，重复实验请先结束原C服务并新建服务输出目录。

任务序号跨TCP连接递增，但设备复位归零，无去重、ACK或持久队列。批次结束后设备继续等待TCP客户端；开发板断Wi-Fi会停止服务并提示重新CONFIG，本版不宣称自动重连/恢复通过。关闭USB配置脚本不影响已经建立的Wi-Fi服务。重新上电需要重新配置凭据。

该测试端口不做鉴权或TLS，只用于可信实验局域网。不要将开发板9001或Linux9000暴露到公网。

## 自动测试

```bash
make -C linux
EDGE_SERVER="$PWD/linux/build/edge_scheduler" python3 -m unittest discover -s tests -v
```

新增 `tests/test_wifi_bridge.py` 使用真实本机TCP连接覆盖拆包/粘包、残帧EOF、超长帧、非法字段、数量不足和超额帧。此测试不冒充实际无线硬件测试；本机实测证据另存D盘outputs。
