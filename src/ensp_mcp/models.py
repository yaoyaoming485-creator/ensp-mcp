"""Device model catalog for eNSP 1.3.00.100 ``.topo`` files.

Everything here was reverse-engineered from an installed eNSP
(``C:\\Program Files\\Huawei\\eNSP``) and cross-checked against topologies that
eNSP itself opens.  Two facts matter for authoring:

* the ``<slot>`` / ``<interface>`` child XML inside ``<dev>`` is what eNSP
  actually parses -- get it wrong and the device silently loses its ports;
* the *order* of the flattened interface list is the index space used by
  ``<interfacePair srcIndex=... tarIndex=...>`` in the ``<lines>`` section.

``ports`` below is that flattened order, so ``ports[i]`` is the label for
interface index ``i``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Group:
    """A counted interface group, rendered as one ``<interface>`` element.

    ``prefix`` is both the eNSP ``interfacename`` and the key of a **running**
    counter.  eNSP numbers ports continuously across the ``<interface>``
    elements that declare them, so AR2220's "GE x1" followed by "GE x2" is
    GE0/0/0, GE0/0/1, GE0/0/2 -- not GE0/0/0 twice.
    """

    sztype: str  # Ethernet | Serial
    prefix: str  # GE | Ethernet | Serial  (== interfacename)
    count: int
    start: int = 0


@dataclass(frozen=True)
class Model:
    name: str
    category: str
    ports: tuple[str, ...]
    xml: str
    settings_template: str = ""
    has_com_port: bool = True
    note: str = ""

    @property
    def port_count(self) -> int:
        return len(self.ports)


def _counted(groups: Iterable[Group], sub: tuple[str, str] | None = None) -> tuple[str, tuple[str, ...]]:
    """Render ``<slot number="slot17" isMainBoard="1">`` from counted groups."""
    body: list[str] = []
    ports: list[str] = []
    counters: dict[str, int] = {}
    for g in groups:
        body.append(
            f'                <interface sztype="{g.sztype}" interfacename="{g.prefix}" count="{g.count}" />'
        )
        n = counters.get(g.prefix, g.start)
        for _ in range(g.count):
            ports.append(f"{g.prefix}0/0/{n}")
            n += 1
        counters[g.prefix] = n
    xml = ['            <slot number="slot17" isMainBoard="1">']
    xml.extend(body)
    xml.append("            </slot>")
    if sub is not None:
        sub_id, sub_type = sub
        xml.append(f'            <slot isMainBoard="0" id="{sub_id}" type="{sub_type}" />')
    return "\n".join(xml), tuple(ports)


def _enumerated(entries: Iterable[tuple[str, str, int, int, int]], slot_id: str = "1") -> tuple[str, tuple[str, ...]]:
    """Render the slot-format devices (NE / CE / CX / USG6000V)."""
    xml = [f'            <slot id="{slot_id}">']
    ports: list[str] = []
    for category, itype, slot_index, card_index, interface_index in entries:
        xml.append(
            f'                <interface category="{category}" type="{itype}" '
            f'slotIndex="{slot_index}" cardIndex="{card_index}" interfaceIndex="{interface_index}" />'
        )
        ports.append(f"{itype}{slot_index}/{card_index}/{interface_index}")
    xml.append("            </slot>")
    return "\n".join(xml), tuple(ports)


def _repeat(prefix: str, count: int, start: int = 0) -> Group:
    return Group("Ethernet", prefix, count, start)


def _serial(count: int) -> Group:
    return Group("Serial", "Serial", count)


# --------------------------------------------------------------------------
# Routers
# --------------------------------------------------------------------------

def _ar201() -> Model:
    xml, ports = _counted([_repeat("Ethernet", 8), _repeat("Ethernet", 1)])
    return Model("AR201", "router", ports, xml)


def _ar1220() -> Model:
    xml, ports = _counted([_repeat("GE", 2), _repeat("Ethernet", 8)], sub=("1", "521"))
    return Model("AR1220", "router", ports, xml)


def _ar2220() -> Model:
    xml, ports = _counted(
        [_repeat("GE", 1), _repeat("GE", 2), _serial(2)],
        sub=("3", "521"),
    )
    return Model("AR2220", "router", ports, xml)


def _ar2240() -> Model:
    xml, ports = _counted([_repeat("GE", 1), _repeat("GE", 2)])
    return Model("AR2240", "router", ports, xml)


def _ar3260() -> Model:
    xml, ports = _counted([_repeat("GE", 1), _repeat("GE", 2)])
    return Model("AR3260", "router", ports, xml)


def _router() -> Model:
    xml, ports = _counted(
        [
            _repeat("Ethernet", 2),
            _repeat("GE", 4),
            _serial(4),
        ]
    )
    return Model("Router", "router", ports, xml)


def _slot_router(name: str, count: int = 10) -> Model:
    xml, ports = _enumerated([("Ethernet", "Ethernet", 1, 0, i) for i in range(count)])
    return Model(name, "router", ports, xml, note="needs an external device image")


# --------------------------------------------------------------------------
# Switches
# --------------------------------------------------------------------------

def _s3700() -> Model:
    # eNSP labels the S3700's 22 FE ports Ethernet0/0/1..22 and its 2 uplinks
    # GE0/0/1..2, i.e. both groups are 1-based.
    xml, ports = _counted([_repeat("Ethernet", 22, start=1), _repeat("GE", 2, start=1)])
    return Model("S3700", "switch", ports, xml)


def _s5700() -> Model:
    xml, ports = _counted([_repeat("GE", 24)])
    return Model("S5700", "switch", ports, xml)


def _ce(name: str, count: int) -> Model:
    xml, ports = _enumerated([("Ethernet", "GE", 1, 0, i) for i in range(count)])
    return Model(name, "switch", ports, xml, note="needs an external device image")


def _cx() -> Model:
    xml, ports = _enumerated([("Ethernet", "Ethernet", 1, 0, i) for i in range(10)])
    return Model("CX", "switch", ports, xml, note="needs an external device image")


# --------------------------------------------------------------------------
# Firewalls
# --------------------------------------------------------------------------

def _usg5500() -> Model:
    xml, ports = _counted([_repeat("GE", 9)])
    return Model("USG5500", "firewall", ports, xml)


def _usg6000v() -> Model:
    entries = [("Ethernet", "GE", 0, 0, 0)] + [("Ethernet", "GE", 1, 0, i) for i in range(7)]
    xml, ports = _enumerated(entries)
    return Model("USG6000V", "firewall", ports, xml, note="needs vfw_usg.vdi")


# --------------------------------------------------------------------------
# Wireless
# --------------------------------------------------------------------------

def _wlan(name: str, category: str, count: int) -> Model:
    xml, ports = _counted([_repeat("GE", count)])
    return Model(name, category, ports, xml)


def _ap(name: str, count: int) -> Model:
    xml, ports = _counted([_repeat("GE", count)])
    return Model(name, "wireless", ports, xml, settings_template=" -apMac {mac} -apSN {sn}")


def _ad9430() -> Model:
    xml, ports = _counted([_repeat("GE", 28)])
    return Model("AD9430", "wireless", ports, xml)


def _sta(name: str) -> Model:
    xml, _ = _counted([])
    return Model(
        name,
        "wireless",
        (),
        xml,
        settings_template=" -simsta_ip 0.0.0.0 -simsta_mask 0.0.0.0 -simsta_gateway 0.0.0.0",
    )


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

def _pc_like(name: str, category: str, count: int, settings: str) -> Model:
    xml, ports = _counted([_repeat("Ethernet", count)])
    return Model(name, category, ports, xml, settings_template=settings, has_com_port=False)


_CATALOG: dict[str, Model] = {}


def _register(m: Model) -> None:
    _CATALOG[m.name] = m


for _m in [
    _ar201(),
    _ar1220(),
    _ar2220(),
    _ar2240(),
    _ar3260(),
    _router(),
    _slot_router("NE40E"),
    _slot_router("NE5000E"),
    _slot_router("NE9000"),
    _s3700(),
    _s5700(),
    _ce("CE6800", 20),
    _ce("CE12800", 10),
    _cx(),
    _usg5500(),
    _usg6000v(),
    _wlan("AC6005", "wireless", 8),
    _wlan("AC6605", "wireless", 24),
    _ap("AP2050", 5),
    _ap("AP3030", 1),
    _ap("AP4030", 2),
    _ap("AP4050", 2),
    _ap("AP5030", 2),
    _ap("AP6050", 2),
    _ap("AP7030", 2),
    _ap("AP7050", 2),
    _ap("AP8030", 3),
    _ap("AP8130", 3),
    _ap("AP9131", 2),
    _ad9430(),
    _sta("STA"),
    _sta("Cellphone"),
    _pc_like("PC", "endpoint", 1, " -simpc_ip {ip} -simpc_mask {mask} -simpc_gateway {gw} -simpc_mac {mac}"),
    _pc_like("Laptop", "endpoint", 1, " -simpc_ip {ip} -simpc_mask {mask} -simpc_gateway {gw} -simpc_mac {mac}"),
    _pc_like("Server", "endpoint", 1, "-domain 0 -eth {mac} -ipaddr {ip} -ipmask {mask} -gateway {gw}"),
    _pc_like("Client", "endpoint", 1, "-domain 0 -eth {mac} -ipaddr {ip} -ipmask {mask} -gateway {gw}"),
    _pc_like(
        "MCS",
        "endpoint",
        1,
        " -simpc_ip {ip} -simpc_mask {mask} -simpc_gateway {gw} -simpc_mac {mac} -simpc_mc_dstip 225.1.1.1",
    ),
    _pc_like("R250D", "endpoint", 1, " -apMac {mac} -apSN {sn}"),
    _pc_like("FRSW", "endpoint", 16, ""),
    _pc_like("HUB", "endpoint", 16, ""),
]:
    _register(_m)

# Cloud carries interfaceMap entries; it is special-cased here rather than
# forced through the counted-group builder.
_CLOUD_XML = """            <slot number="slot17" isMainBoard="1">
                <interface sztype="Ethernet" interfacename="Ethernet" count="3" />
                <interface sztype="Ethernet" interfacename="GE" count="0" />
                <interface sztype="Serial" interfacename="Serial" count="0" />
                <interfaceMap sztype="Ethernet" interfacename="Ethernet" displayNo="1" remoteDisplayNo="2" adapterUid="" isOpen="0" udpPort="0" peerIPAdd="0.0.0.0" peerIP="0" peerPort="0" />
                <interfaceMap sztype="Ethernet" interfacename="Ethernet" displayNo="2" remoteDisplayNo="1" adapterUid="" isOpen="0" udpPort="0" peerIPAdd="0.0.0.0" peerIP="0" peerPort="0" />
            </slot>"""
_register(
    Model(
        "Cloud",
        "endpoint",
        ("Ethernet0/0/0", "Ethernet0/0/1", "Ethernet0/0/2"),
        _CLOUD_XML,
        has_com_port=False,
    )
)


# Devices whose images do NOT ship with the eNSP installer.  A topology that
# uses them is valid, but the device cannot start until the image is imported.
NEEDS_IMAGE = frozenset({"NE40E", "NE5000E", "NE9000", "CE6800", "CE12800", "CX", "USG6000V"})

CATALOG: dict[str, Model] = dict(sorted(_CATALOG.items()))


def get(model: str) -> Model:
    try:
        return CATALOG[model]
    except KeyError:
        known = ", ".join(sorted(CATALOG))
        raise KeyError(f"unknown eNSP model {model!r}; known models: {known}") from None


def describe_all() -> list[dict]:
    out = []
    for m in CATALOG.values():
        out.append(
            {
                "model": m.name,
                "category": m.category,
                "ports": list(m.ports),
                "port_count": m.port_count,
                "takes_com_port": m.has_com_port,
                "needs_device_image": m.name in NEEDS_IMAGE,
                "note": m.note,
            }
        )
    return out
