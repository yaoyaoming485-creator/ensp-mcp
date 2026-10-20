# ensp-mcp

面向华为 **eNSP**（Enterprise Network Simulation Platform）的 MCP 服务器。
它给 Agent 提供 eNSP 本身没有暴露的三样东西：**把拓扑当文件写出来**、
**读取并驱动运行中设备的控制台**、以及**直接和路由器对话并拿到精确文本**。

全部内容均基于真实安装逆向得出 —— eNSP `V1.3.00.100` + Oracle VirtualBox `7.2.18`，
Windows 11。不用 eNSP SDK、不调未公开 DLL、不做 GUI 自动化：只用了 `.topo` 格式、
guest agent 的线路协议，以及 eNSP 本来就在驱动的 VirtualBox 控制接口。

> 英文版见 [README.md](README.md)。本文档对应同一份代码，命令与输出完全一致。

## 逆向出了什么

### 1. `.topo` 格式

拓扑就是 XML。它声明 `<?xml version="1.0" encoding="UNICODE" ?>` ——
**而这个声明是假的**。文件必须是 **UTF-8 无 BOM**。eNSP 1.3.00.100 自带的
40 个示例拓扑全是 UTF-8，eNSP 自己保存的文件也是，早期版本 V1.2.00.390 同样。

搞错的后果是 **eNSP 一点错误都不报**：它把拓扑加载成 0 台设备，状态栏显示
`总数：0`。UTF-16LE 文件（`encoding="UNICODE"` 属性暗示的、也是某些第三方生成器
会输出的那种）会被静默丢弃。本项目早期一直写的是 UTF-16，直到实测才发现；
现在 `ensp_read_topology` 会回报 `encoding`、`has_bom` 和 `loads_in_ensp`，
让这个错误变得可见。

`<topo>` 元素的 `version` 属性是**可选的** —— 自带的示例里就有光秃秃的
`<topo>`。写了的话会与已安装的 eNSP 版本对齐（`ensp_status` 会报出来），
默认回落到 `1.3.00.100`。

```xml
<?xml version="1.0" encoding="UNICODE" ?>
<topo version="1.3.00.100">
  <devices>
    <dev id="UUID" name="R1" poe="0" model="AR2220" settings="" system_mac=""
         com_port="2000" bootmode="0" cx="150.000000" cy="250.000000"
         edit_left="177" edit_top="304">
      <slot number="slot17" isMainBoard="1">
        <interface sztype="Ethernet" interfacename="GE" count="1" />
        <interface sztype="Ethernet" interfacename="GE" count="2" />
        <interface sztype="Serial" interfacename="Serial" count="2" />
      </slot>
      <slot isMainBoard="0" id="3" type="521" />
    </dev>
  </devices>
  <lines>
    <line srcDeviceID="UUID-A" destDeviceID="UUID-B">
      <interfacePair lineName="Copper" srcIndex="0" tarIndex="0" />
    </line>
  </lines>
  <shapes>…</shapes>
  <txttips>…</txttips>
</topo>
```

坐标是浮点数（`cx="150.000000"`），内联编辑器的偏移 `edit_left`/`edit_top` 是整数。

唯一容易踩的规则：**`srcIndex`/`tarIndex` 是设备模型展平后接口列表的下标**，
顺序就是 `<interface>` 元素出现的顺序 —— 不是槽位号，也不是丝印上的端口号。
`models.py` 为每个模型保存了这套下标空间。

### 2. 设备控制台是一个 TCP 服务，不是位图

这是最关键的一条发现，也**修正了本项目早先的结论**（以及其他关于 eNSP 的
分析文章）——它们都说控制台只能靠截 VirtualBox 屏幕来访问。

**VRP 进程跑在 guest 里面，并在 guest 的 TCP 40000 上提供纯文本控制台。**
在跑着的 eNSP 宿主上，`netstat` 里能直接看到这条连接：

```
TCP  192.168.56.1:51442  192.168.56.2:40000  ESTABLISHED  <eNSP_VBoxServer>
```

也就是说 **eNSP 客户端自己就是通过这条 socket 和路由器说话的**。直接连上去，
拿到的就是路由器的真实输出：

