from __future__ import annotations

from xml.etree import ElementTree as ET

from frost.domain import Fault, bounded_int, text


ATOM = "{http://www.w3.org/2005/Atom}"
FROST = "{urn:frost:bulletins:1}"


def child_text(parent, name, required=True):
    nodes = parent.findall(name)
    if len(nodes) > 1:
        raise Fault("ambiguous_atom", "Atom单值字段出现多次")
    if not nodes:
        if required:
            raise Fault("missing_atom_field", "Atom缺少必要字段")
        return None
    if list(nodes[0]):
        raise Fault("atom_markup", "Atom通告字段只接受纯文本")
    return text(nodes[0].text, name.rsplit("}", 1)[-1], 30000)


def parse_atom(raw: str):
    if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise Fault("unsafe_xml", "来源XML不能声明实体或文档类型")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, RecursionError) as exc:
        raise Fault("malformed_atom", "Atom来源无法解析") from exc
    if root.tag != ATOM + "feed":
        raise Fault("invalid_atom", "Atom根节点无效")
    snapshot = child_text(root, FROST + "snapshot")
    total_text = child_text(root, FROST + "total")
    try:
        total = int(total_text)
    except ValueError as exc:
        raise Fault("invalid_total", "Atom通告总数无效") from exc
    bounded_int(total, "通告总数", 0, 10000)
    links = [node.attrib.get("href") for node in root.findall(ATOM + "link") if node.attrib.get("rel") == "next"]
    if len(links) > 1:
        raise Fault("ambiguous_atom", "Atom含多个下一页")
    next_url = links[0] if links else None
    if links:
        text(next_url, "下一页", 2048)
    items = []
    for entry in root.findall(ATOM + "entry"):
        if len(items) >= 1000:
            raise Fault("invalid_items", "Atom单页通告过多")
        revision = child_text(entry, FROST + "revision")
        try:
            revision = int(revision)
        except ValueError as exc:
            raise Fault("invalid_revision", "Atom通告修订号无效") from exc
        categories = entry.findall(ATOM + "category")
        regions = [node.attrib.get("term") for node in categories if node.attrib.get("scheme") == "urn:frost:region"]
        facilities = [node.attrib.get("term") for node in categories if node.attrib.get("scheme") == "urn:frost:facility"]
        content = entry.findall(ATOM + "content")
        if len(content) != 1 or content[0].attrib.get("type", "text") != "text" or content[0].attrib.get("src"):
            raise Fault("atom_content", "Atom通告正文必须内联且为纯文本")
        items.append({
            "id": child_text(entry, ATOM + "id"),
            "canonical_id": child_text(entry, FROST + "canonical-id"),
            "revision": revision,
            "title": child_text(entry, ATOM + "title"),
            "body": child_text(entry, ATOM + "content"),
            "issued": child_text(entry, ATOM + "updated"),
            "expires": child_text(entry, FROST + "expires"),
            "state": child_text(entry, FROST + "state"),
            "level": child_text(entry, FROST + "level"),
            "regions": regions,
            "facilities": facilities,
        })
    return snapshot, total, items, next_url
