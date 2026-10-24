# 实验（训）9：MUX-VLAN 的配置

《9.MUX-VLAN的配置.docx》给出的需求：

> 公司内部三个部门，部门1 在 VLAN10 里，部门2 在 VLAN20 里，部门3 在 VLAN30 里，
> 服务器在 VLAN100 里。三个部门之间不可以互通，但是都可以跟服务器 Server 互通。
> 部门1 内部也隔离，部门2、部门3 内部可以互通。

## 需求 → MUX-VLAN 映射

| 需求 | VLAN | MUX-VLAN 角色 | VRP 命令 |
|---|---|---|---|
| 服务器 Server，可被所有部门访问 | VLAN100 | 主 VLAN（principal） | `vlan 100` → `mux-vlan` |
| 部门1：内部成员之间**不**互通 | VLAN10 | 隔离型从 VLAN（separate） | `vlan 100` → `subordinate separate 10` |
| 部门2：内部成员之间互通 | VLAN20 | 互通型从 VLAN（group） | `vlan 100` → `subordinate group 20` |
| 部门3：内部成员之间互通 | VLAN30 | 互通型从 VLAN（group） | `vlan 100` → `subordinate group 30` |

华为官方命令参考给出的语义（出处见文末）：

* 主 VLAN：加入主 VLAN 的接口可以和 MUX VLAN 内所有接口通信。
* 隔离型从 VLAN：同一 VLAN 内接口之间不能互相通信，可以与主 VLAN 接口通信，
  不同从 VLAN 之间不能互相通信。
* 互通型从 VLAN：同一 VLAN 内接口之间可以互相通信，可以与主 VLAN 接口通信，
  不同从 VLAN 之间不能互相通信。
* **一个主 VLAN 下只能配置一个隔离型从 VLAN**；互通型从 VLAN 可以多个。
* 只有主 VLAN 能创建 VLANIF 接口；从 VLAN 不能创建 VLANIF。

因此「三个部门之间不可以互通，但都可以跟服务器互通」正好由
「主 VLAN ↔ 全部从 VLAN 互通 + 从 VLAN 之间一律不互通」实现。

## 两个现成的实验拓扑

| 目录 | 设备 | 说明 |
|---|---|---|
| `mux-vlan.topo`（本目录） | 1×S5700 + 6×PC + 1×Server | 单交换机版本，7 个 access 口都在 SW1 上，最容易上手 |
| `lab9/` | 2×S5700 + 6×PC + 1×Server | **与《9.MUX-VLAN的配置.docx》图 9-1 一致**：LSW1 挂 4 个部门终端 + 上联，LSW2 挂 2 个部门终端 + 服务器 + 上联 |

拓扑文件由同名 `.json` 生成（`enspctl topo create --spec ... -o ...`），eNSP 可直接加载。
终端地址写在 spec 的 `ip` / `mask` / `gw` 字段里，客户端会在设备启动时下发——
**不要在设备还没起来的时候去终端的「基础配置」页里改地址，那样会丢配置**
（表现为交换机上 MAC/ARP 表全空）。

### 地址规划（lab9/，与图 9-1 的 192.168.1.0/24 一致）

| 设备 | VLAN | 地址 | 归属 |
|---|---|---|---|
| PC1 | 10 | 192.168.1.11 | 部门1（隔离型从 VLAN，与 PC3 互相隔离） |
| PC3 | 10 | 192.168.1.13 | 部门1 |
| PC2 | 20 | 192.168.1.12 | 部门2（互通型从 VLAN，与 PC5 互通） |
| PC5 | 20 | 192.168.1.15 | 部门2 |
| PC4 | 30 | 192.168.1.14 | 部门3（互通型从 VLAN，与 PC6 互通） |
| PC6 | 30 | 192.168.1.16 | 部门3 |
| Server1 | 100 | 192.168.1.2 | 服务器（主 VLAN） |
| 网关 | 100 | 192.168.1.1 | 主 VLAN 的 VLANIF（本平台不支持，见下） |

## 下发与验证

```bash
# 1) 起 lab（拓扑里有 S5700，eNSP 自带交换机，不需要导入设备包）
enspctl lab up --root <lab 目录>

# 2) 下发交换机配置
enspctl run SW1 --file examples/mux-vlan/mux-vlan-sw1.cfg --root <lab 目录>

# 3) 验证
enspctl run SW1 --file examples/mux-vlan/verify-mux-vlan.txt --root <lab 目录>
```

`display mux-vlan` 的期望输出（主/从 VLAN、类型、成员接口）：

```
Principal Subordinate Type     Interface
100         -          principal
100         10         separate   GigabitEthernet0/0/1 GigabitEthernet0/0/2
100         20         group      GigabitEthernet0/0/3
100         30         group      GigabitEthernet0/0/4
```

## 验证矩阵（各主机发起）