```
<Huawei>display interface brief
Interface                   PHY   Protocol InUti OutUti   inErrors  outErrors
Ethernet0/0/0               down  down        0%     0%          0          0
...
Ethernet0/0/8               down  down        0%     0%          0          0
NULL0                       up    up(s)       0%     0%          0          0
<Huawei>
```

这是**精确文本** —— 不用截图、不用 OCR、不用窗口自动化、也不需要提权。
顺带把「截屏读字」那套做法固有的两个问题一起消掉了：掉字符，以及
`---- More ----` 分页器把后续输入吞掉。

VirtualBox 虚拟屏是**另一回事**：对 AR 设备来说它显示的是 **Tiny Core Linux
guest**（`Core Linux`，内核 `3.0.21-tinycore`，主机名 `box`）停在登录提示符，
因为路由器是以另一个进程跑在同一台机器上的。所以 `ensp_console_screen`
返回的是 Linux 屏。屏幕类工具保留着，但**要跟路由器说话请用 `ensp_vrp_command`**。

### 3. guest agent 与宿主怎么通信

guest 启动 Tiny Core Linux 后会跑 `agentd static`（见设备镜像里的
`/opt/bootlocal.sh`）。`agentd` 就是与宿主协商的那一环，一共四条通道：

| # | 通道 | 方向 | 用途 |
| --- | --- | --- | --- |
| 1 | 串口（`/dev/ttyS1` ↔ 宿主 COM2 管道） | 双向 | 配置握手 |
| 2 | 控制（`guest:56789`） | 宿主 → guest | 启动 VRP 进程 |
| 3 | media（`host:<port>`） | guest → 宿主 | **客机主动回连宿主** |
| 4 | console（`guest:40000`） | 宿主 → guest | 路由器自己的 VRP CLI |

**通道 1 —— 配置握手。** `agentd` 往串口写一个 4 字节挑战
`htonl(rand())`，然后阻塞等回复。回复是 28 字节 + 27 字节：

| 偏移 | 长度 | 含义 |
| --- | --- | --- |
| `A[0:4]` | 4 | 原样回显挑战（否则客机记 `Invalid key.`） |
| `A[4:6]` | 2 | media port，**网络字节序**（客机内部会 `ntohs`） |
| `A[8:12]` | 4 | 客机自己的 IP |
| `A[12:16]` | 4 | 掩码（填 0 则客机跳过 `SIOCSIFNETMASK`） |
| `A[16:26]` | 10 | 设备 id 前 10 字节 |
| `B[0:27]` | 27 | 设备 id 后 27 字节 —— 合计 37 字节，即 "ssid" 密钥 |

之后客机用 `ioctl(SIOCSIFADDR/SIOCSIFNETMASK/SIOCSIFFLAGS)` 自己配好
`eth0`，再跑 `ip_test`/`ping_test`。**唯一必须正确的值是那个挑战**，
其余都由我们决定。

**通道 2 —— 控制协议。** 客机**监听** TCP 56789，双方用固定 44 字节帧：
`[u32 大端 code][37 字节 ssid][3 字节填充]`。

| code | 方向 | 含义 |
| --- | --- | --- |
| 0 | guest → host | boot（带着 ssid） |
| 1 | host → guest | 「放行」——释放 start 状态 |
| 2 / 3 | guest → host | prepare 成功 / 失败 |
| 4 / 5 | guest → host | DEV0 启动 / 失败 |
| 6 / 7 | guest → host | DEV1 启动 / 失败 |
| 8 / 9 | guest → host | DEV2 启动 / 失败 |

**这个 socket 必须一直开着。** `agentd` 注册了 `atexit(power_off)` 并 join
了自己的收消息线程，所以控制连接一断，整个客机就自己关机了。

**通道 3 —— media server。** 这条最反直觉：**客机主动连回宿主**的
`192.168.56.1:<media port>`，端口号就是我们在 `A[4:6]` 里给它的那个。
`send_ssid()` 随后把 SysV 消息队列里 type 7 的每一项按 244 字节帧推过来
（`[240 字节载荷][偏移 178 处的 37 字节 ssid]`）。宿主不监听的话，客机会记
`Failed to connect to remote media server: Connection refused.`，ssid 永远送不到。

