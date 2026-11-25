# Copyright 2026 GentleFleet. All rights reserved. Proprietary and confidential.

'''THE NO-STOP AREAS OF A DERIVED GRAPH (D-139 ruling 1; FR-40, FR-46,
FR-43).

Across D-136 to D-138 every lock that was not a defect was a robot stopped
where another robot needed to turn or to get out: in a place's way out
(F-781, F-791, F-807), or in a turning circle at a corridor's mouth (F-792,
F-805). G's ruling: one rule, laid down when the lanes are built, replaces
the case-by-case holds. The builder marks the no-stop areas as part of the
derived network (`regen.finish_graph`, the last pass every graph the fleet
loads goes through); the traffic core never ends a grant inside one
(D-139 ruling 2).

The areas, in track terms (D-139 reading s1) — vertices and lanes of the
level, so the builder's marks and the core's own reading of the same graph
can be compared exactly (reading s3):

  junction  every vertex where three or more lanes meet: the junction
            block round it (the core builds the block's extent: the cells
            within the separation of the turning circle there, D-137
            ruling 2);
  turn      every vertex where a robot turns more than the drive's 0.4 rad
            (`TURN_IN_PLACE`, D-100's drive): a lane in followed by a lane
            out to another neighbour bends over it, or a dead end, where a
            robot turns about — its turning circle (the core reads the
            circle with the shape it is configured with);
  way_out   every place's way out to its first real choice (D-138 reading
            r1, confirmed by D-139 ruling 8): from the place along the
            lanes to the first vertex where a robot leaving has a real
            choice of ways on, that vertex included — the place's own
            vertex is a safe stop, not part of it. Where every lane of it
            past the place's own first lane is served one way, it ends at
            the first vertex past the place's own lane (`oneway_cut`; D-142
            twentieth addendum, G's ruling 5; CORE_DESIGN.md §4.14 class
            (1)): there a robot leaving can only follow the robot ahead of
            it, and nobody can come at a robot standing there the other way;
  mouth     each single-lane corridor — a chain of two-way lanes through
            vertices of two lanes, a junction (no dead end) at each end —
            at each end: its last lane to the end vertex and the turning
            circle at that end vertex, whatever the bend there.

A PLACE (as D-138's ways-out check reads it, `ways_out.places_of`): every
dead end, and every charger, parking spot, holding point and dock.

Pure — standard library only, no ROS, no package import: the candidate
traffic core carries a byte-for-byte copy of this file
(`ops/traffic_model/core139/safe_stops.py`; a test pins the two equal).
'''

import math
from collections import defaultdict

# the drive turns in place above this bend (rad): D-100's drive, the core's
# `Profile.turn_in_place`
TURN_IN_PLACE = 0.4
# the level key the derived graph carries its no-stop areas under (RMF's
# parser reads only a level's `vertices` and `lanes`)
KEY = 'gf_no_stop'
KINDS = ('junction', 'turn', 'way_out', 'mouth')
PLACE_FLAGS = ('is_charger', 'is_parking_spot', 'is_holding_point')
PLACE_KEYS = ('dock_name',)


def _params(entry):
    return entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {}


def _truthy(value):
    if isinstance(value, (list, tuple)):        # building-yaml style [4, true]
        value = value[-1] if value else None
    return bool(value) and value not in ('false', 'False', 0)


class _Graph:
    '''A level as the areas read it: positions, names, directed lanes.'''

    def __init__(self, level):
        vertices = level.get('vertices') or []
        self.xy = {i: (float(v[0]), float(v[1]))
                   for i, v in enumerate(vertices)}
        self.params = {i: _params(v) for i, v in enumerate(vertices)}
        self.outs = defaultdict(list)
        self.nbrs = defaultdict(set)
        self.directed = set()
        for entry in level.get('lanes') or []:
            a, b = int(entry[0]), int(entry[1])
            if a == b:
                continue
            if b not in self.outs[a]:
                self.outs[a].append(b)
            self.directed.add((a, b))
            self.nbrs[a].add(b)
            self.nbrs[b].add(a)
        for a in self.outs:
            self.outs[a].sort()

    def name(self, v):
        p = self.params.get(v, {})
        return str(p.get('name') or p.get('gf_track_junction') or
                   '#%d' % v)

    def two_way(self, a, b):
        return (a, b) in self.directed and (b, a) in self.directed

    def heading(self, a, b):
        (xa, ya), (xb, yb) = self.xy[a], self.xy[b]
        return math.atan2(yb - ya, xb - xa)


