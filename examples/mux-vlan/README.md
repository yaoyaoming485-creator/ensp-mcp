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

## 拓扑与地址规划

* 交换机 SW1（S5700，24 个 GE 口）承载 MUX-VLAN，7 个 access 口分别接 6 台 PC 和 1 台 Server。
* 全网同一个网段 `192.168.100.0/24`（这正是 MUX-VLAN 的典型用法：交换机做二层隔离，
  相当于华为版的 PVLAN）：
  * 部门1（VLAN10）：`192.168.100.11`、`.12`
  * 部门2（VLAN20）：`192.168.100.21`、`.22`
  * 部门3（VLAN30）：`192.168.100.31`、`.32`
  * Server（VLAN100）：`192.168.100.100`
  * 网关：SW1 的 `Vlanif100` = `192.168.100.1`（只有主 VLAN 能配 VLANIF）

拓扑文件：`mux-vlan.topo`（由 `mux-vlan.topo.json` 生成，eNSP 可直接加载）。

## 下发与验证

```bash
# 1) 起 lab（拓扑里有 S5700，需要交换机镜像）
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
100         120        separate   GigabitEthernet1/0/1
...
100         130        group      GigabitEthernet1/0/2
```

## 验证矩阵（各主机发起）

| 源 → 目的 | 期望 | 原因 |
|---|---|---|
| 部门1 内部 PC1 ↔ PC2（同 VLAN10） | **不通** | 隔离型从 VLAN：同 VLAN 内也隔离 |
| 部门2 内部 PC3 ↔ PC4（同 VLAN20） | 通 | 互通型从 VLAN：同 VLAN 内互通 |
| 部门3 内部 PC5 ↔ PC6（同 VLAN30） | 通 | 互通型从 VLAN |
| 部门1 → Server | 通 | 主 VLAN ↔ 从 VLAN 互通 |
| 部门2 → Server | 通 | 同上 |
| 部门3 → Server | 通 | 同上 |
| 部门1 ↔ 部门2 / 部门3 | 不通 | 不同从 VLAN 之间不互通 |
| 部门2 ↔ 部门3 | 不通 | 不同从 VLAN 之间不互通 |

普通 VLAN 划分做不到「不同 VLAN 还能互通」和「同 VLAN 还能隔离」，
这两条正是 MUX-VLAN 的价值所在。

## 当前本机的限制（实测结论）

本机 eNSP 安装里**只有 AR 系列的设备镜像**（`vboxserver/AR_Base/AR_Base.vdi`），
克隆出来的设备身份是 **AR201（VRP AR200 V200R003C00）**，该版本**没有 MUX-VLAN 命令集**：

```
[Huawei-vlan100] mux-vlan
                ^
Error: Unrecognized command found at '^' position.

[Huawei-Ethernet0/0/1] port mux-vlan enable vlan 10
                                    ^
Error: Unrecognized command found at '^' position.
```

（`/dev/upload/cmdtree.bin` 里确实有 MUXVLAN 模块、`infoeng.res` 里也有整套 MUX-VLAN
报错文案，但那是全产品共用的资源文件，不代表 AR201 开启了��特性。）

交换机镜像 `devices/LSW/s5700/eNSP_Switch.exe` 在安装目录里，但没有 `LSW_Base.vdi`：
eNSP 是用宿主侧的文件传输通道（`CFileTransmit::PrePut/PutData`）把交换机 VRP 载荷
注入 guest 磁盘的，guest 里的载荷 `dev_base_0/1/2` 是带自定义头的打包格式
（三段共享 16 字节头，段号不同则头尾 4 字节不同）。要拿到交换机设备，需要：

1. 用真实 eNSP 客户端创建一台 S5700（客户端会自己完成载荷注入），再把它克隆进 lab；或
2. 逆向 `CFileTransmit` 通道 + `dev_base_*` 打包格式，自己造交换机 guest。

## 出处

* 《MUX VLAN配置命令》，S9300/S9300X V200R019C10 命令参考 —
  <https://support.huawei.cn/enterprise/zh/doc/EDOC1100127089/e7fde8f5>
* 《CloudEngine 9800, 8800, 6800, 5800 V200R025C00 命令参考》 —
  <https://support.huawei.com/enterprise/zh/doc/EDOC1100519016/834147df>
* 《CX110&CX31x系列 交换模块 V100R001C00 命令参考 16》 —
  <https://support.huawei.com/enterprise/zh/doc/DOC1000041691/9faa42ea>
