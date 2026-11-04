# Copyright 2026 GentleFleet. All rights reserved. Proprietary and confidential.

'''A DESTINATION ON A NO-STOP JUNCTION IS REFUSED (F-909; G, 2026-11-03,
D-142 second addendum, word for word: "A destination on a no-stop junction is
refused when it's created, with the reason and the nearest place it can go,
and the mission intake refuses a mission to one, with the same reason."
Readings ee1-ee3, REQUIREMENTS.md).

A NO-STOP JUNCTION (ee1): a vertex where three or more lanes meet — the
builder's `junction` no-stop area (`safe_stops.areas`) — that is no place by
the product's rule (`safe_stops.places`: a dead end, or marked charger,
parking spot or holding point, or with a dock name). A marked place at a
junction is a place, and is not refused. Read from the lanes, never from the
level's `gf_no_stop` marks: an imported site is judged before the builder
marks it.

A named one is no destination (ee2): the site goes ahead, the junction stays
track and keeps its name on the map, it is not offered as a mission target,
and a mission to it is refused at intake (ee3) — each with this reason and
the nearest place it can go: the nearest named place by the way a robot
drives (the level's directed lanes, each its straight length) that is not
itself refused and is no robot's home (a charger or a parking spot: its robot
stands there, so a mission to it is refused as occupied, F-34), else the
nearest home; none when the junction reaches no named place.

Pure — standard library only, no ROS, no package import: the api-server
carries a byte-for-byte copy (`api_server/no_stop_destinations.py`), and the
place and junction rules are pinned to `safe_stops`' own on the shipped
graphs (`test/test_f909_no_stop_destinations.py`).
'''

import heapq
import math

# as `safe_stops.PLACE_FLAGS` and `safe_stops.PLACE_KEYS` (a test pins them)
PLACE_FLAGS = ('is_charger', 'is_parking_spot', 'is_holding_point')
HOME_FLAGS = ('is_charger', 'is_parking_spot')
PLACE_KEYS = ('dock_name',)
CODE = 'destination_on_no_stop_junction'
REASON = ('it is a no-stop junction: a robot standing on it closes every way '
          'through it')


def _params(entry):
    return entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {}


def _truthy(value):
    if isinstance(value, (list, tuple)):        # building-yaml style [4, true]
        value = value[-1] if value else None
    return bool(value) and value not in ('false', 'False', 0)


def _name(params):
    value = params.get('name')
    if isinstance(value, (list, tuple)):        # building-yaml style [1, "x"]
        value = value[-1] if value else None
    return str(value) if value else None


def _read(level):
    '''Positions, params, directed lanes out and undirected neighbours.'''
    vertices = level.get('vertices') or []
    xy = {i: (float(v[0]), float(v[1])) for i, v in enumerate(vertices)}
    params = {i: _params(v) for i, v in enumerate(vertices)}
    outs, nbrs = {}, {}
    for entry in level.get('lanes') or []:
        a, b = int(entry[0]), int(entry[1])
        if a == b:
            continue
        outs.setdefault(a, set()).add(b)
        nbrs.setdefault(a, set()).add(b)
        nbrs.setdefault(b, set()).add(a)
    return xy, params, outs, nbrs


def _is_place(p, degree):
    return degree == 1 or any(_truthy(p.get(k)) for k in PLACE_FLAGS) or \
        any(p.get(k) for k in PLACE_KEYS)


def junctions(level):
    '''Every no-stop junction of the level (vertex indices, sorted): three or
    more lanes meet there and it is no place.'''
    _xy, params, _outs, nbrs = _read(level)
    return sorted(v for v, n in nbrs.items()
                  if len(n) >= 3 and not _is_place(params[v], len(n)))


def refused(level):
    '''{name: refusal} for every named no-stop junction of the level. A
    refusal is a dict: code, name, reason, nearest (a name or None), metres
    (or None) and message (the operator's sentence).'''
    xy, params, outs, nbrs = _read(level)
    bad = set(junctions(level))
    named = {v: _name(params[v]) for v in nbrs if _name(params[v])}
    targets = {v for v in named if v not in bad and
               _is_place(params[v], len(nbrs[v]))}
    homes = {v for v in targets
             if any(_truthy(params[v].get(k)) for k in HOME_FLAGS)}
    out = {}
    for v in sorted(bad):
        if v not in named:
            continue
        near = _nearest(v, xy, outs, targets - homes, named) or \
            _nearest(v, xy, outs, homes, named)
        r = dict(code=CODE, name=named[v], reason=REASON,
                 nearest=near[1] if near else None,
                 metres=round(near[0], 1) if near else None)
        r['message'] = message(r)
        out[named[v]] = r
    return out


def refusal(level, name):
    '''The refusal of a destination called `name` on this level, or None
    when it is not a named no-stop junction.'''
    return refused(level).get(name)


def message(r):
    '''The operator's sentence: the reason and the nearest place.'''
    tail = (' The nearest place it can go: [%s], %.1f m.' % (r['nearest'],
                                                            r['metres'])
            if r['nearest'] is not None else
            ' It reaches no named place a robot may stop at.')
    return '[%s] cannot be a destination — %s.%s' % (r['name'], r['reason'],
                                                    tail)


def _nearest(src, xy, outs, targets, named):
    '''(metres, name) of the nearest target by directed lanes from src, ties
    by name; None when none is reached.'''
    dist = {src: 0.0}
    heap = [(0.0, src)]
    best = None
    while heap:
        d, v = heapq.heappop(heap)
        if d > dist.get(v, math.inf) + 1e-9:
            continue
        if best is not None and d > best[0] + 1e-9:
            break
        if v != src and v in targets:
            cand = (d, named[v])
            if best is None or cand < best:
                best = cand
            continue
        for w in outs.get(v, ()):
            nd = d + math.dist(xy[v], xy[w])
            if nd < dist.get(w, math.inf) - 1e-9:
                dist[w] = nd
                heapq.heappush(heap, (nd, w))
    return best
