"""The guest's board description, per model.

The guest's VRP reads its board from ``/dev/upload/resource.cfg``: the
``device_type = <n>`` line picks the board, and the ``interface[i] = ...``
lines describe the ports.  eNSP's own client writes that file from the model
(``eNSP_Client.exe`` pushes the template at ``0x4b53df``), so a guest whose
file was not rewritten runs the board the image shipped with - the AR201's
nine ports, with the eight FE ports as switch ports and only the ninth
routable.

The numbers below are the ones that could be read off a real file:

* ``AR201`` is what the image ships with: eight FE ports
  (``encap_flag 0 / engine_type 3 / port_type 0 / mod_id 0``) and one
  uplink (``encap_flag 5 / engine_type 1 / port_type 0 / mod_id 1``), with
  ``device_type = 33``.
* the AR1220's main board is ``GE x2 + Ethernet x8`` - ten ports, the two
  GE ports being routed and the eight FE ports switched (eNSP's own
  example topologies say so).

``device_type`` for the other models is still unknown: the number lives in
the guest's own board database (``dev2``), not in the host.  Until it is
known the value is left at the image's 33, and the experiment is whether
the interface list alone is enough to make the guest show the right board.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PortGroup:
    """One group of identical ports on the board."""

    #: the name the VRP gives the ports ("GE" stays "GE", everything else
    #: becomes "Ethernet" - that is the eNSP client's rule, at 0x415090)
    name: str
    #: how many ports the group has
    count: int
    encap_flag: int
    engine_type: int
    port_type: int = 0
    mod_id: int = 0


@dataclass(frozen=True)
class Board:
    """A model's board: the device type and its port groups."""

    device_type: int
    groups: tuple[PortGroup, ...] = field(default_factory=tuple)

    @property
    def port_count(self) -> int:
        return sum(g.count for g in self.groups)


#: the boards we know how to write.  ``device_type`` is the number the guest
#: uses to pick the board; only the AR201's is confirmed (33 - the value in
#: the image's own resource.cfg).
BOARDS: dict[str, Board] = {
    "AR201": Board(
        device_type=33,
        groups=(
            PortGroup("Ethernet", 8, encap_flag=0, engine_type=3),
            PortGroup("Ethernet", 1, encap_flag=5, engine_type=1, mod_id=1),
        ),
    ),
    "AR1220": Board(
        device_type=33,   # not confirmed: the AR1220's own number is still unknown
        groups=(
            PortGroup("GE", 2, encap_flag=5, engine_type=1, mod_id=1),
            PortGroup("Ethernet", 8, encap_flag=0, engine_type=3),
        ),
    ),
}


def board_for(model: str) -> Board | None:
    """The board of ``model``, or ``None`` when it is not known."""
    return BOARDS.get(model.strip().upper())


def interface_lines(board: Board, guest_ip: str, host_ip: str,
                    host_ports: dict[int, int]) -> list[str]:
    """The ``interface[i] = ...`` lines of a board, in panel order.

    The numbering is the one eNSP uses: the interface's own index, its
    ``cpu_port`` and its position in the panel are all the same number, and
    ``port_id`` counts inside the group.  That is what the eNSP client
    writes (``eNSP_Client.exe``: ``cpu_port`` is the running port index,
    ``port_id`` restarts at every group) and what the official AR host
    agent writes (``VAR_Plugin.dll``: ``interface[%d]`` and ``port_id:%d``
    are both the array index).
    """
    lines: list[str] = []
    index = 0
    for group in board.groups:
        for member in range(group.count):
            local_port = 10000 + index
            dest_port = host_ports.get(index, 0)
            lines.append(
                f"interface[{index}] = socket[local:{guest_ip}:{local_port} "
                f"dest:{host_ip}:{dest_port} role:client protocol:udp]"
                f"[port_phy card_id:0 panel_port:0 port_name:{group.name} "
                f"cpu_port:{index} encap_flag:{group.encap_flag} "
                f"engine_type:{group.engine_type} port_type:{group.port_type} "
                f"mod_id:{group.mod_id} port_id:{member} phy_id:1]")
            index += 1
    return lines
