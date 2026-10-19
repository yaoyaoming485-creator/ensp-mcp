"""Read and write eNSP ``.topo`` topology files.

Reverse-engineered layout of the format (eNSP 1.3.00.100)::

    <?xml version="1.0" encoding="UNICODE" ?>
    <topo version="1.3.00.100">
        <devices><dev ...><slot>...</slot></dev></devices>
        <lines><line srcDeviceID destDeviceID><interfacePair .../></line></lines>
        <shapes>...</shapes>
        <txttips>...</txttips>
    </topo>

Two details are easy to get wrong and both are handled here:

* the file is **UTF-8 without a BOM**, yet still declares
  ``encoding="UNICODE"``.  All 40 example topologies shipped with eNSP
  1.3.00.100 are like this, as are the files eNSP itself saves.  Writing
  UTF-16LE (what the declaration suggests) makes eNSP load the file without
  any error and show **0 devices** -- the failure is silent;
* ``srcIndex`` / ``tarIndex`` are indices into the device model's flattened
  interface list, *not* slot numbers.
"""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from . import models

TOPO_VERSION = "1.3.00.100"

#: Encodings eNSP is known to accept.  ``utf-8`` (no BOM) is the only one that
#: matches what eNSP writes itself, and the only one verified to open cleanly on
#: older releases (V1.2.00.390 rejects UTF-16LE by loading zero devices).
DEFAULT_ENCODING = "utf-8"
ENCODINGS = ("utf-8", "utf-8-sig", "utf-16")


def resolve_version(explicit: str | None = None) -> str:
    """Pick the ``<topo version="...">`` value.

    eNSP accepts a missing or mismatched version, so this is not load-bearing the
    way the encoding is -- but matching the installed release is free and avoids
    surprises.  Explicit value wins, then the installed eNSP, then a default.
    """
    if explicit:
        return str(explicit)
    try:
        from . import vbox

        detected = vbox.find_ensp_version()
        if detected:
            return detected
    except Exception:  # noqa: BLE001 - detection is best effort
        pass
    return TOPO_VERSION

# Canvas geometry.  A device occupies roughly 80x60 px; edit_left/edit_top are
# the offsets eNSP stores for the inline name editor.
DEVICE_W = 80
DEVICE_H = 60
EDIT_DX = 27
EDIT_DY = 54
GRID_X = 300
GRID_Y = 200
ORIGIN_X = 150
ORIGIN_Y = 250
ROW_WRAP_PX = 1600


class TopoError(ValueError):
    """Raised for a spec that cannot produce a valid topology."""


def new_uuid() -> str:
    return str(uuid.uuid4()).upper()


def _slug_mac(seed: str) -> str:
    """Deterministic locally-administered MAC derived from a device UUID."""
    h = uuid.UUID(seed).int
    octets = [(h >> (8 * i)) & 0xFF for i in range(6)]
    octets[0] = (octets[0] | 0x02) & 0xFE  # locally administered, unicast
    return "-".join(f"{b:02X}" for b in octets)


def _layout(count: int, positions: dict[str, tuple[int, int]] | None = None) -> list[tuple[int, int]]:
    """Grid auto-layout: left to right, wrapping at ROW_WRAP_PX."""
    out: list[tuple[int, int]] = []
    x, y = ORIGIN_X, ORIGIN_Y
    for _ in range(count):
        out.append((x, y))
        x += GRID_X
        if x > ROW_WRAP_PX:
            x = ORIGIN_X
            y += GRID_Y
    return out


def _port_index(model: models.Model, spec: dict, key_int: str, key_name: str, dev_name: str) -> int:
    if key_int in spec and spec[key_int] is not None:
        idx = int(spec[key_int])
    elif key_name in spec and spec[key_name] is not None:
        want = str(spec[key_name])
        if want not in model.ports:
            raise TopoError(
                f"{dev_name} ({model.name}) has no interface {want!r}; "
                f"available: {', '.join(model.ports) or '(none)'}"
            )
        idx = model.ports.index(want)
    else:
        idx = 0
    if idx < 0 or idx >= model.port_count:
        raise TopoError(
            f"{dev_name} ({model.name}) interface index {idx} out of range "
            f"(model has {model.port_count} interfaces)"
        )
    return idx


