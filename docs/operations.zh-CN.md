# 操作手册 —— 用 `enspctl` 跑一个 eNSP 实验

这份文档只讲**怎么操作**：从零跑通一个 lab、下发配置、验收、以及出问题时怎么查。
原理和逆向证据在 [`README.zh-CN.md`](../README.zh-CN.md) 与 [`evidence/`](../evidence/)。

全部命令都在 Windows 上执行，**不需要 eNSP 客户端界面**。

---

## 0. 前置条件

| 需要 | 说明 |
| --- | --- |
| VirtualBox | 已安装，`VBoxManage.exe` 可执行 |
| eNSP | 只需装过一次（它为每类设备注册了 `AR_Base` 等基础虚拟机镜像）；之后不需要启动它 |
| WSL | 改镜像里的 `resource.cfg` 要在 WSL 里挂载磁盘，需要 `wsl -u root` 可用 |
| Python | 用项目自己的 venv：`.venv\Scripts\enspctl.exe`（**不要用 `uvx`**） |

检查环境：

```powershell
enspctl status      # VBoxManage 路径、eNSP 是否在跑、已注册虚拟机数
enspctl models      # 可用的设备型号目录
```

> `status` 里的 `eNSP running` 只要看到 `eNSP_VBoxServer.exe` 就会显示 True。
> 那个进程是 eNSP 自己的宿主服务，**不影响** `enspctl`；客户端进程 `eNSP_Client.exe` 才是 GUI。

---

## 1. 五分钟跑通一个 lab

### 1.1 写拓扑

拓扑用一个 JSON 描述，`topo create` 会生成 eNSP 能打开的 `.topo`。

```json
{
  "devices": [
    {"name": "R1", "model": "AR1220", "ports": ["GE0/0/0"]},
    {"name": "R2", "model": "AR1220", "ports": ["GE0/0/0"]}
  ],
  "links": [
    {"a": "R1", "b": "R2", "type": "Copper", "a_port": 0, "b_port": 0,
     "a_guest_port": 8, "b_guest_port": 8}
  ]
}
```

```powershell
enspctl topo validate --spec spec.json      # 先校验
enspctl topo create --spec spec.json -o lab.topo
```

> **`a_guest_port` / `b_guest_port` 很重要**，原因见 §3。生成的 `.topo` 里它们是
> `guestSrc` / `guestTar` 属性；不写就按面板口编号原样映射（真 eNSP 的行为）。

### 1.2 建 lab（会克隆虚拟机）

```powershell
enspctl lab create mylab --topo lab.topo --root D:\work\mylab --with-vms
```

- `--with-vms` 才会克隆虚拟机；不加则只写 lab 元数据
- 默认 `--nic hostonly`；host-only 网卡坏掉时用 `--nic nat`（见 §4.1）
- 克隆是**完整克隆**，一台约一到两分钟

### 1.3 起设备

```powershell
enspctl lab up --root D:\work\mylab
```

这一步做的全部事情（不需要 eNSP）：

1. 改写镜像里的 `resource.cfg`，把接口地址指向本机
2. 为每个设备起一个**串口命名管道**服务，应答客机的握手（IP / 掩码 / 媒体端口 / 设备 ID）
3. 连客机的控制通道 `56789`，发 `go`，等 `prepare OK` / `DEV0` / `DEV1` / `DEV2`
4. 起媒体服务，接收客机连上来的 TCP
5. 为**每个接口**开一个 UDP relay 端口（`30000 + 设备序×16 + 接口号`），转发帧到对端客机

看到 `lab is up` 就绪。前台观察加 `--foreground`。

### 1.4 下发配置

```powershell
enspctl run --root D:\work\mylab R1 "display ip interface brief"
enspctl run --root D:\work\mylab R1 "system-view" "interface Ethernet0/0/8" "ip address 10.0.0.1 24" "quit"
enspctl run --root D:\work\mylab R2 "system-view" "interface Ethernet0/0/8" "ip address 10.0.0.2 24" "quit"
```

### 1.5 验收：ping 通

```powershell
enspctl run --root D:\work\mylab R1 "ping -c 4 10.0.0.2"
```

预期（本项目已实测）：

```
Reply from 10.0.0.2: bytes=56 Sequence=1..4 ttl=255
4 packet(s) transmitted, 4 packet(s) received, 0.00% packet loss
```

### 1.6 收摊

```powershell
enspctl lab down --root D:\work\mylab
```

---

## 2. 接口命名：为什么 IP 配在 `Ethernet0/0/8`

镜像自带的是 **AR201 的板子**（8 个交换口 + 1 个上行口），`display device` 会直接这么打印：

```
AR201's Device status: ... 0 - AR201 Present PowerOn Registered Normal Master
```

后果：

| 现象 | 原因 |
| --- | --- |
| `display interface brief` 列出 `Ethernet0/0/0`–`0/0/8` | 都是物理口 |
| `display ip interface brief` **只列出 `Ethernet0/0/8`** | 前 8 个是二层交换口，不能配 IP |
| `interface Ethernet0/0/0` 报 `Unrecognized command` | 同上 |

所以在找到「让客机按型号选板」的办法之前：

- IP 配在 **`Ethernet0/0/8`**（唯一三层口）
- 链路用 `a_guest_port: 8` / `b_guest_port: 8` 指到这个口
- 恢复 `Ethernet0/0/8` 的 `Physical/Protocol up` 后即可 ping

