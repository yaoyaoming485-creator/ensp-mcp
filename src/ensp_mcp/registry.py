"""The one place that says what the tools are and what they need.

Every tool is described once, here.  ``server.py`` copies the description
into the tool's MCP metadata, ``ensp_tool_registry`` hands the same data to
an agent that wants to know what it may call, and the documentation lists
the same names.  One description, three consumers -- so a tool cannot be
renamed in one place and forgotten in another.

The ``requires`` list is **machine readable**: every entry names a probe, a
predicate and the error code that is raised when the predicate is false.  An
agent can therefore check a precondition itself, and a failing call says
exactly which precondition was not met and what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Requirement:
    """One precondition of a tool."""

    #: stable identifier, used by the error message and by an agent
    id: str
    #: what has to be true
    description: str
    #: how the server checks it
    probe: str
    #: the predicate the probe result has to satisfy
    predicate: str
    #: the error code raised when it does not hold
    error_code: str
    #: what the caller can do about it
    remedy: str
    #: the arguments the requirement applies to (empty: the whole call)
    applies_to: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "probe": self.probe,
            "predicate": self.predicate,
            "error_code": self.error_code,
            "remedy": self.remedy,
            "applies_to": list(self.applies_to),
        }


@dataclass(frozen=True)
class ToolSpec:
    """One tool, as the registry describes it."""

    name: str
    category: str
    summary: str
    returns: str
    requires: tuple[Requirement, ...] = ()
    error_codes: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "summary": self.summary,
            "returns": self.returns,
            "requires": [r.as_dict() for r in self.requires],
            "error_codes": list(self.error_codes),
            "notes": list(self.notes),
        }

    def meta(self) -> dict:
        """The metadata the MCP tool is registered with."""
        return {
            "category": self.category,
            "summary": self.summary,
            "returns": self.returns,
            "requires": [r.as_dict() for r in self.requires],
            "error_codes": list(self.error_codes),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# the requirements
# --------------------------------------------------------------------------

REQUIRES_VBOX = Requirement(
    id="vbox_installed",
    description="VirtualBox is installed and VBoxManage can be found.",
    probe="vbox.vboxmanage_path()",
    predicate="does not raise VBoxError",
    error_code="vbox_not_found",
    remedy="Install Oracle VirtualBox, or point VBOXMANAGE at VBoxManage.exe.",
)

REQUIRES_ENSP = Requirement(
    id="ensp_installed",
    description="eNSP is installed.",
    probe="vbox.find_ensp_client()",
    predicate="is not None",
    error_code="ensp_not_found",
    remedy="Install Huawei eNSP, or set the path in the registry key.",
)

REQUIRES_CONSOLE_FREE = Requirement(
    id="ensp_client_not_holding_console",
    description="The eNSP client is not holding this device's console session.",
    probe="occupancy.occupancy(guest_ip)",
    predicate="not occupied.held_by_ensp_client",
    error_code="console_held_by_ensp_client",
    remedy=("Quit the eNSP client (closing the console tab is not enough - the "
            "socket stays open until eNSP exits), or send the command through "
            "the eNSP console tab."),
    applies_to=("guest_ip",),
)

REQUIRES_DEVICE_UP = Requirement(
    id="device_reachable",
    description="The device answers on its console port.",
    probe="agentd.probe_endpoint(guest_ip)",
    predicate="is True",
    error_code="device_unreachable",
    remedy=("Start the device (ensp_start_devices) and wait for the guest to "
            "come up; a booted eNSP device needs a minute."),
    applies_to=("guest_ip",),
)

REQUIRES_ELEVATED = Requirement(
    id="process_is_elevated",
    description="This process holds an administrator token.",
    probe="capture.is_elevated()",
    predicate="is True",
    error_code="capture_requires_elevation",
    remedy=("Retry with elevate=true: the capture is then taken by a helper "
            "that is started through the shell's runas verb."),
)

REQUIRES_NOT_PROTECTED = Requirement(
    id="vm_not_protected",
    description="The machine is not on the protected list.",
    probe="vbox.list_vms()",
    predicate="name not in protected",
    error_code="vm_protected",
    remedy=("Pass override_protected=true if this machine really is yours to "
            "stop; the default protects the user's own virtual machines."),
    applies_to=("devices",),
)


# --------------------------------------------------------------------------
# the tools
# --------------------------------------------------------------------------

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="ensp_list_models",
        category="topology",
        summary="The device models this server can generate.",
        returns="JSON with each model's category, interface index order and "
                "whether it needs an image the installer does not ship.",
        requires=(),
        error_codes=(),
    ),
    ToolSpec(
        name="ensp_create_topology",
        category="topology",
        summary="Write a .topo file from a spec.",
        returns="JSON with the path, size, encoding and the validation report.",
        error_codes=("topo_invalid", "topo_write_failed"),
    ),
    ToolSpec(
        name="ensp_read_topology",
        category="topology",
        summary="Parse a .topo file into JSON.",
        returns="JSON with the devices, links, labels and boxes.",
        error_codes=("topo_not_found", "topo_unreadable"),
    ),
    ToolSpec(
        name="ensp_validate_topology",
        category="topology",
        summary="Check a topology spec without writing anything.",
        returns="JSON with the problems and the warnings.",
    ),
    ToolSpec(
        name="ensp_open_topology",
        category="runtime",
        summary="Launch the eNSP GUI, optionally on a topology file.",
        returns="JSON with the client pid and the path that was opened.",
        requires=(REQUIRES_ENSP,),
        error_codes=("ensp_not_found", "topo_not_found", "ensp_launch_failed"),
        notes=("Devices are not started by this call; in eNSP they are still "
               "selected and started with the start button."),
    ),
    ToolSpec(
        name="ensp_status",
        category="runtime",
        summary="Report whether eNSP and VirtualBox are present and what runs.",
        returns="JSON with the client path, the running processes, the device "
                "VMs and which consoles are occupied.",
        error_codes=("vbox_not_found",),
    ),
    ToolSpec(
        name="ensp_list_devices",
        category="runtime",
        summary="List the device VMs and how to reach each console.",
        returns="JSON with one row per VM.",
        requires=(REQUIRES_VBOX,),
        error_codes=("vbox_not_found",),
        notes=("serial_pipe is the agentd configuration handshake, not the "
               "console; the router CLI is the guest's TCP 40000."),
    ),
    ToolSpec(
        name="ensp_start_devices",
        category="lifecycle",
        summary="Start device virtual machines.",
        returns="JSON with the state of every device that was asked for.",
        requires=(REQUIRES_VBOX, REQUIRES_NOT_PROTECTED),
        error_codes=("vbox_not_found", "device_not_found",
                     "device_needs_ensp_client", "vm_protected",
                     "device_start_failed", "lab_create_failed",
                     "lab_not_found"),
        notes=("With lab_root the project's own lab supervisor is started "
               "instead, which is what brings a device up with a working "
               "console and an address. With topo_path as well, a lab is "
               "made from that topology first - its device names, its ssids "
               "and its links all come from the file - so one call starts a "
               "whole topology. A device of the eNSP GUI cannot be started "
               "this way: the eNSP client gives the guest its address and "
               "names its clones after the device's ssid, so the call answers "
               "device_needs_ensp_client."),
    ),
    ToolSpec(
        name="ensp_stop_devices",
        category="lifecycle",
        summary="Stop device virtual machines.",
        returns="JSON with the state of every device that was asked for.",
        requires=(REQUIRES_VBOX, REQUIRES_NOT_PROTECTED),
        error_codes=("vbox_not_found", "device_not_found",
                     "device_needs_ensp_client", "vm_protected",
                     "device_stop_failed"),
        notes=("The machines are asked to power off gracefully; force=true "
               "pulls the plug instead. A device of the eNSP GUI is owned by "
               "the eNSP client and answers device_needs_ensp_client."),
    ),
    ToolSpec(
        name="ensp_prepare_lab",
        category="lifecycle",
        summary="Shut down the ports of a lab that no link uses.",
        returns="JSON with the ports that were shut, per device.",
        error_codes=("lab_not_found",),
        notes=("A port with no peer keeps announcing itself on the console, "
               "and that noise splits the commands that are being typed - the "
               "router then reads half of one and rejects it."),
    ),
    ToolSpec(
        name="ensp_lab_status",
        category="lifecycle",
        summary="The state of a lab: the supervisor and every device in it.",
        returns="JSON with the per-device state and the tail of the log.",
        error_codes=("lab_not_found",),
        notes=("The machines of a lab are cloned and their images rewritten in "
               "the background, because that takes minutes; this is how the "
               "caller watches it."),
    ),
    ToolSpec(
        name="ensp_capture",
        category="capture",
        summary="Capture packets and read the IP headers back out.",
        returns="JSON with the capture file, the per-flow table and the DSCP "
                "distribution.",
        requires=(REQUIRES_ELEVATED,),
        error_codes=("capture_requires_elevation", "capture_failed",
                     "capture_backend_unknown"),
        notes=("pktmon needs an elevated process. Without elevation the call "
               "says so instead of capturing nothing."),
    ),
    ToolSpec(
        name="ensp_vrp_list_devices",
        category="console",
        summary="Find the eNSP guests and say who holds their console.",
        returns="JSON with one row per guest, its ports and its occupancy.",
        error_codes=("no_guests_reachable",),
    ),
    ToolSpec(
        name="ensp_vrp_command",
        category="console",
        summary="Run VRP commands on a device and return the exact text.",
        returns="JSON with the banner and the verbatim output.",
        requires=(REQUIRES_CONSOLE_FREE, REQUIRES_DEVICE_UP),
        error_codes=("no_commands_given", "console_held_by_ensp_client",
                     "console_held_by_other_client", "device_unreachable",
                     "console_no_echo"),
    ),
    ToolSpec(
        name="ensp_vrp_agent_handshake",
        category="console",
        summary="Run the guest agentd control handshake and report what it said.",
        returns="JSON with the events the guest sent.",
        error_codes=("device_unreachable", "agentd_busy"),
    ),
    ToolSpec(
        name="ensp_console_screen",
        category="guest-screen",
        summary="Capture a running device's guest screen (diagnostic only).",
        returns="The PNG of the guest's Linux console.",
        requires=(REQUIRES_VBOX,),
        error_codes=("vbox_not_found", "device_not_running"),
        notes=("This is a bitmap of the guest's Tiny Core Linux console, not "
               "the router CLI. It never answers a question about device "
               "state; use ensp_vrp_command for that."),
    ),
    ToolSpec(
        name="ensp_console_type",
        category="guest-screen",
        summary="Type text into a running device's guest console.",
        returns="A short confirmation string.",
        requires=(REQUIRES_VBOX,),
        error_codes=("vbox_not_found", "device_not_running"),
    ),
    ToolSpec(
        name="ensp_console_key",
        category="guest-screen",
        summary="Send a named key to a device's guest console.",
        returns="A short confirmation string.",
        requires=(REQUIRES_VBOX,),
        error_codes=("vbox_not_found", "device_not_running"),
    ),
    ToolSpec(
        name="ensp_console_send_lines",
        category="guest-screen",
        summary="Send a block of console commands, one line at a time.",
        returns="A short confirmation string.",
        requires=(REQUIRES_VBOX,),
        error_codes=("vbox_not_found", "device_not_running"),
    ),
    ToolSpec(
        name="ensp_tool_registry",
        category="meta",
        summary="This registry: every tool, its preconditions and its errors.",
        returns="JSON with one entry per tool.",
    ),
)

BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def get(name: str) -> ToolSpec | None:
    return BY_NAME.get(name)


def meta_for(name: str) -> dict:
    """The MCP metadata for a tool, or an empty dict for an unknown name."""
    spec = BY_NAME.get(name)
    return spec.meta() if spec else {}


def error_codes(name: str) -> tuple[str, ...]:
    spec = BY_NAME.get(name)
    return spec.error_codes if spec else ()


def as_dict() -> dict:
    return {
        "count": len(TOOLS),
        "categories": sorted({t.category for t in TOOLS}),
        "tools": [t.as_dict() for t in TOOLS],
    }