**设备镜像。** 路由器本体是每台设备三个 ELF：`dev0`、`dev1`、`dev2` ——
启动时由 `agentd` 的 `decompress()` 从 `dev_base_0/1/2` 解出来。它们是
**货真价实的华为 VRP**（镜像里有 `hi3ms.huawei.com` 端点和 GVRP 协议栈）。
它们的 `resource.cfg` 把接口 socket 钉死在固定地址上；如果那个地址和客机
实际持有的 IP 不一致，VRP 就 bind 不了、直接挂掉：

```
bind: Cannot assign requested address
ERROR :Start port 0 fail,return -1
VPORT_Hal_PortEnable failed! u32Ret = -1
```

`scripts/patch_resource.py` 负责把地址改成宿主实际下发的那个。

## 安装

需要 Python 3.10+ 和 Oracle VirtualBox。只有运行时工具才需要 eNSP 本身。

```bash
uv sync
uv run ensp-mcp          # MCP 服务器
uv run enspctl --help    # 命令行
```

## `enspctl` —— 用命令行控制 eNSP

MCP 服务器是给 Agent 用的。`enspctl` 是同样东西的命令行版本：写拓扑、
把整个拓扑拉起成一个 **lab**（每台设备一台 VirtualBox 虚拟机，从 eNSP 自己的
设备镜像克隆），以及和路由器对话。

```console
$ enspctl topo create --spec spec.json -o lab.topo
wrote lab.topo (1767 bytes, utf-8)
  2 devices, 1 links, loads_in_ensp=True

$ enspctl lab create demo --topo lab.topo --root ./demo --with-vms
lab 'demo' created in ./demo
  R1         model=AR2220     vm=demo-R1   ip=192.168.56.101   pipe=\\.\pipe\enspctl-demo-R1
  R2         model=AR2220     vm=demo-R2   ip=192.168.56.102   pipe=\\.\pipe\enspctl-demo-R2
  demo-R1: cloning from AR_Base ...
  demo-R2: cloning from AR_Base ...

$ enspctl lab up --root ./demo
patching resource.cfg ...
  R1: 9 socket line(s) -> local 192.168.56.101, dest 192.168.56.1
  R2: 9 socket line(s) -> local 192.168.56.102, dest 192.168.56.1
preparing the guest agent drivers ...
starting machines ...
  demo-R1: starting
  demo-R2: starting
R1: control: <- boot
R1: control: -> 1 (go)
R1: control: <- prepare OK
R1: control: <- DEV0 started
...
  R1  started
  R2  started
lab is up; press Ctrl-C to stop the drivers

$ enspctl run R1 "display version" --root ./demo
=== display version
Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.130 (AR200 V200R003C00)
...

$ enspctl lab status --root ./demo
lab demo  (./demo)
device     vm                    ip               state      agentd vrp
R1         demo-R1               192.168.56.101   running    yes    yes
R2         demo-R2               192.168.56.102   running    yes    yes

$ enspctl lab down --root ./demo
```

### 命令一览

| 命令 | 作用 |
| --- | --- |
| `enspctl topo create --spec s.json -o x.topo` | 从 JSON spec 生成拓扑 |
| `enspctl topo read x.topo` | 解析拓扑为 JSON |
| `enspctl topo validate --spec s.json` | 校验 spec |
| `enspctl lab create NAME --topo x.topo --root DIR` | 建 lab（加 `--with-vms` 克隆虚拟机） |
| `enspctl lab up --root DIR` | 改写配置、启动并驱动整个 lab |
| `enspctl lab down --root DIR` | 关闭设备 |
| `enspctl lab status --root DIR` | 查看设备状态与是否应答 |
| `enspctl lab patch --root DIR` | 改写每台设备的 `resource.cfg` |
| `enspctl lab destroy --root DIR` | 关闭并删除虚拟机 |
| `enspctl dev list` | 列出 host-only 网段上所有 eNSP 客机 |
| `enspctl dev exec R1 "display version"` | 执行 VRP 命令，返回精确文本 |
| `enspctl dev shell R1` | 交互式 VRP 会话 |
| `enspctl run R1 "display version"` | `dev exec` 的简写 |

