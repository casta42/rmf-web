# Copyright 2026 GentleFleet. All rights reserved. Proprietary and confidential.

'''A DESTINATION ON A NO-STOP JUNCTION IS REFUSED (F-909; G, 2026-11-03,
D-142 second addendum, word for word: "A destination on a no-stop junction is
refused when it's created, with the reason and the nearest place it can go,
and the mission intake refuses a mission to one, with the same reason."
Readings ee1-ee3, REQUIREMENTS.md). AND ON EVERY OTHER NO-STOP KIND (G,
2026-11-06, D-142 fourth addendum ruling 5: "The other no-stop kinds: (a),
refused with the same reason and the nearest place"; reading gg4): a turn's
vertex, a place's way out (its path past the place) and a single-lane
corridor's mouth (its end vertex), as the builder lays them
(`safe_stops.areas`), each with the reason for its kind.

A NO-STOP JUNCTION (ee1): a vertex where three or more lanes meet — the
builder's `junction` no-stop area (`safe_stops.areas`) — that is no place by
the product's rule (`safe_stops.places`: a dead end, or marked charger,
parking spot or holding point, or with a dock name). A marked place at a
junction is a place, and is not refused. Read from the lanes, never from the
level's `gf_no_stop` marks: an imported site is judged before the builder
marks it.

The other kinds are read from the no-stop areas the caller gives
(`safe_stops.areas` of the level: the caller's, so this file imports
nothing), or else from the level's own carried marks (`gf_no_stop`, the
builder's: every served graph carries them); a level with neither has only
its junctions read (`readable` says so, and a caller says it cannot see the
rest). A vertex that is a place is never one: a place's own vertex is a safe
stop.

A named one is no destination (ee2): the site goes ahead, the vertex stays
track and keeps its name on the map, it is not offered as a mission target,
and a mission to it is refused at intake (ee3) — each with its kind's reason
and the nearest place it can go: the nearest named place by the way a robot
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
# the fourth addendum's ruling 5: the other kinds, each its own code and reason
# (`safe_stops.KINDS`; a vertex in several areas takes the first kind here)
KINDS = ('junction', 'turn', 'way_out', 'mouth')
CODES = {'junction': CODE,
         'mouth': 'destination_on_corridor_mouth',
         'way_out': 'destination_on_way_out',
         'turn': 'destination_on_turn'}
REASONS = {'junction': REASON,
           'mouth': ("it is at a single-lane corridor's mouth: a robot "
                     'standing on it closes the corridor both ways'),
           'way_out': ("it is on [%s]'s way out: a robot standing on it "
                       'shuts that place in'),
           'turn': ('it is a turn: a robot standing on it blocks the turning '
                    'circle a robot needs there')}
# the key the builder carries a derived level's no-stop areas under
# (`safe_stops.KEY`; a test pins them equal)
AREAS_KEY = 'gf_no_stop'


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


def _areas(level, areas):
    if areas is not None:
        return areas
    return level.get(AREAS_KEY)


def readable(level, areas=None):
    '''Whether the kinds beyond the junctions can be read on this level: the
    caller gave its no-stop areas, or the level carries the builder's.'''
    return _areas(level, areas) is not None


def no_stop_vertices(level, areas=None):
    '''{vertex: (kind, the place whose way out it is or None)} for every
    vertex no robot may be sent to stop at, as the core reads it: the
    junctions, read from the lanes; a turn's vertex; and a place's way out
    past the place and a mouth's end vertex, except a single-lane corridor's
    inner vertices (each mouth area's `corridor`, its ends dropped), where a
    robot holding the corridor's direction may stand (FR-43) unless a turn's
    circle is there (STOP3) — the kinds beyond the junctions read from
    `areas` (`safe_stops.areas(level)`) or the level's carried marks. A
    place's own spur is no corridor: standing in it shuts the place in.
    Never a place. The first kind of KINDS names a vertex in several areas.'''
    _xy, params, _outs, nbrs = _read(level)
    marks = _areas(level, areas) or ()
    inner = set()
    for a in marks:
        if a.get('kind') == 'mouth':
            inner |= {int(v) for v in list(a.get('corridor') or [])[1:-1]}
    found = {v: [('junction', None)] for v in junctions(level)}
    for a in marks:
        k = a.get('kind')
        if k == 'turn':
            vs, place = [a.get('vertex')], None
        elif k == 'way_out':
            vs, place = list(a.get('path') or [])[1:], a.get('place')
        elif k == 'mouth':
            # its end vertex only: the corridor's own vertices are the
            # corridor's (a robot holding its direction may stand there,
            # FR-43; a turn on it is a turn)
            vs, place = list(a.get('lane') or [])[-1:], None
        else:
            continue                          # junctions: from the lanes
        for v in vs:
            v = int(v)
            if v not in nbrs or _is_place(params[v], len(nbrs[v])):
                continue
            if k != 'turn' and v in inner:
                continue
            found.setdefault(v, []).append((k, place))
    out = {}
    for v, ks in found.items():
        out[v] = min(ks, key=lambda kp: KINDS.index(kp[0]))
    return out


def refused(level, areas=None):
    '''{name: refusal} for every named no-stop vertex of the level (its
    junctions; with `areas`, or the level's carried marks, every other kind
    too). A refusal is a dict: code, kind, name, reason, nearest (a name or
    None), metres (or None) and message (the operator's sentence).'''
    xy, params, outs, nbrs = _read(level)
    bad = no_stop_vertices(level, areas)
    named = {v: _name(params[v]) for v in nbrs if _name(params[v])}
    targets = {v for v in named if v not in bad and
               _is_place(params[v], len(nbrs[v]))}
    homes = {v for v in targets
             if any(_truthy(params[v].get(k)) for k in HOME_FLAGS)}
    out = {}
    for v in sorted(bad):
        if v not in named:
            continue
        kind, place = bad[v]
        reason = REASONS[kind]
        if kind == 'way_out':
            reason = reason % (named.get(place) or '#%s' % place)
        near = _nearest(v, xy, outs, targets - homes, named) or \
            _nearest(v, xy, outs, homes, named)
        r = dict(code=CODES[kind], kind=kind, name=named[v], reason=reason,
                 nearest=near[1] if near else None,
                 metres=round(near[0], 1) if near else None)
        r['message'] = message(r)
        out[named[v]] = r
    return out


def refusal(level, name, areas=None):
    '''The refusal of a destination called `name` on this level, or None
    when it is not a named no-stop vertex.'''
    return refused(level, areas).get(name)


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
