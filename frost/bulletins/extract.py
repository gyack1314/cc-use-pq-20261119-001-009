from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser

from frost.domain import FACILITIES, LEVELS, Fault, bounded_int, digest, string_set, text, timestamp


@dataclass(frozen=True)
class Page:
    snapshot: str
    total: int
    items: list[dict]
    next_url: str | None


class BulletinHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metadata = {}
        self.items = []
        self.current = None
        self.field = None
        self.depth = 0
        self.field_depth = 0
        self.pieces = []
        self.next_url = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("name") in {"snapshot", "total"}:
            key = values["name"]
            if key in self.metadata:
                raise Fault("ambiguous_html", "页面重复声明轮次信息")
            self.metadata[key] = values.get("content", "")
        if tag == "a" and values.get("rel") == "next":
            if self.next_url is not None:
                raise Fault("ambiguous_html", "页面含多个下一页")
            self.next_url = values.get("href", "")
        if tag == "article":
            if self.current is not None:
                raise Fault("ambiguous_html", "通告节点发生嵌套")
            self.current = {}
            for key in ("id", "canonical_id", "revision", "issued", "expires", "state", "level"):
                self.current[key] = values.get("data-" + key)
            for key in ("regions", "facilities"):
                self.current[key] = (values.get("data-" + key) or "").split(",")
        if self.current is not None:
            if tag not in {"meta", "br", "img", "input", "hr", "link"}:
                self.depth += 1
            name = values.get("data-field")
            if name in {"title", "body"}:
                if self.field is not None or name in self.current:
                    raise Fault("ambiguous_html", "通告正文字段重复或嵌套")
                self.field = name
                self.field_depth = self.depth
                self.pieces = []

    def handle_endtag(self, tag):
        if self.current is None:
            return
        if self.field and self.depth == self.field_depth:
            self.current[self.field] = "".join(self.pieces).strip()
            self.field = None
        if tag == "article":
            if self.depth != 1:
                raise Fault("malformed_html", "通告节点没有完整闭合")
            self.items.append(self.current)
            self.current = None
        self.depth -= 1

    def handle_data(self, data):
        if self.field:
            self.pieces.append(data)


def decode(body: bytes, content_type: str) -> str:
    match = re.search(r"charset=([^;\s]+)", content_type, re.I)
    encoding = match.group(1).strip("\"'") if match else "utf-8"
    if encoding.lower() not in {"utf-8", "utf8", "gb18030"}:
        raise Fault("unsupported_charset", "来源字符集不受支持")
    try:
        return body.decode(encoding, errors="strict")
    except UnicodeError as exc:
        raise Fault("invalid_encoding", "来源字节与声明字符集不符") from exc


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Fault("duplicate_key", "JSON含重复字段")
        result[key] = value
    return result


def parse_page(body: bytes, format: str, content_type: str) -> Page:
    raw = decode(body, content_type)
    media = content_type.split(";")[0].strip().lower()
    if format == "json":
        if media not in {"application/json", "application/feed+json"}:
            raise Fault("content_type", "来源不是约定的JSON媒体类型")
        try:
            value = json.loads(raw, object_pairs_hook=unique_object)
        except (ValueError, RecursionError) as exc:
            raise Fault("malformed_json", "来源JSON无法解析") from exc
        if not isinstance(value, dict):
            raise Fault("invalid_page", "来源根节点必须是对象")
        snapshot, total = value.get("snapshot"), value.get("total")
        items, next_url = value.get("items"), value.get("next")
    elif format == "html":
        if media not in {"text/html", "application/xhtml+xml"}:
            raise Fault("content_type", "来源不是约定的HTML媒体类型")
        parser = BulletinHTML()
        parser.feed(raw)
        parser.close()
        if parser.current is not None:
            raise Fault("malformed_html", "HTML通告未闭合")
        snapshot = parser.metadata.get("snapshot")
        try:
            total = int(parser.metadata.get("total", ""))
        except ValueError as exc:
            raise Fault("invalid_page", "HTML缺少有效总数") from exc
        items, next_url = parser.items, parser.next_url
        for item in items:
            try:
                item["revision"] = int(item["revision"])
            except (ValueError, TypeError) as exc:
                raise Fault("invalid_revision", "HTML通告修订号无效") from exc
    elif format == "atom":
        if media not in {"application/atom+xml", "application/xml", "text/xml"}:
            raise Fault("content_type", "来源不是约定的Atom媒体类型")
        from frost.bulletins.atom import parse_atom
        snapshot, total, items, next_url = parse_atom(raw)
    else:
        raise Fault("invalid_format", "来源格式不受支持", 422)
    snapshot = text(snapshot, "快照标识", 160)
    total = bounded_int(total, "通告总数", 0, 10000)
    if not isinstance(items, list) or len(items) > 1000:
        raise Fault("invalid_items", "分页通告列表无效")
    if next_url is not None:
        next_url = text(next_url, "下一页", 2048)
    return Page(snapshot, total, [normalize(item) for item in items], next_url)


def normalize(item: dict) -> dict:
    if not isinstance(item, dict):
        raise Fault("invalid_item", "通告必须是对象")
    result = {key: text(item.get(key), key, limit) for key, limit in (
        ("id", 160), ("canonical_id", 160), ("title", 300), ("body", 30000),
    )}
    result["revision"] = bounded_int(item.get("revision"), "修订号", 1, 1000000000)
    result["issued"] = timestamp(item.get("issued"))
    result["expires"] = timestamp(item.get("expires"))
    if result["expires"] <= result["issued"]:
        raise Fault("invalid_period", "失效时间必须晚于发布时间")
    state = item.get("state", "active")
    if state not in {"active", "cancelled"}:
        raise Fault("invalid_state", "来源通告状态无效")
    level = item.get("level")
    if level not in LEVELS:
        raise Fault("invalid_level", "来源通告级别无效")
    result.update(state=state, level=level)
    result["regions"] = string_set(item.get("regions"), "区域")
    result["facilities"] = string_set(item.get("facilities"), "设施", FACILITIES)
    return result


def semantic(document: dict) -> str:
    return digest({key: value for key, value in document.items() if key != "id"})
