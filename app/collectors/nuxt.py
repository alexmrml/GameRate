"""Reader for the Nuxt SSR payload Metacritic embeds in every page.

Metacritic renders with Nuxt and serialises the API response it used into a
``<script id="__NUXT_DATA__">`` tag using devalue's "flat" format: one JSON array
where every node addresses its children by array index. Reading that payload is
markedly more stable than scraping the generated markup, because it is the source
data the page itself was built from.
"""

import json
import re
from typing import Any

_PAYLOAD_PATTERN = re.compile(
    r'<script[^>]+id="__NUXT_DATA__"[^>]*>(?P<payload>.*?)</script>', re.DOTALL
)

# devalue reserves negative indexes for values that cannot be represented by a node.
_UNDEFINED = -1
_HOLE = -2
_NAN = -3
_POSITIVE_INFINITY = -4
_NEGATIVE_INFINITY = -5
_NEGATIVE_ZERO = -6

_SCALARS = {
    _UNDEFINED: None,
    _NAN: float("nan"),
    _POSITIVE_INFINITY: float("inf"),
    _NEGATIVE_INFINITY: float("-inf"),
    _NEGATIVE_ZERO: -0.0,
}


class NuxtPayloadError(ValueError):
    """The document does not carry a readable Nuxt payload."""


def _hydrate(nodes: list[Any], index: int, cache: dict[int, Any]) -> Any:
    if index in _SCALARS:
        return _SCALARS[index]
    if index in cache:
        return cache[index]
    if index < 0 or index >= len(nodes):
        raise NuxtPayloadError(f"Payload reference {index} is out of range")

    node = nodes[index]
    if isinstance(node, list):
        if node and isinstance(node[0], str):
            # Custom type: ["Date", ref], ["Set", ref, ...], ["ShallowReactive", ref], ...
            name = node[0]
            if name == "Map":
                mapping: dict[Any, Any] = {}
                cache[index] = mapping
                for key_ref, value_ref in zip(node[1::2], node[2::2], strict=False):
                    mapping[_hydrate(nodes, key_ref, cache)] = _hydrate(nodes, value_ref, cache)
                return mapping
            if name == "Set":
                members = []
                cache[index] = members
                members.extend(_hydrate(nodes, ref, cache) for ref in node[1:])
                return members
            value = _hydrate(nodes, node[1], cache) if len(node) > 1 else None
            cache[index] = value
            return value
        items: list[Any] = []
        cache[index] = items
        items.extend(None if ref == _HOLE else _hydrate(nodes, ref, cache) for ref in node)
        return items
    if isinstance(node, dict):
        mapping = {}
        cache[index] = mapping
        for key, ref in node.items():
            mapping[key] = _hydrate(nodes, ref, cache)
        return mapping
    cache[index] = node
    return node


def parse_payload(html: str) -> dict[str, Any]:
    """Return the hydrated Nuxt payload of an SSR document."""
    match = _PAYLOAD_PATTERN.search(html)
    if match is None:
        raise NuxtPayloadError("Document contains no __NUXT_DATA__ payload")
    try:
        nodes = json.loads(match.group("payload"))
    except json.JSONDecodeError as exc:
        raise NuxtPayloadError("__NUXT_DATA__ payload is not valid JSON") from exc
    if not isinstance(nodes, list) or not nodes:
        raise NuxtPayloadError("__NUXT_DATA__ payload is not a devalue node list")
    payload = _hydrate(nodes, 0, {})
    if not isinstance(payload, dict):
        raise NuxtPayloadError("__NUXT_DATA__ root is not an object")
    return payload


def payload_data(html: str) -> dict[str, Any]:
    """Return the ``data`` section, which holds one entry per fetched API route."""
    data = parse_payload(html).get("data")
    if not isinstance(data, dict):
        raise NuxtPayloadError("Payload carries no data section")
    return data


def find_data_entry(data: dict[str, Any], prefix: str) -> Any:
    """Return the first data entry whose route key starts with ``prefix``.

    Route keys embed slugs and query state (``loadPage:games:elden-ring:``), so callers
    match on the stable prefix instead of reconstructing the whole key.
    """
    for key, value in data.items():
        if key.startswith(prefix):
            return value
    raise NuxtPayloadError(f"Payload carries no {prefix!r} entry")


def components(page: Any) -> dict[str, Any]:
    """Map ``componentName`` to component data for a rendered page entry."""
    if not isinstance(page, dict):
        raise NuxtPayloadError("Page entry is not an object")
    result: dict[str, Any] = {}
    for component in page.get("components") or []:
        if not isinstance(component, dict):
            continue
        meta = component.get("meta")
        name = meta.get("componentName") if isinstance(meta, dict) else None
        if name and name not in result:
            result[name] = component.get("data") or {}
    return result