def _bend(g, u, v, w):
    d = g.heading(v, w) - g.heading(u, v)
    return abs(math.atan2(math.sin(d), math.cos(d)))


def places(g):
    '''Every vertex a robot stands at: the dead ends and the roles.'''
    out = []
    for v in sorted(g.nbrs):
        p = g.params.get(v, {})
        if len(g.nbrs[v]) == 1 \
                or any(_truthy(p.get(k)) for k in PLACE_FLAGS) \
                or any(p.get(k) for k in PLACE_KEYS):
            out.append(v)
    return out


def way_out(g, place):
    '''A place's only way out to its first real choice (D-138 reading r1,
    the core's `Track.only_way_out` and `ways_out.way_out`'s 'choice'): the
    vertices from the place on, its end included — while a robot leaving
    has exactly one lane on whose far side, that vertex closed, still
    reaches half the graph (a dead end off it is no choice).'''
    half = len(g.nbrs) / 2.0
    memo = {}

    def reach(w, banned):
        key = (w, banned)
        if key not in memo:
            seen, stack = {w}, [w]
            while stack:
                x = stack.pop()
                for y in g.outs.get(x, ()):
                    if y != banned and y not in seen:
                        seen.add(y)
                        stack.append(y)
            memo[key] = len(seen)
        return memo[key]
    path, prev, v = [place], None, place
    while True:
        real = [w for w in g.outs.get(v, ()) if w != prev
                and w not in path and reach(w, v) >= half]
        if len(real) != 1:
            return path
        prev, v = v, real[0]
        path.append(v)


def oneway_cut(g, path):
    '''The way out's MARK (D-142 twentieth addendum, G's ruling 5; F-982;
    CORE_DESIGN.md §4.14 (a) 1, class (1)'s clause): a way out of more than
    one lane whose every lane past the place's own first lane is served one
    way (no lane back) is marked no-stop only to the first vertex past the
    place's own lane; else the path as it is. The way out itself
    (`way_out`) is the way a robot leaves, and is not changed.'''
    if len(path) > 2 and all((path[j + 1], path[j]) not in g.directed
                             for j in range(1, len(path) - 1)):
        return path[:2]
    return path


def turning_vertices(g, lim=TURN_IN_PLACE):
    '''{vertex: the largest bend a robot drives there} for every vertex
    where it turns more than `lim`: a lane in, then a lane out to another
    neighbour; a dead end (a robot turns about there) reads pi.'''
    ins = defaultdict(list)
    for a, outs in g.outs.items():
        for b in outs:
            ins[b].append(a)
    out = {}
    for v in sorted(g.nbrs):
        if len(g.nbrs[v]) == 1:
            out[v] = math.pi
            continue
        best = 0.0
        for u in ins.get(v, ()):
            for w in g.outs.get(v, ()):
                if w != u:
                    best = max(best, _bend(g, u, v, w))
        if best > lim + 1e-9:
            out[v] = best
    return out


