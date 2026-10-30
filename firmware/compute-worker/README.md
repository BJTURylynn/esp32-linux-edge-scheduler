# v2 板上计算固件

这个本地 Arduino 库供 Uno `ComputeWorker` 和 ESP32-C3 `ComputeWiFi` 共用，不依赖 ArduinoJson。旧 `EdgeDevice` / `EdgeWiFi` 工程不变，重新烧录旧工程即可恢复原来的数据上传演示。烧录新工程会替换板上原程序。

## 构建

在仓库根目录使用已安装的 Arduino CLI，额外传入单库路径：

```text
arduino-cli compile --fqbn arduino:avr:uno --library <compute-worker库目录绝对路径> firmware/coloreasyduino/ComputeWorker
arduino-cli compile --fqbn esp32:esp32:esp32c3:FlashMode=dio,FlashFreq=40,FlashSize=8M --library <compute-worker库目录绝对路径> firmware/esp32c3-arduino/ComputeWiFi
```

Uno 使用 AVR core 1.8.8；ESP 使用 ESP32 Arduino core 3.3.12，保持已实测的 DIO/40MHz/8MB 设置。COM 口以实际枚举结果为准。库目录必须跟随工程保留，不能只复制 `.ino`。

## 设备任务与协议

两个设备均实际执行滑动平均、均值和 RMS，任务只携带合成样本的生成参数。`count=1..256`、`window=1..16`、`passes=1..1000`、`seed=uint32`，样本 `((i + seed % 201) % 201) - 100`。有限窗口使用 16 个 `int16_t`，累积及结果使用 `float`。每轮从空窗口重算，最终返回最后一轮结果；volatile 累积阻止编译器将相同的多轮计算合并成一轮。

协议是最大256字节（不含LF及可选CR）的平面ASCII JSON。未知、重复字段、越界数字、带转义字符串、残缺对象及超长行不会执行计算。传输拆包不会影响逐字节组帧，超长行丢弃到LF后恢复。

- 固定设备 ID：`uno-01`、`esp32c3-01`。
- 连接后设备发 `{"v":2,"type":"hello","device":"uno-01"}`，中央回 `welcome`，提供16位小写十六进制 `session`。
- `task` 必须具有 `v,type,session,id,seed,count,window,passes` 八字段。任务编号为中央每次运行的 `1..512`，与中央最多512条记录的上限一致。匹配会话且未执行过的编号才执行；使用64字节位图查重，**不要求编号递增**，因为紧急任务可先于更早入队的普通任务执行。同会话重复 `welcome` 不清除位图，新会话或启动 `probe` 才清除。
- 设备先输出并刷新 `accept`，再开始计时和计算，最后输出 `result`；结果包含 `device,session,id,mean,rms,compute_us`。数值打印6位小数，不含样本数组。
- `ping` 对应 `pong`，都回显会话。设备没有应用任务队列，也不选择优先级。
- 安全关联到当前任务的参数错误返回 `error/invalid_task`；重复编号返回 `error/duplicate_task`。无可信任务标识的畸形输入直接丢弃，不能报告成功。
- **串口网关重连握手**：在打开串口、设备复位等待和输入清空之后发送 `{"v":2,"type":"probe"}`。设备清空旧会话并重发 `hello`。网关只能在启动/空闲握手时发probe，不能在计算中用它取消任务。设备未收到welcome时也会每秒重发hello，网关应只转发首次握手。

`compute_us` 是 MCU `micros()` 在接受确认发送后、结果发送前测量的本机耗时，不含网关和网络往返；Uno计时包含维持D13心跳的少量回调开销。它不是Unix时间戳，不能与Linux时钟相减。

## 各板连接

Uno串口115200，D13约每1000ms切换；计算中也调用心跳更新。所有串口输出均为v2协议行。

ESP32-C3 TCP9001接受一个网关连接，任务及结果仅经Wi-Fi。USB保留配置诊断：`CONFIG`，随后SSID行、密码行；`SCAN`；以及只读`STATUS`。STATUS返回`WIFI_STATUS state=connected|disconnected ip=... port=9001 rssi_dbm=... protocol=2`，不包含SSID或密码。TCP已连接时仍可查询，最长计算中的查询会等当前计算返回后处理；此时CONFIG／SCAN会明确拒绝，须由工作台检查空闲并停止自有ESP网关后再更换网络。

密码不回显、不写源码或持久配置。配置方式与原 `tools.wifi_configure` 兼容；新的网页连接入口自动使用板端IP重启ESP网关，旧固件仍可CONFIG，但要使用STATUS需重新烧录本版。Wi-Fi断开后等待重新配置。网络发送使用固定小缓冲和3秒绝对期限，避免调用可能清空接收缓存的 `NetworkClient.flush()`。

## 主机验证

`firmware/tests` 的 CMake 测试增加共享内核参考验证和Arduino运行时stub验证，涵盖最大参数、uint32种子、严格解析、会话、重复编号、确认先于结果、超长/CRLF/二进制输入、probe与重新握手。主机stub只验证代码行为，不代替实际编译、烧录、RAM报告及两块实物运行验收。
