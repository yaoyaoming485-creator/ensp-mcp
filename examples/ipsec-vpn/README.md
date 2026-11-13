# 实验（训）14 IPsec VPN 配置

《14.IPsecVPN配置.docx》给出的组网（实验图 14-1）：

```
PC1(.1) 172.16.1.1/24 ── site1(.254) 172.16.1.254/24
                             site1(.1) 61.128.1.1/24 ── internet(.10) 61.128.1.10/24
                                                         internet(.1) 202.100.1.10/24 ── site2(.1) 202.100.1.1/24
                                                                                            site2(.254) 10.1.1.254/24 ── PC2(.1) 10.1.1.1/24
```

要求：**内网网段 172.16.1.0 通过 internet 网络访问 10.1.1.0，在 internet 上看不到私有网段的信息**。
实验步骤：① 手动模式配置；② IKE 自主协商模式配置；③ 检测配置效果。

## 本实验在 lab 里的形态

| 角色 | 设备 | 接口与地址 |
|---|---|---|
| PC1 | H1（AR2220 代） | GE0/0/0 = 172.16.1.1/24，默认路由 → 172.16.1.254 |
| site1 | site1（AR2220） | GE0/0/0 = 172.16.1.254/24，GE0/0/1 = 61.128.1.1/24 |
| internet | internet（AR2220） | GE0/0/1 = 61.128.1.10/24，GE0/0/0 = 202.100.1.10/24，**不配私有网段路由** |
| site2 | site2（AR2220） | GE0/0/1 = 202.100.1.1/24，GE0/0/0 = 10.1.1.254/24 |
| PC2 | H2（AR2220 代） | GE0/0/0 = 10.1.1.1/24，默认路由 → 10.1.1.254 |

eNSP 的 PC/Server 需要独立的 PC 镜像（本机没装），用一台 AR2220 当主机：
只在链路口上配一个地址加一条默认路由，行为与实验图里的 PC 等价。

> **端口号与实验图不同**：图里 internet↔site2 走 GE0/0/2↔GE0/0/2，本 lab 用
> GE0/0/0↔GE0/0/1。原因是 AR2220 板的第三个 GE 口在这套 lab 里**链路起不来**
> （两端都只发 4 字节握手、没有真实帧，VRP 的口持续 up/down 抖动），
> IP 规划完全按实验图保持不变。详见 §实测结果。

## 文件

| 文件 | 内容 |
|---|---|
| `mux-vlan.topo.json` → `mux-vlan.topo` | 拓扑（`enspctl topo create` 生成，eNSP 可直接打开） |
| `h1.cfg` / `h2.cfg` | 两台主机的地址与默认路由 |
| `site1-base.cfg` / `site2-base.cfg` | 两端站点：接口地址与路由 |
| `internet.cfg` | 公网路由器：两个直连网段，**不给私有网段配路由** |
| `site1-ike.cfg` / `site2-ike.cfg` | IKE 自主协商模式的 IPsec 配置 |
| `verify-ipsec.txt` | 验证命令 |

## IKE 自主协商模式（实验步骤②）

两端的配置除地址外完全对称：

```
system-view
interface GigabitEthernet0/0/1
 undo ipsec policy                 # 策略在用时改对端会报 "IKE Peer in use."
quit
undo ipsec policy IPSEC1

acl number 3000
 rule 5 permit ip source 172.16.1.0 0.0.0.0 destination 10.1.1.0 0.0.0.0
quit

ipsec proposal ESP1
 esp encryption-algorithm aes-128
 esp authentication-algorithm sha1
quit

ike proposal 5
 encryption-algorithm aes-cbc-128
 dh group1
 authentication-algorithm sha1
 authentication-method pre-share
quit

ike peer SITE2 v1
 pre-shared-key cipher Huawei@123
 ike-proposal 5
 local-address 61.128.1.1         # 华为官方示例里对端都要写这两个地址
 remote-address 202.100.1.1
quit

ipsec policy IPSEC1 10 isakmp
 security acl 3000
 ike-peer SITE2
 proposal ESP1
quit

interface GigabitEthernet0/0/1
 ipsec policy IPSEC1
quit
```

几个版本差异（都是实测出来的）：

* `ike peer <name> v1` —— V200R008 之前是这个写法，之后改成 `ike peer <name>` + `version 1|2`。
* `pre-shared-key cipher` —— V2R3C00 之前是明文 `pre-shared-key <口令>`。
* `local-address` / `remote-address` —— 官方示例里两端都写；缺 `local-address` 时
  响应方匹配不到对端。