def build(spec: dict, version: str | None = None) -> str:
    """Turn a JSON spec into ``.topo`` XML text.

    Spec::

        {
          "devices": [{"name": "R1", "model": "AR2220", "x": 150, "y": 250}],
          "links":   [{"a": "R1", "b": "R2", "a_port": 0, "b_port": 0, "type": "Copper"}],
          "labels":  [{"text": "Area0", "x": 190, "y": 160}],
          "boxes":   [{"x": 100, "y": 100, "w": 400, "h": 200, "color": 16776960}]
        }

    ``x``/``y`` are optional (auto-layout), ``a_port``/``b_port`` accept either an
    integer index or ``a_if``/``b_if`` with an interface name like ``GE0/0/1``.
    """
    devices = spec.get("devices") or []
    if not devices:
        raise TopoError("spec has no devices")

    seen: dict[str, dict] = {}
    for d in devices:
        name = str(d.get("name") or "").strip()
        if not name:
            raise TopoError("every device needs a name")
        if name in seen:
            raise TopoError(f"duplicate device name {name!r}")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
            raise TopoError(f"device name {name!r} contains characters eNSP rejects")
        seen[name] = d

    auto = _layout(len(devices))
    resolved: list[dict] = []
    com_port = 2000
    for i, d in enumerate(devices):
        name = str(d["name"])
        try:
            model = models.get(str(d.get("model") or "AR2220"))
        except KeyError as exc:
            raise TopoError(str(exc)) from None
        dev_id = str(d.get("id") or new_uuid())
        cx = int(d["x"]) if d.get("x") is not None else auto[i][0]
        cy = int(d["y"]) if d.get("y") is not None else auto[i][1]

        settings = d.get("settings")
        if settings is None and model.settings_template:
            settings = model.settings_template.format(
                ip=d.get("ip", "0.0.0.0"),
                mask=d.get("mask", "255.255.255.0"),
                gw=d.get("gw", "0.0.0.0"),
                mac=d.get("mac", _slug_mac(dev_id)),
                sn=d.get("sn", "21023500000000000000"),
            )
        if model.has_com_port:
            port = int(d["com_port"]) if d.get("com_port") is not None else com_port
            com_port += 1
        else:
            port = int(d.get("com_port") or 0)

        resolved.append(
            {
                "name": name,
                "model": model,
                "id": dev_id,
                "cx": cx,
                "cy": cy,
                "settings": settings or "",
                "mac": str(d.get("system_mac") or ""),
                "com_port": port,
                "bootmode": int(d.get("bootmode") or 0),
            }
        )

    by_name = {r["name"]: r for r in resolved}

    lines: list[str] = []
    for link in spec.get("links") or []:
        a, b = str(link.get("a") or ""), str(link.get("b") or "")
        for end in (a, b):
            if end not in by_name:
                raise TopoError(f"link references unknown device {end!r}")
        if a == b:
            raise TopoError(f"link {a} -> {b} connects a device to itself")
        ra, rb = by_name[a], by_name[b]
        ai = _port_index(ra["model"], link, "a_port", "a_if", a)
        bi = _port_index(rb["model"], link, "b_port", "b_if", b)
        line_name = str(link.get("type") or "Copper")
        lines.append(
            f'        <line srcDeviceID="{ra["id"]}" destDeviceID="{rb["id"]}">\n'
            f'            <interfacePair lineName="{escape(line_name, {chr(34): "&quot;"})}" '
            f'srcIndex="{ai}" tarIndex="{bi}" />\n'
            f"        </line>"
        )

    dev_xml: list[str] = []
    for r in resolved:
        dev_xml.append(
            f'        <dev id="{r["id"]}" name={quoteattr(r["name"])} poe="0" '
            f'model="{r["model"].name}" settings={quoteattr(r["settings"])} '
            f'system_mac="{r["mac"]}" com_port="{r["com_port"]}" bootmode="{r["bootmode"]}" '
            f'cx="{r["cx"]:.6f}" cy="{r["cy"]:.6f}" '
            f'edit_left="{r["cx"] + EDIT_DX}" edit_top="{r["cy"] + EDIT_DY}">\n'
            f'{r["model"].xml}\n'
            f"        </dev>"
        )

    tip_xml: list[str] = []
    for t in spec.get("labels") or []:
        text = str(t.get("text") or "")
        x = int(t.get("x") or 0)
        y = int(t.get("y") or 0)
        w = int(t.get("w") or max(120, 9 * len(text)))
        h = int(t.get("h") or 26)
        body = text.replace("\r\n", "&#x0D;&#x0A;").replace("\n", "&#x0D;&#x0A;")
        tip_xml.append(
            f'        <txttip left="{x}" top="{y}" right="{x + w}" bottom="{y + h}" '
            f'content={quoteattr(body)} fontname="{t.get("font", "Consolas")}" '
            f'fontstyle="{int(t.get("style", 0))}" editsize="{int(t.get("size", 100))}" '
            f'txtcolor="{int(t.get("color", -16777216))}" '
            f'txtbkcolor="{int(t.get("background", -1))}" charset="1" />'
        )

    shape_xml: list[str] = []
    for s in spec.get("boxes") or []:
        x, y = int(s.get("x") or 0), int(s.get("y") or 0)
        w, h = int(s.get("w") or 100), int(s.get("h") or 100)
        shape_xml.append(
            f'        <shape type="{int(s.get("type", 1))}" filloption="1" '
            f'color="{int(s.get("color", 12632256))}" upleftcorner="{x},{y}" '
            f'width="{w}" height="{h}" startpt="0,0" endpt="0,0" '
            f'borderStyle="5" borderWidth="1" />'
        )

    parts = [
        '<?xml version="1.0" encoding="UNICODE" ?>',
        f'<topo version="{escape(resolve_version(version))}">',
        "    <devices>",
        *dev_xml,
        "    </devices>",
        "    <lines>",
        *lines,
        "    </lines>",
        "    <shapes>",
        *shape_xml,
        "    </shapes>",
        "    <txttips>",
        *tip_xml,
        "    </txttips>",
        "</topo>",
        "",
    ]
    return "\n".join(parts)