def corridors(g):
    '''Every single-lane corridor: a maximal chain of two-way lanes through
    vertices of two lanes (both two-way), with a junction or a lane of
    another kind at each end and no dead end — its vertices, ends first
    and last, the lower-numbered end first. A chain that ends at a dead end
    is a spur (a place's way out); a closed loop of two-way lanes has no
    mouth.'''
    def chain_vertex(v):
        n = g.nbrs.get(v, ())
        return len(n) == 2 and all(g.two_way(v, w) for w in n)
    seen = set()
    out = []
    for a in sorted(g.nbrs):
        for b in sorted(g.nbrs[a]):
            if not g.two_way(a, b) or frozenset((a, b)) in seen:
                continue
            # extend both ways through chain vertices
            path = [a, b]
            seen.add(frozenset((a, b)))
            loop = False
            for side in (0, 1):
                while True:
                    end = path[-1] if side == 0 else path[0]
                    prev = path[-2] if side == 0 else path[1]
                    if not chain_vertex(end):
                        break
                    nxt = [w for w in g.nbrs[end] if w != prev][0]
                    if frozenset((end, nxt)) in seen:
                        loop = True
                        break
                    seen.add(frozenset((end, nxt)))
                    if side == 0:
                        path.append(nxt)
                    else:
                        path.insert(0, nxt)
                if loop:
                    break
            if loop:
                continue
            e0, e1 = path[0], path[-1]
            if len(g.nbrs[e0]) == 1 or len(g.nbrs[e1]) == 1:
                continue                  # a spur: a place's way out
            if e1 < e0:
                path.reverse()
            out.append(path)
    out.sort()
    return out


def areas(level, lim=TURN_IN_PLACE):
    '''The no-stop areas of a nav graph level (`vertices: [x, y, params]`,
    `lanes: [a, b, params]`, directed as the fleet loads them), in a stable
    order: dicts with `kind`, `name` and the kind's vertices — `vertex`
    (junction, turn: with `bend`, rad), `place` and `path` (way_out),
    `corridor`, `end` and `lane` (mouth).'''
    g = _Graph(level)
    out = []
    for v in sorted(g.nbrs):
        if len(g.nbrs[v]) >= 3:
            out.append({'kind': 'junction', 'vertex': v, 'name': g.name(v)})
    for v, bend in sorted(turning_vertices(g, lim).items()):
        out.append({'kind': 'turn', 'vertex': v, 'name': g.name(v),
                    'bend': round(bend, 4)})
    for p in places(g):
        path = oneway_cut(g, way_out(g, p))
        if len(path) > 1:
            out.append({'kind': 'way_out', 'place': p, 'path': path,
                        'name': g.name(p)})
    for c in corridors(g):
        for end, inner in ((c[0], c[1]), (c[-1], c[-2])):
            out.append({'kind': 'mouth', 'corridor': c, 'end': end,
                        'lane': [inner, end],
                        'name': 'corridor %s-%s at %s' % (
                            g.name(c[0]), g.name(c[-1]), g.name(end))})
    return out


def canonical(marks):
    '''What two readings of the same graph must agree on: each area's kind
    and vertices (names and bends are what it is called, not what it
    is).'''
    out = []
    for a in marks or ():
        k = a.get('kind')
        if k in ('junction', 'turn'):
            out.append((k, int(a['vertex'])))
        elif k == 'way_out':
            out.append((k, int(a['place']), tuple(int(v) for v in a['path'])))
        elif k == 'mouth':
            out.append((k, tuple(int(v) for v in a['corridor']),
                        int(a['end']), tuple(int(v) for v in a['lane'])))
        else:
            out.append(('unknown', repr(k)))
    return sorted(out, key=repr)


def differences(level, lim=TURN_IN_PLACE):
    '''The ways a level's carried marks (`KEY`) differ from the areas read
    from its own lanes now — [] when it carries none or they agree.'''
    if KEY not in level:
        return []
    mine = set(canonical(areas(level, lim)))
    carried = set(canonical(level.get(KEY)))
    out = ['carried, not read: %r' % (x,) for x in sorted(carried - mine,
                                                          key=repr)]
    out += ['read, not carried: %r' % (x,) for x in sorted(mine - carried,
                                                           key=repr)]
    return out


def mark_graph(graph, lim=TURN_IN_PLACE):
    '''Every level of a nav graph document given its no-stop areas, in
    place; returns how many areas were marked.'''
    n = 0
    for level in (graph.get('levels') or {}).values():
        level[KEY] = areas(level, lim)
        n += len(level[KEY])
    return n
