"""Minimal decoder for Nuxt 3's devalue-flattened __NUXT_DATA__ payload.

Format: a flat JSON array. Index 0 is the root. Container entries hold integer
references into the array; primitives are stored inline at their own index.
Negative sentinels encode undefined/NaN/etc. Type wrappers look like
["Date", <ref>], ["Reactive", <ref>], ["Map", k1, v1, ...], etc.
"""
import json

UNDEFINED = -1
HOLE = -2
NAN = -3
POSITIVE_INFINITY = -4
NEGATIVE_INFINITY = -5
NEGATIVE_ZERO = -6
BIGINT = -7

# wrappers that just pass through to a single inner reference
PASSTHROUGH = {
    "Reactive",
    "ShallowReactive",
    "Ref",
    "ShallowRef",
    "NuxtRef",
    "NuxtShallowRef",
    "EmptyRef",
    "EmptyShallowRef",
    "Object",
}


def decode_nuxt_payload(payload):
    values = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    if not isinstance(values, list) or not values:
        raise ValueError("invalid nuxt payload")
    hydrated = {}
    in_progress = set()

    def hydrate(index):
        if isinstance(index, str) or index is None or isinstance(index, bool):
            return index
        if isinstance(index, float):
            return index
        if index == UNDEFINED:
            return None
        if index == HOLE:
            return None
        if index == NAN:
            return float("nan")
        if index == POSITIVE_INFINITY:
            return float("inf")
        if index == NEGATIVE_INFINITY:
            return float("-inf")
        if index == NEGATIVE_ZERO:
            return 0
        if index < 0:
            return None
        if index in hydrated:
            return hydrated[index]
        if index >= len(values):
            return None
        if index in in_progress:
            return None
        node = values[index]
        if not isinstance(node, (list, dict)):
            hydrated[index] = node
            return node
        in_progress.add(index)
        try:
            if isinstance(node, list):
                if node and isinstance(node[0], str):
                    kind = node[0]
                    if kind == "Date":
                        hydrated[index] = node[1]
                    elif kind == "BigInt":
                        hydrated[index] = str(node[1])
                    elif kind == "Set":
                        hydrated[index] = [hydrate(x) for x in node[1:]]
                    elif kind == "Map":
                        out = {}
                        for k, v in zip(node[1::2], node[2::2]):
                            out[str(hydrate(k))] = hydrate(v)
                        hydrated[index] = out
                    elif kind == "RegExp":
                        hydrated[index] = node[1]
                    elif kind == "null":
                        hydrated[index] = None
                    elif kind == "undefined":
                        hydrated[index] = None
                    elif kind in PASSTHROUGH:
                        hydrated[index] = hydrate(node[1])
                    else:
                        # unknown wrapper: hydrate remaining entries
                        hydrated[index] = [hydrate(x) for x in node[1:]]
                else:
                    arr = []
                    hydrated[index] = arr
                    for x in node:
                        arr.append(hydrate(x))
            else:
                obj = {}
                hydrated[index] = obj
                for k, v in node.items():
                    obj[k] = hydrate(v)
            return hydrated[index]
        finally:
            in_progress.discard(index)

    return hydrate(0)
