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
> GE0/0/0↔GE0/0/1。原因是 AR_Base 基础镜像每台设备只有两个可用的千兆口，
> 第三个 GE0/0/2 的链路起不来（两端都只发 4 字节握手、没有真实帧，
> 主机侧链路中继的统计里它是 `N four-byte, 0 longer`），
> IP 规划完全按实验图保持不变。

## 文件

| 文件 | 内容 |
|---|---|
| `ipsec-vpn.topo` | 拓扑（eNSP 可直接打开；连线为**实际跑通**的那一版） |
| `ipsec-vpn.topo.json` | 拓扑的生成描述 |
| `h1.cfg` / `h2.cfg` | 两台主机的地址与默认路由 |
| `site1-base.cfg` / `site2-base.cfg` | 两端站点：接口地址与路由 |
| `internet.cfg` | 公网路由器：两个直连网段，**不给私有网段配路由** |
| `manual-site1.cfg` / `manual-site2.cfg` | 手动模式的 IPsec 配置（实验步骤①） |
| `site1-ike.cfg` / `site2-ike.cfg` | IKE 自主协商模式的 IPsec 配置（实验步骤②） |
| `verify-ipsec.txt` | 验证命令 |

## 手工模式（实验步骤①）

AR2200 V200R003C00 的手工模式命令集是 `sa` 子命令族（**不是**别的版本的
`esp encryption-algorithm`，本机上那些命令全是 `Unrecognized command`）：

```
ipsec policy MAN1 10 manual
 security acl 3000
 proposal ESP1
 tunnel local 61.128.1.1
 tunnel remote 202.100.1.1
 sa spi outbound esp 256
 sa spi inbound esp 512
 sa encryption-hex outbound esp simple 00112233445566778899aabbccddeeff
 sa authentication-hex outbound esp simple 00112233445566778899aabbccddeeff00112233
 sa encryption-hex inbound esp simple 00112233445566778899aabbccddeeff
 sa authentication-hex inbound esp simple 00112233445566778899aabbccddeeff00112233
```

三个要点：

* **密钥长度**：AES-128 = 16 字节（32 个十六进制字符），SHA1 = 20 字节（40 个字符）。
* **SPI 两端交叉配对**：本端 outbound 的 SPI 等于对端 inbound 的 SPI（本端 256 / 对端 512）。
* **ACL 通配符必须是 `0.0.0.255`**：写成 `0.0.0.0` 时规则只匹配恰好等于网段地址的
  单个报文，隧道「配得出来却一条流量都进不去」——这是本实验最难查的一个坑。

## IKE 自主协商模式（实验步骤②）

两端的配置除地址外完全对称：

```
system-view
interface GigabitEthernet0/0/1
 undo ipsec policy                 # 策略在用时改对端会报 "IKE Peer in use."
quit
undo ipsec policy IPSEC1

acl number 3000
 rule 5 permit ip source 172.16.1.0 0.0.0.255 destination 10.1.1.0 0.0.0.255
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
  所以要「接口上解绑（接口视图下这条命令**不能带名字**）→ 系统视图删策略 →
  改对端 → 重建策略 → 重新绑定」。
* 静态路由 `ip route-static 10.1.1.0 255.255.255.0 202.100.1.1` 在本版本显示
  `Inactive`（下一跳不可直接解析、该版本不做递归路由解析），但**不影响转发**：
  报文按默认路由从公网口出去后照样命中 IPsec 策略。
* `display ipsec statistics` 本版本不支持（`Incomplete command found`），
  判断有没有流量过隧道要看 `display ipsec sa` 里的序号。

## 实测结果（2026-11-15）

两种方式都跑通了，跨隧道 ping 双向 4 发 4 收、TTL 为 254（隧道特征）：

```
H1 -> 10.1.1.254 : 4 packet(s) transmitted / 4 received / 0.00% loss / ttl=254
H2 -> 172.16.1.254: 4 packet(s) transmitted / 4 received / 0.00% loss / ttl=254
```

公网上只看到 ESP。主机侧链路中继对 internet 两个公网接口的统计：

```
[14:02:35] internet: udp relay: iface 0:   8 x ip50 61.128.1.1->202.100.1.1
[14:02:35] internet: udp relay: iface 0:   3 x icmp 61.128.1.1->202.100.1.1
[14:02:35] internet: udp relay: iface 1:   8 x ip50 202.100.1.1->61.128.1.1
```

`ip50` 就是 ESP（IP 协议号 50）：跨隧道的 ICMP 全部以 ESP 形式过公网，
明文 ICMP 只出现在公网地址之间，**没有一条报文出现 172.16.1.x 或 10.1.1.x**。

IKE 模式下隧道有流量之后再看 SA，收发序号都是 8：

```
[Outbound ESP SAs]  SPI: ...  Max sent sequence-number: 8
[Inbound  ESP SAs]  SPI: ...  Max received sequence-number: 8
```

## 验证矩阵

| 源 → 目的 | 期望 | 实测 |
|---|---|---|
| H1 → site1 内网口 172.16.1.254 | 通 | 通（ttl=255） |
| site1 → internet 61.128.1.10 | 通 | 通 |
| internet → site2 202.100.1.1 | 通 | 通 |
| H1 → site2 内网口 10.1.1.254 | 通（走隧道） | 通，4 发 4 收，ttl=254 |
| H2 → site1 内网口 172.16.1.254 | 通（走隧道） | 通，4 发 4 收，ttl=254 |
| internet 上看到的 | 只有 ESP，私有网段地址不可见 | 只有 ip50，没有私网地址 |

## 怎么跑

```powershell
enspctl lab create ipsec --topo examples/ipsec-vpn/ipsec-vpn.topo --root D:\work\ensp-guest\demo\lab-ipsec --with-vms
enspctl lab up --root D:\work\ensp-guest\demo\lab-ipsec
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec H1  --file examples/ipsec-vpn/h1.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec site1 --file examples/ipsec-vpn/site1-base.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec internet --file examples/ipsec-vpn/internet.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec site2 --file examples/ipsec-vpn/site2-base.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec H2  --file examples/ipsec-vpn/h2.cfg
# 步骤① 手动模式
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec site1 --file examples/ipsec-vpn/manual-site1.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec site2 --file examples/ipsec-vpn/manual-site2.cfg
# 步骤② IKE 自主协商模式（先在两端清掉手动模式的策略再下发）
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec site1 --file examples/ipsec-vpn/site1-ike.cfg
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec site2 --file examples/ipsec-vpn/site2-ike.cfg
# 步骤③ 检测效果
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec H1 "ping -c 4 10.1.1.254"
enspctl run --root D:\work\ensp-guest\demo\lab-ipsec site1 "display ipsec sa"
```