def write(
    spec: dict,
    out_path: str | Path,
    version: str | None = None,
    encoding: str = DEFAULT_ENCODING,
) -> Path:
    """Write a topology file.

    ``encoding`` defaults to ``utf-8`` **without a BOM**, which is what eNSP
    writes and reads.  ``utf-16`` is offered only because some third-party
    generators emit it; eNSP silently loads such a file as zero devices.
    """
    if encoding not in ENCODINGS:
        raise TopoError(f"unsupported encoding {encoding!r}; choose one of {', '.join(ENCODINGS)}")
    path = Path(out_path)
    if path.suffix.lower() != ".topo":
        path = path.with_suffix(".topo")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(spec, version=version), encoding=encoding)
    return path


def _as_int(value: str | None, default: int = 0) -> int:
    """eNSP writes some numeric attributes as floats, e.g. cx="220.000000"."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return default


def read(path: str | Path) -> dict:
    """Parse a ``.topo`` into the same shape :func:`build` accepts."""
    path = Path(path)
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encoding, has_bom, text = "utf-16", True, raw.decode("utf-16")
    elif raw[:3] == b"\xef\xbb\xbf":
        encoding, has_bom, text = "utf-8-sig", True, raw.decode("utf-8-sig")
    else:
        encoding, has_bom, text = "utf-8", False, raw.decode("utf-8", "replace")
    root = ET.fromstring(text)
    if root.tag != "topo":
        raise TopoError(f"{path} is not an eNSP topology (root <{root.tag}>)")

    devices = []
    for dev in root.findall("./devices/dev"):
        model_name = dev.get("model", "")
        model = models.CATALOG.get(model_name)
        entry = {
            "name": dev.get("name", ""),
            "model": model_name,
            "id": dev.get("id", ""),
            "x": _as_int(dev.get("cx")),
            "y": _as_int(dev.get("cy")),
            "com_port": _as_int(dev.get("com_port")),
        }
        if model is not None:
            entry["ports"] = list(model.ports)
            if model_name in models.NEEDS_IMAGE:
                entry["needs_device_image"] = True
        else:
            entry["ports"] = None
        devices.append(entry)

    by_id = {d["id"]: d for d in devices}
    links = []
    for line in root.findall("./lines/line"):
        src, dst = line.get("srcDeviceID", ""), line.get("destDeviceID", "")
        for pair in line.findall("./interfacePair"):
            links.append(
                {
                    "a": by_id.get(src, {}).get("name", src),
                    "b": by_id.get(dst, {}).get("name", dst),
                    "type": pair.get("lineName", "Copper"),
                    "a_port": _as_int(pair.get("srcIndex")),
                    "b_port": _as_int(pair.get("tarIndex")),
                    "a_if": _if_at(by_id.get(src, {}), pair.get("srcIndex")),
                    "b_if": _if_at(by_id.get(dst, {}), pair.get("tarIndex")),
                }
            )

    labels = [
        {
            "text": (t.get("content") or "").replace("&#x0D;&#x0A;", "\n"),
            "x": _as_int(t.get("left")),
            "y": _as_int(t.get("top")),
        }
        for t in root.findall("./txttips/txttip")
    ]
    boxes = []
    for s in root.findall("./shapes/shape"):
        if s.get("type") not in ("1", "2"):
            continue
        corner = (s.get("upleftcorner") or "0,0").split(",")
        boxes.append(
            {
                "x": _as_int(corner[0] if corner else None),
                "y": _as_int(corner[1] if len(corner) > 1 else None),
                "w": _as_int(s.get("width")),
                "h": _as_int(s.get("height")),
                "color": _as_int(s.get("color")),
            }
        )
    return {
        "version": root.get("version"),
        "encoding": encoding,
        "has_bom": has_bom,
        # eNSP loads a UTF-16 topology without any error and shows zero devices.
        "loads_in_ensp": encoding == "utf-8" and not has_bom,
        "devices": devices,
        "links": links,
        "labels": labels,
        "boxes": boxes,
    }


def _if_at(device: dict, index: str | None) -> str | None:
    ports = device.get("ports")
    if not ports or index is None:
        return None
    try:
        return ports[int(index)]
    except (ValueError, IndexError):
        return None


def validate(spec: dict) -> dict:
    """Check a spec without writing anything. Returns a report, never raises."""
    problems: list[str] = []
    warnings: list[str] = []
    try:
        xml = build(spec)
    except TopoError as exc:
        return {"ok": False, "problems": [str(exc)], "warnings": []}
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as a problem
        return {"ok": False, "problems": [f"{type(exc).__name__}: {exc}"], "warnings": []}

    devices = spec.get("devices") or []
    for d in devices:
        model_name = str(d.get("model") or "AR2220")
        if model_name in models.NEEDS_IMAGE:
            warnings.append(
                f"{d.get('name')} is a {model_name}: its device image does not ship with eNSP, "
                "so it cannot start until the image is imported via eNSP's 导入设备包 dialog."
            )
    return {
        "ok": True,
        "problems": problems,
        "warnings": warnings,
        "device_count": len(devices),
        "link_count": len(spec.get("links") or []),
        "xml_bytes": len(xml.encode("utf-16")),
    }
