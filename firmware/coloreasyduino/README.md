# ColorEasyDuino USB 设备端

目标为立创粉色 ColorEasyDuino（ATmega328P，Arduino Uno 兼容），不是 GD32 梁山派，也不是能运行 Linux 的开发板。Linux 计算端运行在电脑 Ubuntu 中；本设备端通过 CH340 USB 串口连接主机转发程序，不使用 Wi-Fi。

## 程序行为

- 工程：`EdgeDevice/EdgeDevice.ino`；Arduino IDE 开发板选择 **Arduino Uno**，串口选择实际 CH340 COM 端口。
- 波特率 115200；设备编号 `uno-01`，序号从 0 开始。串口仅输出现有五字段 NDJSON，不输出调试文本。
- 数据明确标记 `source: synthetic`。样本公式为 `((i + (seq % 201)) % 201) - 100`，范围 -100..100；没有传感器采集。
- 每 5 帧的第 5 帧为 urgent（序号 4、9、14……），包含 16 个样本；其余 normal 帧包含 256 个样本。
- 一帧完整发送后至少等待 250 ms，再发送下一帧；帧串行发送时间另计，因此不是固定 4 Hz 采样。
- 13 口灯每 1000 ms 尝试切换，用于运行心跳。板载灯与 13 口外接灯共用同一引脚；它不代表 Linux 已接收、入队或完成任务。
- 使用 `F()` 将常量字符串放入 Flash，逐字段、逐样本输出，没有整帧或样本数组，适应 Uno 的 2 KiB SRAM。

`Serial.print()` 与 `Serial.flush()` 会等待 UART 发送，心跳时间可能出现延后；程序不是硬实时控制器。设备不接收应用层 ACK，没有离线存储或自动重发；拔插或打开串口可能复位，序号从 0 重新开始，`device_id + seq` 不保证全局唯一。

## 构建和上传

已配置 Arduino CLI 及 `arduino:avr` 后，在仓库根目录执行：

```powershell
arduino-cli compile --fqbn arduino:avr:uno firmware/coloreasyduino/EdgeDevice
arduino-cli upload --fqbn arduino:avr:uno --port COM10 firmware/coloreasyduino/EdgeDevice
```

`COM10` 仅为当前电脑曾识别到的端口示例，上传前必须核对。上传与主机桥接不能同时占用串口；关闭桥接或 IDE 串口监视器后再上传。

完整链路为 **设备串口 → 主机串口转 TCP 桥接 → Linux C 调度器**。主机转发成功不表示 Linux 已计算成功，验收以 Linux 的任务 CSV、状态及数值参考对照为准。串口转发、启动和实测步骤以仓库主说明为准。

编译期常量集中在文件顶部，可修改设备编号、普通/紧急样本数、紧急帧比例、发送间隔和心跳间隔。变更后需要重新编译上传。不要沿用 ESP32 的 8192 样本内存分配到本板。