### 一个 lab 做了什么

lab 拥有一个目录：拓扑、每台设备一台 VirtualBox 虚拟机、以及一份清单。对每台设备它会：

* 克隆 eNSP 自己的 `AR_Base` 机器，所以客机是真的 eNSP 设备
  （差异盘里带着 `/dev/upload`：`agentd`、`resource.cfg`、VRP 二进制）；
* 把它挂到 host-only 网段，并分配一个空闲地址；
* 把它的 COM2 串口设成 **client 模式**，让 VirtualBox 来连 lab 自己起的管道服务
  —— VirtualBox 自建的管道只给宿主读权限；
* 改写 `resource.cfg`，让 VRP 的接口 socket 指向 lab 实际下发的地址 ——
  不改的话 VRP 会以 `VPORT_Hal_PortEnable failed!` 挂掉。

然后 `lab up` 就像 eNSP 客户端那样驱动每个客机：串口握手、控制帧、media server。

> lab 的虚拟机**不是** eNSP 应用管理的那批，也不会出现在 eNSP 画布上。
> `enspctl` 是通往同一批设备的另一条、可脚本化的路径。

### 用项目自己的 venv 启动，不要用 `uvx`

`uvx --from <dir> <entrypoint>` 看起来是最顺手的写法，而且在你不改源码之前
它确实能跑。但 `uv` 会构建 wheel 并缓存到
`%LOCALAPPDATA%\uv\cache\archive-v0\<hash>\Lib\site-packages\ensp_mcp\`，
**包版本号不变时它会一直跑旧代码**。改完源码后你看到的还是旧行为，很容易
误判成「改了没用」。

直接用项目 venv 里的解释器（editable 安装，改源码即时生效）：

```bash
.venv\Scripts\python.exe -m ensp_mcp
```

DSH 的 MCP 配置片段：

```yaml
- id: mcp-ensp
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: ensp
    transport: stdio
    command: 'D:\work\ensp-mcp\.venv\Scripts\python.exe'
    args: ['-m', 'ensp_mcp']
    toolCallTimeoutMs: 120000
```

## 工具

**拓扑**

| 工具 | 作用 |
| --- | --- |
| `ensp_list_models` | 设备型号目录，带接口下标顺序 |
| `ensp_create_topology` | spec → `.topo` 文件 |
| `ensp_read_topology` | `.topo` → 结构化 JSON |
| `ensp_validate_topology` | 校验 spec，警告缺失的设备镜像 |
| `ensp_open_topology` | 用指定文件启动 eNSP GUI |

**运行时**

| 工具 | 作用 |
| --- | --- |
| `ensp_status` | eNSP / VirtualBox 是否在、什么在跑 |
| `ensp_list_devices` | 设备 VM 及其控制通道 |

**真实 VRP CLI**

| 工具 | 作用 |
| --- | --- |
| `ensp_vrp_list_devices` | 找出 host-only 网段上的设备及其开放的端口 |
| `ensp_vrp_command` | 执行 VRP 命令，返回路由器的**精确文本** |
| `ensp_vrp_agent_handshake` | 跑一遍 agentd 控制握手，回报设备说了什么 |

**控制台（VirtualBox 虚拟屏）**

| 工具 | 作用 |
| --- | --- |
| `ensp_console_screen` | 截屏，返回 PNG |
| `ensp_console_type` | 输入文本（可选回车） |
| `ensp_console_key` | 发具名按键（`ctrl-z`、方向键、`F1`…） |
| `ensp_console_send_lines` | 逐行发送一段配置 |

凡是要跟路由器说话，都用 `ensp_vrp_command`：它给文本、不掉字符、
也不受分页器影响。屏幕类工具驱动的是 guest 的 Linux 控制台。

```jsonc
// ensp_vrp_command(guest_ip="192.168.56.101", commands=["display version"])
{
  "ok": true,
  "output": "=== display version\nHuawei Versatile Routing Platform Software\n\
             VRP (R) software, Version 5.130 (AR200 V200R003C00)\n..."
}
```

### 不依赖 eNSP 客户端拉起设备

`scripts/host_side.py` 可以替掉 eNSP 客户端管理一台设备：管道服务、media server、
控制客户端，全在一个进程里。

```bash
# 设备的 UART 必须指向我们自建的管道（见 evidence/agentd-protocol.md）
VBoxManage startvm MYR2 --type headless
python scripts/host_side.py --pipe '\\.\pipe\MYR2-COM2' --guest-ip 192.168.56.101
python scripts/vrp_cli.py --guest 192.168.56.101 display version
```

`scripts/patch_resource.py` 改写客机的 `resource.cfg`，让 VRP 的接口 socket
指向客机真正持有的地址 —— 不改的话 VRP 会以 `VPORT_Hal_PortEnable failed!` 挂掉。

### 实际效果

```console
$ python scripts/vrp_cli.py --list
192.168.56.2
192.168.56.3
192.168.56.4
192.168.56.5
192.168.56.101