* **顺序**：`ipsec policy` 一旦引用了对端，再改对端就报 `IKE Peer in use.`，
  所以要「接口上解绑 → 删策略 → 改对端 → 重建策略 → 重新绑定」。
* 手工模式（实验步骤①）在这套 AR2200 V200R003 的 VRP 上**没有对应的命令**：
  `ipsec policy X 10 manual` 视图能进，`security acl` 也能配，但
  `esp encryption-algorithm` / `esp authentication-algorithm` / `esp integrity-key` /
  `esp security-key` 全部 `Unrecognized command`。

## 实测结果（2026-11-13）

**基线：三层全通。** 设备侧按实验图配好地址后：

```
site1 -> internet : Reply from 61.128.1.10: bytes=56 Sequence=1 ttl=255 time=20 ms
internet -> site2 : Reply from 202.100.1.1: bytes=56 Sequence=1 ttl=255 time=310 ms
H1 -> site1      : Reply from 172.16.1.254: bytes=56 Sequence=1..3 ttl=255
H1 -> H2（未建隧道）：Request time out          ← 隧道前的基线，符合预期
```

**IKE 协商：请求发得出去，响应方不吭声。** 主机侧抓包（relay 的 UDP 载荷就是
客机原始帧）逐跳确认：

```
192.168.56.102:10001 → 192.168.56.1:30017    inner 61.128.1.1->202.100.1.1   site1 发出
192.168.56.1:30017  → 192.168.56.103:10001   inner 61.128.1.1->202.100.1.1   到达 internet
192.168.56.103:10000 → 192.168.56.1:30032    inner 61.128.1.1->202.100.1.1   internet 转发
192.168.56.1:30032  → 192.168.56.104:10001   inner 61.128.1.1->202.100.1.1   到达 site2
```

也就是说 **IKE_SA_INIT 完整走完了 site1 → internet → site2**（报文内容已解出来：
exchange=2、SA 提议 = AES-128 / SHA1 / PSK / MODP-768，两端配置一致），
但 **site2 一个包都不回**，`display ike sa` 在 site2 上是空的，
`display ipsec sa` 两端都是 `No Security Associations established`。

已排除：路径与路由（三段 ping 全通 ✓）、两端配置对称（预共享密钥的密文在两端
完全相同 ✓）、提议一致 ✓、对端名字一致 ✓、带 `local-address` ✓、
用默认 IKE 提议（DES-CBC/SHA1/MODP-768）✓。

**剩下的可能**：AR2200 V200R003 的 IKE 响应方在这个模拟环境里解析不了
SA_INIT（报文里 ISAKMP 负载长度域的字节序可疑：头部长度是大端、
SA 负载长度读出来像小端），或者缺了某个该版本必须的参数。
下一步要么用 `debug ike` 抓响应方的日志（`debug ike all` + `terminal debugging`
在本机拿不到输出），要么换一台 AR2200 之外的支持 IPsec 的设备再比。

## 验证矩阵（隧道建立后应成立）

| 源 → 目的 | 期望 | 现状 |
|---|---|---|
| 部门1 → Server(10.1.1.254 侧) | 通 | 隧道未建 |
| 部门2 → Server | 通 | 隧道未建 |
| 部门1 ↔ 部门2 | 不通 | 由 ACL 3000 限制（隧道内不通） |
| internet 上看到的 | 只有 ESP，私有网段地址不可见 | 待隧道建立后在 internet 上抓包验证 |

## 怎么跑

```powershell
enspctl topo create --spec examples/ipsec-vpn/mux-vlan.topo.json -o examples/ipsec-vpn/mux-vlan.topo
enspctl lab create ipsec --topo examples/ipsec-vpn/mux-vlan.topo --root D:\work\ensp-guest\demo\lab-ipsec --with-vms
enspctl lab up --root D:\work\ensp-guest\demo\lab-ipsec
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec H1  --file examples/ipsec-vpn/h1.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec site1 --file examples/ipsec-vpn/site1-base.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec internet --file examples/ipsec-vpn/internet.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec site2 --file examples/ipsec-vpn/site2-base.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec H2  --file examples/ipsec-vpn/h2.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec site1 --file examples/ipsec-vpn/site1-ike.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec site2 --file examples/ipsec-vpn/site2-ike.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec H1 "ping -c 3 10.1.1.1"
```