> 三种「换板」尝试（`device_type`、AR1220 的 10 行接口表、官方 `vrpcfg.zip`、
> `producttype.efs`）都实测失败，记录在
> [`evidence/agentd-protocol.md`](../evidence/agentd-protocol.md) §③f-1～③f-3。
> 不要重复这些实验。

---

## 3. 链路与端口映射

- 面板口编号（`a_port`/`b_port`）是拓扑里的编号
- **客机口编号**（`a_guest_port`/`b_guest_port`）是镜像内部 VPORT 的索引
- 真 eNSP 客户端让两者同号；镜像板子与型号不符时，必须用 guest 口号覆盖

relay 侧的对应关系：`面板口序 = 客机口序 = VRP 里第 N 个物理口`。

---

## 4. 故障排查

### 4.1 `VERR_INTNET_FLT_IF_NOT_FOUND`，设备起不来

```
Failed to open/create the internal network 'HostInterfaceNetworking-VirtualBox Host-Only Ethernet Adapter'
```

原因：VirtualBox 把这块 host-only 网卡**归类成了 bridged**（`VBoxManage modifyvm` 会警告
`Interface "..." is of type bridged`），host-only 就永远打不开。

两种处置：

```powershell
# A. 用 NAT（工具原生支持，端口转发由 lab 配置好）
enspctl lab create ... --nic nat

# B. 新建一块 host-only 网卡，把地址挪过去，再让虚拟机动它
VBoxManage hostonlyif create                                    # 得到 "...#2"
VBoxManage hostonlyif ipconfig "VirtualBox Host-Only Ethernet Adapter" --ip 192.168.99.1 --netmask 255.255.255.0
VBoxManage hostonlyif ipconfig "VirtualBox Host-Only Ethernet Adapter #2" --ip 192.168.56.1 --netmask 255.255.255.0
VBoxManage modifyvm <vm> --nic1 hostonly --hostonlyadapter1 "VirtualBox Host-Only Ethernet Adapter #2"
```

用完记得把 `#2` 删掉、原网卡 IP 改回 `192.168.56.1`。

### 4.2 日志反复出现 `the device disappeared - restarting it`

这曾是一个**真实 bug**：supervisor 每 10 秒向客机 `56789` 开一个新连接探活，而客机的
`agentd` **一生只 `accept()` 一次**，未接受的连接会一直占着 128 深的 accept 队列；
约两分钟后队列满，握手开始失败，于是一台完全健康的设备被电源循环掉（表现为
「活了约 190 秒就重启」）。

现在的做法：**用已持有的控制连接判活**，探活一律 RST 关闭（`SO_LINGER{1,0}`）。
如果又看到这条日志，检查是不是某个地方重新引入了「连一下 56789 就关」的探活。

### 4.3 控制台能连上，但命令不执行 / `did not echo it`

`192.168.56.101:40000` 能 TCP 连上但发命令被 reset，通常是**客机正在死**（VPORT 或
VRP 退了），不是 VRP 拒绝。先看 lab 日志里有没有重启记录。

### 4.4 接口全部 `down/down`

往镜像里写了与客机板子不匹配的接口表，会导致 `VPORT_Hal_PortEnable` 失败、所有口起不来。
**不要**在板子不匹配时写 `--board`；需要换板就按 §2 的结论用 guest 口覆盖。

### 4.5 `lab up` 会把 `lab patch` 的结果冲掉

曾经如此（`lab up` 挪地址时会从原始镜像重打补丁、且不带板子）。已修：补丁标记里记过板子，
后续补丁会继续写。若复现，检查 `lab.marker_board_state()` 是否被绕过。

### 4.6 设备起来了但 ping 不通

按顺序查：

1. `display ip interface brief` —— 目标口是否 `Physical up / Protocol up`、IP 是否正确
2. IP 是否配在**链路所在的那个客机口**（§2、§3）
3. relay 的长度统计：日志里 `udp relay: iface N: ... four-byte ... longer`，
   只有 4 字节说明该口还在控制态；出现 `longer` 才是真的在转发帧
4. `udp relay: ... N orphaned` 说明该口没有对端，帧被丢弃 —— 正常（没有链路的接口）

---

## 5. 不要做的事

- 不要修改 `C:\Program Files\Huawei\eNSP` 下的任何文件（分析只读）
- 不要改动 eNSP 自己的 `ini`（`manual` / `LISTENPORT` 等），以免影响 GUI 使用
- 不要在未经允许的情况下启停别人正在用的虚拟机
- `unregistervm --delete` 会删盘，只对自己创建的临时 lab 使用

---

## 6. 已验证的边界

| 项 | 状态 |
| --- | --- |
| 串口握手、控制通道、`DEV0/1/2` 引导 | ✅ 实测 |
| 接口进入数据态、真实帧转发（帧级证据：arp-request/reply、icmp） | ✅ 实测 |
| 两台 AR1220 互相 ping | ✅ 4/4，0% 丢包 |
| 设备长期存活 | ✅ 330 秒无重启 |
| 完全脱离 eNSP 客户端界面 | ✅ 全部命令行为准 |
| 让客机按型号选板（`GigabitEthernet0/0/0` 正常出现） | ❌ 未解决，用 guest 口覆盖绕过 |
