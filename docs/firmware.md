# ESP32 固件：本地实现与上板步骤

## 当前状态与版本

代码位于 `firmware/`，使用 ESP-IDF **v5.5.6**，默认目标为经典 ESP32（如 ESP32-WROOM-32）。ESP32-S3、C3 等板卡必须先核对芯片再更改 `idf.py set-target`；未验证这些目标。2026-10-23 已在本地 VMware Ubuntu 中完成 **ESP-IDF v5.5.6 的真实 ESP32 编译与链接**，生成 app、bootloader 和分区表，完整构建退出码为 0。主机编码器和固件帧到 Linux 服务的跨端测试也已通过。**未烧录、未连接真实 Wi-Fi、未完成开发板断线验证**，构建通过不等于硬件验收通过。

版本选择核对于 2026-10-23：[官方 v5.5.6 发布页](https://github.com/espressif/esp-idf/releases/tag/v5.5.6)确认这是 v5.5 的修复版本；5.5 分支仍在维护支持期。该版本作为可复现基线，不等于宣称它是最新主版本。接口依据 [v5.5.6 Wi-Fi 驱动文档](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32/api-guides/wifi.html)、[lwIP/BSD socket 文档](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32/api-guides/lwip.html)以及 [ESP Timer 文档](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32/api-reference/system/esp_timer.html)。

## 本次真实构建记录

- SDK 固定标签：`v5.5.6`，主仓库提交：`880a203ae603d08a94d06aa01e0d6761958e5e9e`。
- 环境：本地 VMware Ubuntu，ESP32 目标，Xtensa GCC 14.2.0（`esp-14.2.0_20260121`）、Python 3.12.3；使用 `IDF_PY_BUILD_JOBS=1`，实际执行 `ninja -j 1 all`。
- 结果：742/742 构建步骤完成，`main.c`、`frame.c` 已编译并成功链接；应用镜像 **767,568 字节**，默认 1 MiB app 分区剩余约 27%。
- 当前产物使用空 SSID、空密码，启动时会明确提示配置无效并停止；**不能直接用于联网演示**，需要填写真实局域网参数后重新编译。
- 已检查最终 `app_main` 反汇编，保留了 Wi-Fi 连接、TCP 连接、帧生成、`send_all` 和主循环跳转路径，确认网络实现实际参与编译与链接。

产物已复制回本机项目目录 `F:\项目\嵌入式+linux\.local\firmware-build\`：

| 文件 | 内容 |
| --- | --- |
| `build-default.log` | 包含版本、配置、编译、链接和分区大小检查的完整成功日志 |
| `edge_scheduler_device.bin`、`edge_scheduler_device.elf` | 默认空网络配置的应用镜像和 ELF |
| `bootloader/bootloader.bin` | 引导程序 |
| `partition_table/partition-table.bin` | 分区表 |
| `flasher_args.json`、`flash_args` | ESP-IDF 生成的地址/文件参数，仅保存，未执行烧录 |
| `sdkconfig` | 本次构建配置，已核对 SSID/password 都为空 |
| `app_main-disassembly.txt` | 主函数反汇编，包含 Wi-Fi/TCP 主循环调用 |
| `manifest.json` | SDK 提交、编译环境、源码与产物 SHA256、未烧录状态 |

应用镜像 SHA256：`42119d9db83c71ebc4526db5b6797b39c4053e6ac623be7454de4c3a951c61eb`。回传时已逐字节确认 VM 构建输入与本地对应固件源码相同。

VM 的 `/home/edge/.local/esp/esp-idf` 是**官方固定提交的必要组件源码快照**：主源码与所用子模块均从已核对的 Git 提交导出，保留 Unix LF 和文件权限，含本项目需要的 Wi-Fi/PHY/Coex/lwIP/mbedTLS 等组件，**不带 `.git`，也不是完整官方 SDK 检出**。因此日志会显示 Git 版本不可用并回退读取源码版本，实际报告 `ESP-IDF v5.5.6`。以后增加 BLE、OpenThread 等组件，应补齐对应固定提交子模块，或另外按官方 `git clone -b v5.5.6 --recursive https://github.com/espressif/esp-idf.git` 建立完整 SDK。宿主的完整递归检出缓存保留在项目 `.local/esp/esp-idf`。

在当前 VM 重现默认构建：

```bash
export IDF_TOOLS_PATH=/home/edge/.local/esp/tools
. /home/edge/.local/esp/esp-idf/export.sh
cd /home/edge/project/firmware
IDF_PY_BUILD_JOBS=1 idf.py build
```

## 固件行为

1. 检查 Wi-Fi 配置、设备 ID 与服务器 IPv4，初始化 NVS、ESP-NETIF 和 Wi-Fi STA。
2. 连接 2.4 GHz WPA2/WPA3 网络，获得 DHCP IPv4 后才创建 TCP socket。事件回调只更新状态，应用任务独占 socket 操作，避免跨任务关闭描述符。
3. 生成有界、可重复的**合成测试数据**，每行一个 JSON，以 `\n` 分帧。整数样本取值范围为 `[-1000,1000]`；序号与样本下标共同决定数值。
4. 默认每 5 帧中前 4 帧为 2048 点 `normal`，第 5 帧为 64 点 `urgent`；每帧成功写入本地 TCP 后至少等待 250 ms。此间隔不包含编码、连接和发送耗时，因此不是精确采样周期。`urgent` 只是任务标记，实际排队行为由 Linux 的调度策略决定。
5. `send_all()` 处理短写、`EINTR`、`EAGAIN/EWOULDBLOCK`。socket 全程非阻塞，用 `select()` 和单调时钟为整个连接/整帧发送设置总期限，默认各为 10 s，避免单个短写反复刷新超时。等待期间每 250 ms 检查 Wi-Fi 变化。
6. Wi-Fi 关联及 DHCP 默认最多等待 15 s。失败后取消本次连接，按 1、2、4、8、16、30 s（封顶）退避；TCP 连接/发送失败有独立的同样退避。地址变更或 Wi-Fi 断开会使旧 socket 失效，重连后重新建立 TCP。

协议示例：

```json
{"device_id":"esp32-01","seq":1,"priority":"normal","source":"synthetic","samples":[-999,-962,-925]}
```

设备 ID 只允许 1–32 个 ASCII 字母、数字、`_`、`-`。`seq` 为 1 到 `9007199254740991` 的整数；达到上限即停止生产，不回绕。每帧 1–8192 个样本；单个发送缓冲区按最大配置批量分配一次，最多 49,344 字节，包含结尾 NUL。NUL 不发送，换行发送；实际帧远小于 Linux 协议的 262,144 字节上限。发生缓冲区分配或编码错误会停止生产并记录原因。

## 交付语义和限制

**当前协议没有应用层 ACK。** 日志 `accepted by local TCP` 只说明完整帧已被设备的 TCP 栈接受，不能说明 Linux 已接收、入队或计算完成；Linux 队列满时仍可能拒绝任务。是否完成以 Linux 任务日志和状态为准。

失败帧会被丢弃，关闭原连接，不在新连接上续发残帧，也不自动重放整帧。每个尝试发送的帧都会消耗序号，日志可能出现间隔；发送失败时该帧是否已经到达服务器未知。重启后序号从 1 开始，不跨重启持久化。当前不提供无丢失、严格去重、跨重启唯一编号或 exactly-once 保证。

数据为明文 TCP，只用于本地局域网实验。所有上传记录均固定标注 `source=synthetic`。本实现不读取传感器，不能把其结果表述成真实环境采集。若以后需要可靠投递，应先扩展双方协议，增加会话标识、入队/完成 ACK、持久化与去重机制，再设计重传。

## 在本机只编译

用 [ESP-IDF 官方安装指南](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32/get-started/index.html)安装/激活 **v5.5.6** 的开发环境。在官方 ESP-IDF PowerShell 中运行：

```powershell
cd 'F:\项目\嵌入式+linux\firmware'
idf.py --version
idf.py set-target esp32
idf.py menuconfig
idf.py build
```

在 `Edge scheduler device` 菜单填写真实参数。编译阶段不需要插板，也不会烧录。经典 ESP32 只支持 2.4 GHz，热点/路由器应提供此频段。

| 配置 | 默认值 | 设置方式 |
| --- | --- | --- |
| Wi-Fi SSID / password | 空，运行时拒绝启动 | SSID 1–32 字节，WPA2/WPA3 密码 8–63 字节或 64 个十六进制字符 |
| Server IPv4 / port | `192.168.1.100:9000` | 改为 ESP32 可以访问的 Linux IPv4；需与服务监听端口一致 |
| Device ID | `esp32-01` | 多设备时各自唯一 |
| Normal / urgent samples | 2048 / 64 | 各 1–8192，通常紧急批量更小 |
| Every Nth frame urgent | 5 | 0 全部普通，1 全部紧急 |
| Send interval | 250 ms | 10–60000 ms |
| Socket deadline | 10000 ms | 100–60000 ms，连接与整帧发送各自计时 |
| Wi-Fi deadline | 15000 ms | 1000–60000 ms，关联和 DHCP 合计 |

也可以用模板生成首次配置：

```powershell
Copy-Item sdkconfig.example sdkconfig.local
# 编辑 sdkconfig.local 后，在尚未生成 sdkconfig 的全新构建中使用：
idf.py -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.local' set-target esp32
idf.py -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.local' build
```

已有 `sdkconfig` 的值优先于 defaults，此时用 `menuconfig` 修改。`sdkconfig`、`sdkconfig.old`、`sdkconfig.local` 和 `build/` 已忽略；真实密码会进入本地配置及固件二进制，勿上传这些文件或公开构建产物。仓库的 `sdkconfig.example` 仅包含占位符。

Linux 容器中可只编译而不烧录：

```bash
docker run --rm -v "$PWD:/project" -w /project/firmware espressif/idf:v5.5.6 idf.py build
```

在仓库根目录执行这条命令。默认空 SSID/password 允许编译，但上板运行时会提示配置无效并停止，这是预期行为。

## 帧编码的主机测试

编码模块 `main/frame.c` 不依赖 ESP-IDF，可以先检查协议与缓冲区边界：

```bash
cmake -S firmware/tests -B build/firmware-host
cmake --build build/firmware-host
ctest --test-dir build/firmware-host --output-on-failure
build/firmware-host/test_frame --emit-max > build/firmware-max-frame.ndjson
```

测试覆盖准确输出、设备 ID 字符和长度、最大安全序号、8192 点上限、空/非法输入，以及每一种过小缓冲区都不越界。最大帧输出还可交给 Linux 服务验证两端兼容。仓库集成测试 `tests/test_firmware_protocol.py` 会把该 C 输出按 137 字节分片送入真实服务，并对照 Python 参考计算；运行前设置 `EDGE_SERVER` 为服务可执行文件绝对路径、`EDGE_FRAME_TEST` 为 `test_frame` 可执行文件绝对路径。CI 已配置这些步骤。它不验证 ESP-IDF 链接、无线驱动、FreeRTOS 调度、TCP 断线恢复或真实设备内存状况。

## 之后上板和网络验证

以下步骤供具备板卡和正确配置后执行；本次没有执行。

1. 先启动 Linux 服务，在 Linux 端确认监听了可从 LAN 访问的地址和 9000 端口。虚拟机优先采用桥接，使 VM 和 ESP32 位于可互通的局域网；若采用 NAT，则必须按 VMware 配置可达的端口转发。`127.0.0.1` 是设备自己的回环地址，不能用作服务器地址。访客网络或 AP 隔离也可能阻断访问。
2. 填好配置并完成 `idf.py build` 后，确认板卡型号、USB 串口和供电。示例：`idf.py -p COM5 flash monitor`，其中 COM5 必须替换为实际端口。终端退出为 `Ctrl+]`。Linux USB 示例端口为 `/dev/ttyUSB0`，是否具备访问权限由当地系统决定。
3. 串口应依次出现获得 IPv4、TCP connected、`source=synthetic` 与递增序号的发送日志；Linux 日志应出现同设备 ID 和序号。核对第 5、10 等帧为 `urgent`，样本数分别与配置一致。
4. 关闭 Linux 服务再启动，观察 TCP 错误、退避和重新连接；验收依据是新序号在 Linux 日志恢复出现，不是只看到串口发送成功。
5. 关闭 Wi-Fi AP 约 30 s 后恢复，检查断线事件、重新获取 IP、创建新 TCP，且不重复初始化 NVS/整个网络栈；重复至少 10 次，记录是否出现复位或内存持续下降。该项现在仍待硬件验收。
6. 用错误的服务地址测试连接总期限，使用接受连接但不读取的测试服务并降低间隔/增大批量，持续运行到 TCP 缓冲区填满，确认整帧发送超时后重连且设备没有卡死。网络栈初期可能缓存多帧，不能期望第一帧立即超时。
7. Linux 队列满时核对服务端拒绝记录；固件不会收到拒绝 ACK。恢复演示前恢复正常工作负载。

建议保存固件版本、板卡/芯片、配置（隐去密码）、串口日志、Linux 日志、断线时间与恢复时间。只有完成这些实际步骤后，才能把原计划的“ESP32 断线可恢复”验收项标记通过。
