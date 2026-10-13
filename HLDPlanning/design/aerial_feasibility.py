# -*- coding: utf-8 -*-
"""Aerial feasibility — the shared answer to "can this drop be buried?".

The legacy stage's ``trench_layer._eval_drop_feasibility`` and the designer's
``_split_drop_legs`` both need this decision, so the **rules** live here once,
free of QGIS and GDAL. Geometry is the caller's job: measuring a distance,
testing a crossing and reading a terrain tag all differ between the QGIS stage
and the standalone designer, so each caller works out its facts and this module
applies the same table to them.

Reasons (unchanged from the original scorer):

    ``spare_duct_available``  buried preferred — brownfield duct exists
    ``distance_threshold``    too far from the network for economical UG
    ``prohibited_crossing``   a barrier forbids the crossing
    ``major_road_crossing``   major carriageway — expensive restoration
    ``terrain_constraint``    rocky / waterlogged / protected ground
    ``ug_default``            buried is fine

The docx's **drop decision chain** is applied by the same callers and named
here too (``crossed_carriageway`` / ``drop_chain_reason``):

    ``setback``               the customer sits further from the street than
                              ``SETBACK_TRIGGER_M`` (20 m)
    ``road_crossing``         the drop has to cross a public carriageway
    ``driveway_crossing``     … a driveway / service aisle / field track
    ``existing_pole``         an existing telecom pole is within
                              ``EXISTING_POLE_TRIGGER_M`` (50 m)
    ``trench_length``         the new trenching is longer than the urban /
                              rural limit (100 m / 300 m)
    ``low_density``           fewer than ``DENSITY_MIN_HOMES_PER_KM`` homes per
                              km of street at the premise
    ``permit_delay``          the permit is delayed beyond
                              ``PERMIT_DELAY_TRIGGER_DAYS`` (60 days)

``no_duct_no_pole_ug_required`` is carried in the original docstring but never
returned by its body; it is not invented here either.

The docx also offers a **point model** as the alternative to the IF-chain
("Instead of many IF conditions, assign points"), and that lives here too:
``drop_score`` / ``decision_for_score`` apply its 12 weights and its three bands,
with every fact optional and every unmeasured fact named in ``notes`` rather
than read as a zero.

No ML — this is the deterministic scoring model the AI reroute pass (A21) will
one day put learned weights on top of.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

# Roads too expensive to open-cut across: restoration, traffic management and
# reinstatement dominate the cost of a drop that crosses one.
AERIAL_BARRIER_CLASSES = frozenset({
    "motorway", "trunk", "primary", "secondary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
})

# Ground a buried drop should not be forced through.
AERIAL_TERRAIN_TYPES = frozenset({
    "rock", "rocky", "scree", "boulder",
    "water", "wetland", "marsh", "swamp", "flood",
    "canal", "ditch",
})

# Default max economical UG drop distance (metres). Rural projects can raise
# it; the designer exposes the same knob as ``Params.max_ug_drop_m``.
MAX_UG_DROP_M_DEFAULT = 300.0

# ─────────────────────────────────────────────────────────────────────────────
# docs/aerial planning.docx — the DROP decision chain
# ─────────────────────────────────────────────────────────────────────────────
#
# The docx gives the drop decision as an ordered chain, and names its switches
# in the printer's own YAML block:
#
#     IF existing drop duct available   -> UG Drop
#     ELSE IF customer setback > 20 m   -> Evaluate Aerial
#     ELSE IF road crossing required    -> Aerial
#     ELSE IF driveway cutting required -> Aerial
#     ELSE IF existing telecom pole within 50 m -> Aerial
#     ELSE                              -> UG Drop
#
#     road_crossing_trigger: true
#     driveway_crossing_trigger: true
#
# The two crossing triggers are what this module supplies. They are not the
# same rule as ``major_road_crossing`` above: that one is the *barrier* set
# (motorway/trunk/primary/secondary — restoration cost and permits), while
# these fire on ANY carriageway, because a drop that has to reach the far side
# of a street needs a road opening, an HDD and reinstatement for a single
# premise. The docx's Scenario 2 states the outcome exactly:
#
#     Instead of: UG road crossing     Use: Pole -> Pole -> House
#     Result: Drop Aerial
#
# ``customer setback``, ``existing telecom pole`` and the permit-delay trigger
# stay unimplemented here: the designer has no setback, pole-route or permit
# facts at the drop stage (see docs/stages/AERIAL.md §5).

# Classes the drop CROSSES without that counting as a road crossing: they are
# the pavement the leg is expected to follow — a footway crossing is not a road
# crossing, it is the normal way to reach a house.
CROSSING_SKIP_CLASSES = frozenset({
    "footway", "path", "pedestrian", "cycleway", "steps", "bridleway",
    "sidewalk",
})

# Driveways, parking aisles and field tracks: the docx's separate
# ``driveway_crossing_trigger``. Cutting one is cheap compared with opening a
# road, so it is its own reason rather than being folded into road_crossing.
DRIVEWAY_CROSSING_CLASSES = frozenset({"service", "track"})


def crossing_reason(fclass: object) -> str:
    """``""``, ``"road_crossing"`` or ``"driveway_crossing"`` for one class.

    Empty means "not a crossing": either a pavement class or an unusable tag.
    """
    cls = str(fclass or "").strip().lower()
    if not cls or cls in CROSSING_SKIP_CLASSES:
        return ""
    return ("driveway_crossing" if cls in DRIVEWAY_CROSSING_CLASSES
            else "road_crossing")


def _crossing_hits(line: Sequence[Tuple[float, float]], road_parts,
                   road: bool = True, driveway: bool = True
                   ) -> list:
    """Every ``(class, reason)`` ``line`` crosses, in read order.

    One scanner behind both public functions: a disabled category
    (``road``/``driveway``, the designer's ``Params.aerial_road_crossing`` /
    ``aerial_driveway_crossing``) is not even reported, so a leg that crosses a
    driveway AND a street is still classified on the driveway when only that
    trigger is on.
    """
    hits = []
    if len(line) < 2:
        return hits
    for part in road_parts or ():
        coords, fclass = part[0], part[1]
        reason = crossing_reason(fclass)
        if not reason:
            continue
        if reason == "road_crossing" and not road:
            continue
        if reason == "driveway_crossing" and not driveway:
            continue
        if polyline_crosses(line, coords):
            hits.append((str(fclass).strip().lower(), reason))
    return hits


def crossed_carriageway(line: Sequence[Tuple[float, float]], road_parts,
                        road: bool = True, driveway: bool = True
                        ) -> Optional[Tuple[str, str]]:
    """``(class, reason)`` for the first carriageway ``line`` crosses, else None.

    A public carriageway outranks a driveway when a leg crosses both, so the
    log names the expensive obstacle rather than whichever part happened to be
    read first.
    """
    best = None
    for cls, reason in _crossing_hits(line, road_parts, road, driveway):
        if best is None or (best[1] == "driveway_crossing"
                            and reason == "road_crossing"):
            best = (cls, reason)
    return best


def crossing_flags(line: Sequence[Tuple[float, float]], road_parts,
                   road: bool = True, driveway: bool = True) -> Tuple[bool, bool]:
    """``(crosses_a_road, crosses_a_driveway)`` for one drop leg."""
    hits = _crossing_hits(line, road_parts, road, driveway)
    return (any(r == "road_crossing" for _c, r in hits),
            any(r == "driveway_crossing" for _c, r in hits))

# ─────────────────────────────────────────────────────────────────────────────
# Which OSM landuse classes are grounds for "do not dig here"
# ─────────────────────────────────────────────────────────────────────────────
#
# This is the ONE list that decides it, and both entry points read it:
# ``trench_design._read_polygons_geom`` (the pipeline path) and
# ``web/backend/design.derive_aerial_zones`` (the standalone designer). It
# used to live only in the second, so the pipeline handed the designer the
# whole landuse layer and the designer took every polygon in it as a
# no-excavation zone.
#
# That is not a subtle over-count. In a residential ward the landuse layer is
# mostly `residential`, `retail` and `commercial` — the ground the network
# exists to serve — so on run ab8399fcc (North Edgbaston, 2 620 premises) the
# mask blanketed the AOI and 2 040 of 2 057 drop legs were classified aerial
# for `zone`. Restricted land is a small minority of a ward, so the filter has
# to be an allow-list; a deny-list would eventually include the suburbs.
#
# Roadside classes (grass, scrub, garden, recreation_ground) are deliberately
# absent: in OSM they are mostly verges, and including them blankets the whole
# AOI for the same reason residential did.
RESTRICTED_LANDUSE = frozenset({
    "park", "forest", "nature_reserve", "meadow", "wetland", "wood",
    "farmland", "farm", "orchard", "vineyard", "cemetery", "allotments",
})

# Zones below this are dropped as slivers/mapping noise rather than treated as
# grounds for aerial construction.
MIN_ZONE_AREA_M2 = 2000.0


def is_restricted_landuse(fclass: object) -> bool:
    """Is this OSM ``fclass`` grounds for a no-excavation zone?

    Accepts the raw field value so a caller can pass what OGR handed back
    without pre-cleaning it. An absent/blank/unknown class is never
    restricted — the safe direction is to allow digging, not to invent a zone.
    """
    return str(fclass or "").strip().lower() in RESTRICTED_LANDUSE


def evaluate_drop_feasibility(*,
                              distance_m: Optional[float] = None,
                              road_class: Optional[str] = None,
                              terrain: Optional[str] = None,
                              has_spare_duct: bool = False,
                              crosses_barrier: bool = False,
                              max_ug_drop_m: float = MAX_UG_DROP_M_DEFAULT
                              ) -> Tuple[bool, str]:
    """(buried_ok, reason) for one drop, from facts the caller measured.

    ``distance_m`` is the house-to-network distance, or ``None`` when the
    caller has no network to measure against (the rule is then skipped rather
    than guessed).
    """
    # 1. Existing spare duct → buried preferred
    if has_spare_duct:
        return (True, "spare_duct_available")

    # 2. Distance to the nearest network point
    if distance_m is not None and distance_m > max_ug_drop_m:
        return (False, "distance_threshold")

    # 3. Barrier crossing
    if crosses_barrier:
        return (False, "prohibited_crossing")

    # 4. Major road type → expensive restoration
    if road_class and str(road_class).lower() in AERIAL_BARRIER_CLASSES:
        return (False, "major_road_crossing")

    # 5. Terrain constraint
    if terrain and str(terrain).lower() in AERIAL_TERRAIN_TYPES:
        return (False, "terrain_constraint")

    return (True, "ug_default")


# ─────────────────────────────────────────────────────────────────────────────
# The rest of docs/aerial planning.docx — the drop decision chain
# ─────────────────────────────────────────────────────────────────────────────
# The docx prints the chain in order, one switch per step, and hands the drop
# back to underground when nothing fires:
#
#     IF existing drop duct available      -> UG Drop
#     ELSE IF customer setback > 20 m      -> Evaluate Aerial
#     ELSE IF road crossing required       -> Aerial
#     ELSE IF driveway cutting required    -> Aerial
#     ELSE IF existing telecom pole < 50 m -> Aerial
#     ELSE                                 -> UG Drop
#
# ``drop_chain_reason`` applies that order to facts the caller measured. It
# also carries the business triggers §4 lists beside the chain — they are
# aerial grounds in their own right, and the drop stage is the only place they
# can be applied: new trenching over the urban/rural limit, a customer density
# under the threshold, and a permit delayed past the limit.
#
# ``spare_duct_available`` is the one step this module cannot apply: the drop
# stage has no duct registry (see ``trench_design._drop_feasibility_reason``),
# so the scorer owns it and the chain starts at the setback.
SETBACK_TRIGGER_M = 20.0
EXISTING_POLE_TRIGGER_M = 50.0
TRENCH_LENGTH_URBAN_M = 100.0
TRENCH_LENGTH_RURAL_M = 300.0
DENSITY_MIN_HOMES_PER_KM = 20.0
PERMIT_DELAY_TRIGGER_DAYS = 60.0

# The field spellings a permit-delay register may state its lead time under,
# read in order so a layer shipping both ``days`` and ``lead_time_days`` states
# which one it means. It lives here, with the trigger it feeds, because two
# callers read it: the designer's ``_read_permit_delays`` and the stage's
# ``_dump_permit_delays``, which normalises the register to one field.
PERMIT_DELAY_FIELDS = ("lead_time_days", "permit_delay_days", "lead_days",
                       "permit_days", "days")

# Radius the density is measured over: the customer's own street end, not the
# ward. A dead-end with 5 homes on 400 m of street is 12.5 homes/km.
DENSITY_RADIUS_M = 250.0


def point_segment_m(point, a, b) -> float:
    """Distance from a point to segment ``a``-``b`` (projected metres)."""
    px, py = float(point[0]), float(point[1])
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + dx * t), py - (ay + dy * t))


def nearest_street_m(point, road_parts) -> Optional[float]:
    """The customer's setback: distance to the nearest carriageway.

    ``None`` when the caller supplied no road geometry at all — absent evidence
    is not a measured 0 m, and the chain treats the two differently.
    """
    best: Optional[float] = None
    for part in road_parts or ():
        coords = part[0]
        for a, b in zip(coords, coords[1:]):
            d = point_segment_m(point, a, b)
            if best is None or d < best:
                best = d
    return best


def _segment_circle_length(a, b, centre, radius: float) -> float:
    """Length of segment ``a``-``b`` that lies inside the circle (0 if none)."""
    ax, ay = float(a[0]) - float(centre[0]), float(a[1]) - float(centre[1])
    bx, by = float(b[0]) - float(centre[0]), float(b[1]) - float(centre[1])
    dx, dy = bx - ax, by - ay
    seg = math.hypot(dx, dy)
    if seg <= 0.0:
        return 0.0
    qa = dx * dx + dy * dy
    qb = 2.0 * (ax * dx + ay * dy)
    qc = ax * ax + ay * ay - radius * radius
    disc = qb * qb - 4.0 * qa * qc
    if disc <= 0.0:
        return 0.0
    root = math.sqrt(disc)
    lo = max(0.0, min((-qb - root) / (2.0 * qa), (-qb + root) / (2.0 * qa)))
    hi = min(1.0, max((-qb - root) / (2.0 * qa), (-qb + root) / (2.0 * qa)))
    return max(0.0, hi - lo) * seg


def street_length_within_m(point, road_parts, radius_m: float) -> float:
    """Total carriageway length inside a circle — the density denominator."""
    total = 0.0
    for part in road_parts or ():
        coords = part[0]
        for a, b in zip(coords, coords[1:]):
            total += _segment_circle_length(a, b, point, radius_m)
    return total


def density_per_km(point, homes, road_parts,
                   radius_m: float = DENSITY_RADIUS_M) -> Optional[float]:
    """Homes per km of street around ``point``, or ``None`` with no street.

    ``homes`` are the caller's own premise points (mappings with ``x``/``y``),
    so the metric is the same one the docx reasons about: how many customers
    hang off the street the drop serves. A dead-end with five homes on 400 m of
    street is 12.5 homes/km, i.e. below the threshold.
    """
    length_km = street_length_within_m(point, road_parts, radius_m) / 1000.0
    if length_km <= 0.0:
        return None
    px, py = float(point[0]), float(point[1])
    near = 0
    for home in homes or ():
        try:
            hx, hy = float(home.get("x")), float(home.get("y"))
        except (TypeError, ValueError):
            continue
        if math.hypot(hx - px, hy - py) <= radius_m:
            near += 1
    return near / length_km


def drop_chain_reason(*, setback_m: Optional[float] = None,
                      road_crossing: bool = False,
                      driveway_crossing: bool = False,
                      nearest_pole_m: Optional[float] = None,
                      trench_m: Optional[float] = None,
                      urban: bool = True,
                      density_km: Optional[float] = None,
                      permit_delay_days: Optional[float] = None,
                      setback_trigger: bool = True,
                      pole_trigger: bool = True,
                      trench_trigger: bool = False,
                      density_trigger: bool = False,
                      permit_trigger: bool = False,
                      setback_limit_m: float = SETBACK_TRIGGER_M,
                      pole_limit_m: float = EXISTING_POLE_TRIGGER_M,
                      trench_urban_m: float = TRENCH_LENGTH_URBAN_M,
                      trench_rural_m: float = TRENCH_LENGTH_RURAL_M,
                      density_min: float = DENSITY_MIN_HOMES_PER_KM,
                      permit_limit_days: float = PERMIT_DELAY_TRIGGER_DAYS
                      ) -> str:
    """The docx's reason for building this drop aerial, or ``""`` for buried.

    The crossing flags arrive already switch-gated (``crossing_flags``), so the
    chain does not re-apply those two switches; a public road is reported ahead
    of a driveway because it is the expensive obstacle. A step whose fact is
    ``None`` was not measured and is skipped rather than treated as zero: an
    unreadable pole layer must not read as "no pole within 50 m".
    """
    if setback_trigger and setback_m is not None and setback_m > setback_limit_m:
        return "setback"
    if road_crossing:
        return "road_crossing"
    if driveway_crossing:
        return "driveway_crossing"
    if pole_trigger and nearest_pole_m is not None and nearest_pole_m <= pole_limit_m:
        return "existing_pole"
    if trench_trigger and trench_m is not None:
        limit = trench_urban_m if urban else trench_rural_m
        if trench_m > limit:
            return "trench_length"
    if (density_trigger and density_km is not None
            and density_km < density_min):
        return "low_density"
    if (permit_trigger and permit_delay_days is not None
            and permit_delay_days > permit_limit_days):
        return "permit_delay"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# The docx's OTHER decision: "Scoring Model (Best for Automation)"
# ─────────────────────────────────────────────────────────────────────────────
#
# The docx offers this beside the IF-chain, and says what it is for:
#
#     Instead of many IF conditions, assign points.
#
#     No duct available             +30
#     Existing poles                +25
#     Rocky terrain                 +20
#     Concrete road                 +15
#     NH crossing                   +20
#     Low customer density          +15
#     Permit delay >60 days         +15
#     Flood area                    +10
#     Spare duct available          -50
#     Metro city core               -30
#     High customer density         -20
#     Pole loading >80%             -50
#
#     Score < 20   -> UG
#     Score 20-50  -> Cost comparison
#     Score > 50   -> Aerial
#
# Points, not switches: this is the model for a project that wants the facts to
# ADD UP instead of letting the first one that fires decide. The chain ships
# ``low_density`` / ``trench_length`` / ``permit_delay`` OFF because each of them
# alone put most of a ward overhead; with points they act on the total instead,
# which is what makes them usable (see docs/stages/AERIAL.md §5).
#
# Two deliberate readings of the docx:
#
# * **It replaces the CHAIN, not the physical constraints.** ``zone`` (a
#   non-diggable area), ``unreachable`` and ``distance_threshold`` stay hard
#   rules in both models: they are not cost preferences — underground is not
#   permitted there, or the network does not reach. The scorer's crossing and
#   terrain rules DO become points (that is what "Rocky terrain +20", "Flood
#   area +10" and "NH crossing +20" are), because the docx lists those same
#   facts as points here and as conditions there.
# * **A fact nobody measured scores nothing, and says so.** Every fact is
#   optional; ``None`` means "not measured" and lands in ``notes``. It is never
#   read as false, so an unreadable pole layer cannot silently cost a drop 25
#   points — the same rule the chain applies to a ``None`` fact.
#
# The middle band is the docx's "Cost comparison", which the drop stage cannot
# perform (it holds no aerial-vs-UG unit rates). It is reported by name and left
# underground; ``Params.aerial_score_compare_aerial`` builds it aerial instead
# when a project has done that comparison itself.

# The docx's point table, verbatim. Kept as (fact, points) pairs so the order a
# breakdown is printed in is the order the docx writes it in.
SCORE_WEIGHTS: Tuple[Tuple[str, float], ...] = (
    ("no_duct", 30.0),
    ("existing_poles", 25.0),
    ("rocky_terrain", 20.0),
    ("concrete_road", 15.0),
    ("nh_crossing", 20.0),
    ("low_density", 15.0),
    ("permit_delay", 15.0),
    ("flood_area", 10.0),
    ("spare_duct", -50.0),
    ("metro_core", -30.0),
    ("high_density", -20.0),
    ("pole_loading_high", -50.0),
)

SCORE_BY_FACT: Dict[str, float] = dict(SCORE_WEIGHTS)

# The bands, from the docx's decision block.
SCORE_UG_BELOW = 20.0
SCORE_AERIAL_ABOVE = 50.0

# Terrain splits the scorer treats as one thing (``terrain_constraint``) but the
# score prices separately: rock is +20, water +10.
SCORE_ROCKY_TYPES = frozenset({"rock", "rocky", "scree", "boulder"})
SCORE_FLOOD_TYPES = frozenset({
    "water", "wetland", "marsh", "swamp", "flood", "canal", "ditch",
})

# A national highway is the road class the docx names as its own point line;
# ``AERIAL_BARRIER_CLASSES`` is wider (it also carries primary/secondary, whose
# restoration the chain prices as a plain crossing).
NH_CLASSES = frozenset({"motorway", "motorway_link", "trunk", "trunk_link"})

# Road surfaces the docx's "Concrete road +15" means. Kept to the surface
# values OSM actually writes for a concrete carriageway.
CONCRETE_SURFACES = frozenset({"concrete", "concrete:plates", "concrete:lanes"})

# The docx's "Pole loading >80%", the same limit the aerial stage enforces.
POLE_LOADING_LIMIT = 0.80


class DropScore(NamedTuple):
    """One drop's score, why, and what could not be judged.

    ``points`` maps each firing fact to its points (in the docx's own order);
    ``notes`` names every fact that did not score and why, so a report can say
    "low_density: not measured" rather than leaving a silent zero.
    """

    score: float
    points: Dict[str, float]
    notes: List[str]
    decision: str  # "ug" | "compare" | "aerial"

    @property
    def firing(self) -> List[str]:
        """The facts that scored, strongest first (ties keep the docx's order)."""
        order = {fact: i for i, (fact, _p) in enumerate(SCORE_WEIGHTS)}
        return sorted(self.points,
                      key=lambda f: (-abs(self.points[f]), order.get(f, 99)))


def score_terrain(terrain: object) -> str:
    """``"rocky_terrain"`` / ``"flood_area"`` / ``""`` for a terrain tag."""
    val = str(terrain or "").strip().lower()
    if val in SCORE_ROCKY_TYPES:
        return "rocky_terrain"
    if val in SCORE_FLOOD_TYPES:
        return "flood_area"
    return ""


def is_concrete_surface(surface: object) -> bool:
    """Is this OSM ``surface`` a concrete carriageway?"""
    return str(surface or "").strip().lower() in CONCRETE_SURFACES


def is_nh_class(fclass: object) -> bool:
    """Is this road class a national highway (the docx's ``NH crossing``)?"""
    return str(fclass or "").strip().lower() in NH_CLASSES


def decision_for_score(score: float, compare_aerial: bool = False) -> str:
    """``"ug"`` / ``"aerial"`` for a score, per the docx's bands.

    The middle band is the docx's "Cost comparison": ``compare_aerial`` says
    the project has done that comparison and wants the band built aerial; the
    default leaves it underground, because the drop stage cannot price it.
    """
    if score > SCORE_AERIAL_ABOVE:
        return "aerial"
    if score >= SCORE_UG_BELOW:
        return "aerial" if compare_aerial else "ug"
    return "ug"


def drop_score(*, spare_duct: Optional[bool] = None,
               existing_poles: Optional[bool] = None,
               terrain: Optional[str] = None,
               concrete_road: Optional[bool] = None,
               crosses_nh: Optional[bool] = None,
               density_km: Optional[float] = None,
               permit_delay_days: Optional[float] = None,
               metro_core: Optional[bool] = None,
               pole_loading_pct: Optional[float] = None,
               density_low_min: float = DENSITY_MIN_HOMES_PER_KM,
               density_high_min: Optional[float] = None,
               permit_limit_days: float = PERMIT_DELAY_TRIGGER_DAYS,
               pole_loading_limit: float = POLE_LOADING_LIMIT,
               compare_aerial: bool = False) -> DropScore:
    """The docx's point score for one drop, from facts the caller measured.

    Every fact is optional and ``None`` means "not measured": it scores nothing
    and is named in ``notes``. Only two of the docx's lines need a threshold the
    docx does not state — ``high_density`` (what counts as high) and a density
    that was never measured — and both stay un-awarded and reported rather than
    being guessed at, because a constant invented here would change designs.

    ``spare_duct`` is the one fact the drop stage cannot supply (it holds no
    duct registry); a caller that has one — the legacy stage's brownfield
    registry — passes it, and the drop stage passes ``None``, which is why the
    chain leaves that step to the scorer too.
    """
    points: Dict[str, float] = {}
    notes: List[str] = []

    def _award(fact: str) -> None:
        points[fact] = SCORE_BY_FACT[fact]

    # ── the duct line (one of the two, or neither) ──────────────────────────
    if spare_duct is None:
        notes.append("duct: not measured (no duct registry at this stage)")
    elif spare_duct:
        _award("spare_duct")
        notes.append("duct: spare way available - buried preferred")
    else:
        _award("no_duct")

    # ── the pole line ───────────────────────────────────────────────────────
    if existing_poles is None:
        notes.append("pole route: not measured (no pole inventory supplied)")
    elif existing_poles:
        _award("existing_poles")
    else:
        notes.append("pole route: no existing pole available (a mandatory "
                     "condition, so nothing scores here)")
    if pole_loading_pct is None:
        notes.append("pole loading: not measured")
    elif pole_loading_pct >= pole_loading_limit * 100.0:
        _award("pole_loading_high")

    # ── ground ─────────────────────────────────────────────────────────────
    if terrain is None or not str(terrain).strip():
        notes.append("terrain: not measured")
    else:
        fact = score_terrain(terrain)
        if fact:
            _award(fact)
        else:
            notes.append("terrain: %s is not rocky or flood" % terrain)

    # ── the carriageway ─────────────────────────────────────────────────────
    if crosses_nh is None:
        notes.append("NH crossing: not measured")
    elif crosses_nh:
        _award("nh_crossing")
    if concrete_road is None:
        notes.append("concrete road: not measured")
    elif concrete_road:
        _award("concrete_road")

    # ── demand ──────────────────────────────────────────────────────────────
    if density_km is None:
        notes.append("customer density: not measured")
    elif density_km < density_low_min:
        _award("low_density")
    elif density_high_min is None:
        notes.append("customer density: %.1f homes/km - above the low "
                     "threshold but no high threshold is set, so neither "
                     "line scores" % density_km)
    elif density_km >= density_high_min:
        _award("high_density")
    else:
        notes.append("customer density: %.1f homes/km - between the low and "
                     "high thresholds, so neither line scores" % density_km)

    # ── permits ─────────────────────────────────────────────────────────────
    if permit_delay_days is None:
        notes.append("permit delay: not measured (no permit lead time "
                     "supplied)")
    elif permit_delay_days > permit_limit_days:
        _award("permit_delay")

    # ── the declared project fact ───────────────────────────────────────────
    if metro_core is None:
        notes.append("metro city core: not declared")
    elif metro_core:
        _award("metro_core")

    score = round(sum(points.values()), 2)
    return DropScore(score=score, points=points, notes=notes,
                     decision=decision_for_score(score, compare_aerial))


# ── Planar geometry the callers share (the CRS is projected metres) ─────────

def segments_intersect(p1, p2, p3, p4) -> bool:
    """True when segment p1-p2 crosses segment p3-p4 (touching counts)."""

    def _orient(a, b, c) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def _on_segment(a, b, c) -> bool:
        return (min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9
                and min(a[1], b[1]) - 1e-9 <= c[1] <= max(a[1], b[1]) + 1e-9)

    o1 = _orient(p1, p2, p3)
    o2 = _orient(p1, p2, p4)
    o3 = _orient(p3, p4, p1)
    o4 = _orient(p3, p4, p2)

    if ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)) and o1 != 0 and o2 != 0 and o3 != 0 and o4 != 0:
        return True
    if o1 == 0 and _on_segment(p1, p2, p3):
        return True
    if o2 == 0 and _on_segment(p1, p2, p4):
        return True
    if o3 == 0 and _on_segment(p3, p4, p1):
        return True
    if o4 == 0 and _on_segment(p3, p4, p2):
        return True
    return False


def polyline_crosses(line: Sequence[Tuple[float, float]],
                     other: Sequence[Tuple[float, float]]) -> bool:
    if len(line) < 2 or len(other) < 2:
        return False
    for i in range(len(line) - 1):
        for j in range(len(other) - 1):
            if segments_intersect(line[i], line[i + 1], other[j], other[j + 1]):
                return True
    return False


def crossed_road_class(line: Sequence[Tuple[float, float]],
                       road_parts: Iterable[Sequence],
                       classes: Iterable[str] = AERIAL_BARRIER_CLASSES
                       ) -> Optional[str]:
    """The first barrier-class road ``line`` crosses, or None.

    A drop crossing a major carriageway is the case the scorer calls
    ``major_road_crossing``; a road it merely runs alongside is not a crossing.
    ``road_parts`` items are ``(coords, fclass)`` pairs — or
    ``(coords, fclass, tags)`` triples as ``trench_design._read_road_parts``
    emits them; the extra element is ignored.
    """
    wanted = {c.lower() for c in classes}
    for part in road_parts:
        coords, fclass = part[0], part[1]
        if str(fclass or "").strip().lower() not in wanted:
            continue
        if polyline_crosses(line, coords):
            return str(fclass).strip().lower()
    return None