$ python scripts/vrp_cli.py --guest 192.168.56.101 'display version' 'display clock'
=== display version
display version

Huawei Versatile Routing Platform Software

VRP (R) software, Version 5.130 (AR200 V200R003C00)

Copyright (C) 2011-2012 HUAWEI TECH CO., LTD

Huawei AR201 Router uptime is 0 week, 0 day, 0 hour, 1 minute
...
<Huawei>
=== display clock
display clock

2026-10-20 20:51:22

Tuesday

Time Zone(China-Standard-Time) : UTC-08:00

<Huawei>
```

## 验证

两套测试，都是绿的：

| 套件 | 覆盖内容 |
| --- | --- |
| `tests/smoke.py` | 模块级：型号目录、`.topo` 构建/写/读往返、校验失败路径、解析真实 eNSP 拓扑、VirtualBox 盘点、截屏、键盘注入、**真实 VRP 控制台、agentd 控制握手** |
| `tests/mcp_client.py` | 按 JSON-RPC stdio 驱动服务器：像 Agent 一样 `initialize`、`tools/list`，然后**逐个调用全部 14 个工具**并检查结果 |

`mcp_client.py` 是真正的验收测试 —— 它断言服务器公布了全部 14 个工具，
且 `ensp_console_screen` 返回的 `content types=['text', 'image']`、
`mimeType: image/png`。

设备独占时（eNSP 客户端正连着那台设备）相关检查会标 **SKIP** 而不是 FAIL ——
工具会明确报告「被占用」，而不是假装成功。

## 已知限制

* **屏幕类工具仍有老问题。** `ensp_console_screen` 是位图，
  `ensp_console_type` 是往 guest 的 Linux 控制台里打字，所以照样掉字符、
  照样拿不到文本。请改用 `ensp_vrp_command`。
* **VRP 控制台同一时刻只服务一个客户端。** eNSP 客户端在驱动某台设备时
  会占着它的控制台会话，第二个客户端能连上但收不到东西。工具会如实报告，
  不会干等。
* **缺镜像的设备。** `NE40E`、`NE5000E`、`NE9000`、`CE6800`、`CE12800`、
  `CX`、`USG6000V` 需要 eNSP 安装包不带的外部镜像（`CE.img`、`NE40E.img`、
  `vfw_usg.vdi`…）。用到它们的拓扑本身是合法的，但设备要等镜像导入后才能启动。
* **偏 Windows。** 设备发现依赖 Windows 注册表和 `tasklist`；拓扑工具是通用的。

## 出处

所有测量都来自真实安装。

* [`evidence/agentd-protocol.md`](evidence/agentd-protocol.md) —— guest agent 的
  四条通道，通过反汇编客机的 `agentd` 二进制得出，并在活的客机上验证。
  （该二进制是 eNSP 镜像里的厂商文件，不随本仓库分发；文档给出了它的
  SHA-256 和提取方法。）
* [`evidence/boot_capture.log`](evidence/boot_capture.log) —— 从 VM 上电那一刻起
  抓取串口管道，由 [`evidence/capture_boot.py`](evidence/capture_boot.py) 生成。
* [`evidence/console-*.png`](evidence/) —— guest 的虚拟屏截图。

MIT 许可。