| 源 → 目的 | 期望 | 原因 |
|---|---|---|
| 部门1 内部 PC1 ↔ PC3（同 VLAN10） | **不通** | 隔离型从 VLAN：同 VLAN 内也隔离 |
| 部门2 内部 PC2 ↔ PC5（同 VLAN20） | 通 | 互通型从 VLAN：同 VLAN 内互通 |
| 部门3 内部 PC4 ↔ PC6（同 VLAN30） | 通 | 互通型从 VLAN |
| 部门1 → Server | 通 | 主 VLAN ↔ 从 VLAN 互通（**本平台不通**，见下） |
| 部门2 → Server | 通 | 同上（**本平台不通**） |
| 部门3 → Server | 通 | 同上（**本平台不通**） |
| 部门1 ↔ 部门2 / 部门3 | 不通 | 不同从 VLAN 之间不互通 |
| 部门2 ↔ 部门3 | 不通 | 不同从 VLAN 之间不互通 |

普通 VLAN 划分做不到「不同 VLAN 还能互通」和「同 VLAN 还能隔离」，
这两条正是 MUX-VLAN 的价值所在。

## 本平台的实机验证结论（2026-10-24，eNSP 1.3.00.100）

### 1. eNSP 自带的 S5700 是可用的

`devices/LSW/s5700/eNSP_Switch.exe` 在安装目录里，但它是 **Windows PE**，
之前因此推断「客机需要交换机的 Linux 载荷、必须导入设备包」。**这个推断是错的**：
eNSP 的交换机根本不是 guest appliance，它以**宿主进程** `eNSP_Switch.exe` 运行
（设备启动后能看到该进程，监听 127.0.0.1 的 10xxx/103xx 端口），
**不需要任何设备包，装完就能用**。而且它自带完整的 MUX-VLAN 命令集：

```
[Huawei-vlan100] mux-vlan
[Huawei-vlan100] subordinate separate 10
```

`display mux-vlan` / `display vlan` 的实机回显见
[`eNSP-实机记录.txt`](eNSP-实机记录.txt)。

### 2. 三条语法规则（写错就配不出来）

| 规则 | 写错时的报错 |
|---|---|
| `subordinate` 必须在**主 VLAN 的视图**下执行，参数填**从 VLAN 的 ID** | `Error: Must configure principal vlan firstly.`（真意是"当前视图不是主 VLAN"，极具误导性） |
| 从 VLAN 必须**先创建** | `Error: Vlan is not exist.` |
| 主 VLAN 的 `mux-vlan` 要**单独提交一次**（`vlan 100` → `mux-vlan` → `quit`，再重新进 `vlan 100`） | `Error: Please enable MUX-VLAN in vlan command mode.` |

另外，本版本的 `port mux-vlan enable` **不带 `vlan` 参数**（写
`port mux-vlan enable vlan 10` 会被拒）。

### 3. 本平台的硬限制

* **主 VLAN 不能配三层接口**，因此没有 MUX-VLAN 的共享网关，
  「各部门 ↔ 服务器」的三层互通在本平台无法完成：
  ```
  [Huawei] interface Vlanif100
  Error: The VLAN is already a MUX VLAN.          （先建三层接口再配 mux-vlan 也不行：
  Error: The VLAN has a L3 interface, Please delete it first.）
  ```
  在支持共享网关的新版 VRP（S5700 V200R011 及以后 / S9300 等）上，
  在主 VLAN 上建 VLANIF 即可打通这一条。
* **互联口（trunk）进不了 MUX 组**：`port mux-vlan enable` 在 trunk 上被拒
  （`Error: Only one VLAN can be configured on the port.`），
  实测互联口下来的帧也不会递送进 MUX 成员域，跨交换机的 MUX 语义
  只能靠组内成员口互访来验证。
* **eNSP 客户端或设备重启后，交换机的运行配置会被清空**
  （`save` 到 vrpcfg.zip 的部分也没能恢复 MUX-VLAN 组），复测前要重新下发。

### 4. 端口编号从 1 起

eNSP 的 S5700 是 `GigabitEthernet0/0/1 .. 0/0/24`，终端是 `Ethernet0/0/1`
（与原图 9-1 的标注一致）。本仓库的 `models.py` 之前按 0 起算，
导致 `a_port`/`a_if` 整体偏移一位（画布上 a_port=0 会落在 GE 0/0/1 之外的
下一个口），已在 `src/ensp_mcp/models.py` 修正。

## 出处

* 《MUX VLAN配置命令》，S9300/S9300X V200R019C10 命令参考 —
  <https://support.huawei.cn/enterprise/zh/doc/EDOC1100127089/e7fde8f5>
* 《CloudEngine 9800, 8800, 6800, 5800 V200R025C00 命令参考》 —
  <https://support.huawei.com/enterprise/zh/doc/EDOC1100519016/834147df>
* 《CX110&CX31x系列 交换模块 V100R001C00 命令参考 16》 —
  <https://support.huawei.com/enterprise/zh/doc/DOC1000041691/9faa42ea>
