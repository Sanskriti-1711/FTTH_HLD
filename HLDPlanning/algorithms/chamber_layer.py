# -*- coding: utf-8 -*-
"""
Chamber Layer — generate planned civil chambers for the HLD.

Implements the 8-rule chamber placement logic:

  1. Splitter locations            → Chamber at every PDP (FAT/FDT/FDH/PFP point)
  2. Branching points              → Manhole/Handhole where >= 3 distinct ducts
                                     converge (feeder / distribution junction)
  3. Feeder→Distribution transition→ covered by the PDP Chamber (feeder and
                                     distribution meet there)
  4. Distribution→Drop transition  → Handhole at every distribution-duct
                                     endpoint that connects to a drop duct
  5. Cable joint locations         → covered by junction chambers (rule 2)
  6. Direction changes             → Manhole/Handhole at duct bends > 45°
  7. Road crossings / HDD pits     → DHH at BOTH ends (entry + exit) of every drill crossing
                                     These are placed FIRST (before every other rule)
                                     and reserve the widest keep-out (HDD_PIT_SPACING_M),
                                     so the drill openings always get their chamber and
                                     no other structure is stacked next to them.
  8. Long straight routes          → intermediate pull Manholes/Handholes
                                     every ~250 m along long duct runs

Types follow the HLD_attr.docx Simple Rule:
    Manhole   → Feeder network            (large duct banks, backbone access)
    Chamber   → Feeder + Distribution     (splicing, branching, cable pulling)
    Handhole  → Distribution + Garden     (FAT access, garden cable connections)

Candidates within their rule-specific spacing are collapsed
(Chamber > Manhole > Handhole, densest junction first).

Separation rules (HLD review):
  • HDD pits first — placed before all other rules, widest keep-out.
  • Every pair of placed structures keeps at least
    MIN_STRUCTURE_SEPARATION_M so no two chambers sit real close by.
  • A splitter location (PDP) that falls inside an HDD pit's keep-out is
    MERGED into that pit (the pit becomes the PDP/F2D chamber and records
    the PDP id) instead of stacking a second chamber next to it.
"""
import math
from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterFeatureSink,
    QgsProcessingException, QgsWkbTypes, QgsFeature, QgsFeatureSink,
    QgsGeometry, QgsPointXY, QgsRectangle, QgsSpatialIndex,
    QgsVectorLayer,
)

from ..utils.fields import COMMON_FIELDS, THIN_PROFILES, build_fields
from ..utils.brownfield import InfraStatus, VerifyStatus


class ChamberLayerAlgorithm(QgsProcessingAlgorithm):

    P_FEEDER_DUCTS = "INPUT_FEEDER_DUCTS"
    P_DIST_DUCTS = "INPUT_DIST_DUCTS"
    P_DROP_DUCTS = "INPUT_DROP_DUCTS"   # drop ducts: distribution→drop transitions
    P_PDP = "INPUT_PDP"
    P_TANGENTS = "INPUT_TANGENT_CROSSINGS"
    P_TRENCHES = "INPUT_TRENCHES"
    P_AOI = "INPUT_AOI"                 # design boundary (dissolved AOI polygon)
    P_BUILDINGS = "INPUT_BUILDINGS"     # building footprints (chamber exclusion)
    OUT_CHAMBERS = "OUT_CHAMBERS"

    JUNCTION_RADIUS_M = 1.5     # how close two DISTINCT ducts must pass to count as a junction
    JUNCTION_MIN_DUCTS = 3      # distinct ducts required for a junction chamber (rule 2)
    JUNCTION_SPACING_M = 40.0   # min spacing between junction-derived Manholes/Handholes
    # Rule 9 — chambers where TRENCHES meet. Rule 2 only sees ducts, so a
    # junction where the trench network branches but fewer than three distinct
    # ducts pass (an HDD crossing meeting the open cut, a garden leg meeting the
    # mains) used to get no civil structure at all.
    TRENCH_JUNCTION_MIN = 3     # distinct trench runs meeting at one point
    HANDHOLE_SPACING_M = 40.0   # min spacing for distribution-level handholes
    CHAMBER_SPACING_M = 2.0     # legacy default (superseded by the rules below)
    # ── Structure separation (HLD review: no two chambers side by side) ──
    # HDD entry/exit pits are placed FIRST and reserve the widest keep-out;
    # every other structure is then placed respecting a global minimum
    # separation, so two civil structures never end up real close by.
    HDD_PIT_SPACING_M = 15.0        # keep-out around an HDD entry/exit pit
    MIN_STRUCTURE_SEPARATION_M = 10.0  # floor between ANY two planned structures
    MANDATORY_MERGE_M = 20.0        # a splitter location within this of a pit
                                    # merges into it instead of stacking
    BEND_ANGLE_DEG = 60.0       # direction change above this gets a structure (rule 6)
    BEND_SPACING_M = 25.0       # min spacing between bend-derived structures
    BEND_MIN_LEG_M = 3.0        # both legs of the angle must be at least this long (snap zigzags)
    DROP_TRANSITION_SPACING_M = 30.0  # min spacing between distribution→drop handholes
    DROP_TRANSITION_MIN_DROPS = 2     # >= drop ducts tapping within CONN_RADIUS_M
    PULL_SPACING_M = 250.0      # intermediate pull structures along long runs (rule 8)
    PULL_MIN_RUN_M = 500.0      # only runs longer than this get intermediate pulls
    PULL_END_SKIP_M = 50.0      # don't place a pull structure this close to a run end
    PULL_CLEARANCE_M = 40.0     # a prior structure only blocks a pull point this close
    CONN_RADIUS_M = 3.0         # ducts within this radius count as 'connected'
    TRENCH_JOIN_M = 3.0         # parent trench join tolerance
    TRENCH_SNAP_M = 5.0         # max shift to snap a chamber ONTO the trench path

    # HLD review rules: chambers must sit inside the design boundary, and out
    # of buildings EXCEPT high-density PDPs (> PDP_INBUILDING_HH homes), where
    # the PDP/chamber may be installed inside the building to shorten drops.
    INSIDE_TOL_M = 2.0          # 'outside boundary' tolerance (edge slivers)
    SNAP_OUT_MAX_M = 15.0       # max shift to snap a chamber out of a building
    SNAP_OUT_STEP_M = 2.0       # radial search step for the snap-out
    PDP_INBUILDING_HH = 50      # > this many HP → PDP/chamber may stay in-building

    # ── Standard chamber catalogue (HLD review: exactly 3 types) ──
    # code  type                   size             use
    # HH    Handhole               300x300/450x450  Pulling point, route access, micro-trench network
    # DHH   Distribution Handhole  600x600          Splitter installation, distribution splicing, HDD entry/exit
    # MH    Manhole                1200x1200        Feeder splicing, FDH/FDC locations, major network junctions
    CHAMBER_CATALOGUE = {
        "HH":  ("Handhole",              "300x300 / 450x450 mm"),
        "DHH": ("Distribution Handhole", "600x600 mm"),
        "MH":  ("Manhole",               "1200x1200 mm"),
    }
    # Type per placement reason:
    #   PDP (splitter install + F2D) → DHH; HDD entry/exit → DHH;
    #   feeder-side branching junctions → MH; distribution/drop access,
    #   direction changes and pull points → HH.
    REASON_TYPE = {
        "Splitter/F2D (PDP)": "DHH",
        "HDD pit": "DHH",
        "Branching junction": "MH",
        "Drop transition": "HH",
        "Direction change": "HH",
        "Pull point": "HH",
    }

    # Civil sub-category, from the catalogue code:
    #   "Bore"     = the entry/exit opening of an HDD (directional-drill)
    #                crossing — access to the bore, at the drill ends, never
    #                mid-carriageway;
    #   "Manhole"  = MH (1200x1200, walk-in, feeder);
    #   "Handhole" = DHH / HH (hand-access, distribution & drop).
    SUBTYPE_BY_CODE = {"MH": "Manhole", "DHH": "Handhole", "HH": "Handhole"}

    def tr(self, s):
        return QCoreApplication.translate("ChamberLayerAlgorithm", s)

    def name(self):
        return "07_chamber_layer"

    def displayName(self):
        return self.tr("Generate Chamber / Manhole / Handhole Layer")

    def group(self):
        return self.tr("07 Civil")

    def groupId(self):
        return "07_civil"

    def createInstance(self):
        return ChamberLayerAlgorithm()

    def shortHelpString(self):
        return self.tr(
            "Plans civil chambers from the designed network using the 8-rule "
            "logic: Chamber at every PDP (splitter + feeder→distribution "
            "transition), Manholes/Handholes at branching junctions (≥3 "
            "ducts), Handholes at distribution→drop transition points, "
            "Manholes at HDD/drill pits, structures at >45° direction changes, "
            "and intermediate pull chambers every ~250 m on long straight "
            "runs. Implements the HLD_attr.docx Simple Rule (Manhole = Feeder, "
            "Chamber = Feeder+Distribution, Handhole = Distribution+Garden)."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_FEEDER_DUCTS, self.tr("Feeder Ducts [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_DIST_DUCTS, self.tr("Distribution Ducts [lines]"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_DROP_DUCTS, self.tr("Drop Ducts [lines] (optional; distribution→drop transitions)"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_PDP, self.tr("PDP points"),
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_TANGENTS, self.tr("Used drill crossings [points] (optional)"),
            [QgsProcessing.TypeVectorPoint], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_TRENCHES, self.tr("Final Trenches [lines] (optional; for parent id)"),
            [QgsProcessing.TypeVectorLine], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_AOI, self.tr("Design boundary / AOI [polygons] (optional; chambers kept inside)"),
            [QgsProcessing.TypeVectorPolygon], optional=True,
        ))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_BUILDINGS, self.tr("Buildings [polygons] (optional; chambers snapped out)"),
            [QgsProcessing.TypeVectorPolygon], optional=True,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT_CHAMBERS, self.tr("Chambers (planned)"),
            optional=True, createByDefault=True,
        ))

    # ── helpers ──────────────────────────────────────────────────────────

    def _layer(self, params, key, context):
        try:
            return self.parameterAsVectorLayer(params, key, context)
        except Exception:
            return None

    @staticmethod
    def _geom_vertices(g):
        """Yield (x, y) for every vertex of a geometry (single or multi line).

        NOTE: `g.constGet()` returns the abstract QgsMultiLineString /
        QgsLineString — those do NOT have an `isMultipart()` method (that
        lives on QgsGeometry).  We must test the WKB type instead.
        """
        if g is None or g.isEmpty():
            return
        parts = g.constGet()
        try:
            if QgsWkbTypes.isMultiType(parts.wkbType()):
                for part in parts.parts():
                    for i in range(part.numPoints()):
                        p = part.pointN(i)
                        yield p.x(), p.y()
            else:
                for i in range(parts.numPoints()):
                    p = parts.pointN(i)
                    yield p.x(), p.y()
        except Exception:
            return

    def _line_vertices(self, lyr):
        """Yield (x, y) for every vertex of every line feature."""
        if lyr is None:
            return
        for f in lyr.getFeatures():
            for x, y in self._geom_vertices(f.geometry()):
                yield x, y

    def _junction_points(self, lyr, radius):
        """Points where >= 2 DISTINCT ducts pass within `radius`.

        Every duct vertex is a seed.  A seed is a junction only when at least
        two *different* duct features pass within `radius` of it — so a single
        duct's own dense vertices never count.  Returns (x, y, weight) where
        weight = number of distinct ducts at that point (denser = stronger).
        """
        if lyr is None:
            return []
        index = QgsSpatialIndex()
        geoms = {}
        verts = []
        for f in lyr.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            fid = f.id()
            # NOTE: addGeometry() was removed from QgsSpatialIndex in modern
            # QGIS — the only supported way is addFeature(feature).
            index.addFeature(f)
            geoms[fid] = g
            for x, y in self._geom_vertices(g):
                verts.append((x, y, fid))

        r2 = radius * radius
        out = []
        seen = set()
        for x, y, fid in verts:
            rect = QgsRectangle(x - radius, y - radius, x + radius, y + radius)
            hits = index.intersects(rect)
            distinct = set()
            qpt = QgsPointXY(x, y)
            for hfid in hits:
                g = geoms.get(hfid)
                if g is None:
                    continue
                try:
                    sqd = g.closestSegmentWithContext(qpt)[0]
                except Exception:
                    continue
                if sqd <= r2:
                    distinct.add(hfid)
            if len(distinct) >= 2:
                key = (round(x, 1), round(y, 1))
                if key not in seen:
                    seen.add(key)
                    out.append((x, y, len(distinct)))
        return out

    # Rule order mirrors the 8-rule spec: splitter locations first, then
    # branching junctions, HDD pits, drop transitions, direction changes,
    # and finally intermediate pull structures.  A higher-ranked rule always
    # beats a lower one at the same spot, regardless of raw weight. Trench
    # intersections (rule 9) sit with the duct junctions: the duct-based
    # branching rule wins a tie, since it is the stronger evidence.
    RULE_ORDER = {
        # HDD entry/exit pits are placed FIRST (priority 3 = PDP level, weight
        # 1000 beats the PDP's 999) so the drill openings are never dropped and
        # always reserve HDD_PIT_SPACING_M around them. Everything else is then
        # fitted around them.
        "HDD pit": 0,
        "Splitter/F2D (PDP)": 1,
        "Branching junction": 2,
        "Trench intersection": 3,
        "Drop transition": 4,
        "Direction change": 5,
        "Pull point": 6,
    }

    def _rule_rank(self, cand):
        reason = cand[7] if len(cand) > 7 else ""
        return self.RULE_ORDER.get(reason, 9)

    def _place_structures(self, candidates):
        """Greedy placement: highest priority first, densest junctions first.

        Sort key = (type priority, rule order, weight) so that HDD pits and PDP
        chambers (priority 3) are always placed before junctions, bends and
        pull points, and branching junctions are never starved by bend or pull
        candidates whose raw weight (angle degrees, spacing) is larger.

        Candidates carry their own rule-specific spacing as the 7th tuple
        element. The effective keep-out between two structures is the WIDER of
        the two demands (``max(sp_new, sp_placed)``) — so an HDD pit keeps its
        HDD_PIT_SPACING_M even against a candidate that would otherwise accept
        a tighter fit — and a global floor (MIN_STRUCTURE_SEPARATION_M) applies
        to every pair, so no two chambers end up real close by.

        Mandatory splitter locations are never lost: a PDP chamber that falls
        inside a placed HDD pit's keep-out is merged INTO that pit (the pit is
        upgraded into the PDP/F2D chamber and records the PDP id) rather than
        pushing a second structure next to it.

        Pull chambers (rule 8) are the exception: they exist precisely to
        fill long empty stretches every ~250 m, so they must be spaced
        against each other, not suppressed by every nearby PDP / handhole.
        A prior structure only blocks a pull point if it sits right at the
        spot (within PULL_CLEARANCE_M).  Returns the kept list.
        """
        ordered = sorted(
            candidates,
            key=lambda c: (-c[2], self._rule_rank(c), -c[5]),
        )
        kept = []
        index = QgsSpatialIndex()
        merged_splitter = 0   # PDP chambers folded into an HDD pit
        blocked_close = 0     # candidates dropped for sitting too close
        for cand in ordered:
            x, y, prio, ctype, equip, weight = cand[:6]
            reason = cand[7] if len(cand) > 7 else ""
            sp = cand[6] if len(cand) > 6 else self.CHAMBER_SPACING_M
            # Global floor: no two planned structures closer than this.
            sp = max(sp, self.MIN_STRUCTURE_SEPARATION_M)
            if reason == "Pull point":
                # Only blocked by a structure essentially at the same spot;
                # spacing against *other* pull points is their own 250 m.
                cl = max(self.PULL_CLEARANCE_M, self.MIN_STRUCTURE_SEPARATION_M)
                rect = QgsRectangle(x - cl, y - cl, x + cl, y + cl)
            else:
                rect = QgsRectangle(x - sp, y - sp, x + sp, y + sp)
            blocked = False
            for bid in index.intersects(rect):
                # index ids == position in `kept`
                bx, by = kept[bid][0], kept[bid][1]
                bsp = kept[bid][6] if len(kept[bid]) > 6 else self.CHAMBER_SPACING_M
                breason = kept[bid][7] if len(kept[bid]) > 7 else ""
                keepout = max(sp, bsp)
                if (x - bx) ** 2 + (y - by) ** 2 > keepout ** 2:
                    continue  # inside the box, outside the real keep-out
                blocked = True
                # A splitter location must always own a chamber — if the
                # blocking structure is an HDD pit, upgrade the pit into the
                # PDP/F2D chamber instead of stacking two structures here.
                if reason == "Splitter/F2D (PDP)" and breason == "HDD pit" \
                        and (x - bx) ** 2 + (y - by) ** 2 <= self.MANDATORY_MERGE_M ** 2:
                    bk = list(kept[bid])
                    tag = ("PDP:" + str(equip)) if equip else "PDP"
                    bk[4] = (str(bk[4]) + ("; " if bk[4] else "") + tag)
                    kept[bid] = tuple(bk)
                    merged_splitter += 1
                break
            if blocked:
                blocked_close += 1
                continue
            fid = len(kept)
            kept.append(cand)
            feat = QgsFeature()
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            feat.setId(fid)
            index.addFeature(feat)
        self._place_stats = {
            "merged_splitter": merged_splitter,
            "blocked_close": blocked_close,
        }
        return kept

    def _line_parts_xy(self, geom):
        """Yield lists of (x, y) vertices, one list per (multi)line part."""
        if geom is None or geom.isEmpty():
            return
        parts = geom.constGet()
        try:
            if QgsWkbTypes.isMultiType(parts.wkbType()):
                for part in parts.parts():
                    pts = [(part.pointN(i).x(), part.pointN(i).y())
                           for i in range(part.numPoints())]
                    if len(pts) >= 2:
                        yield pts
            else:
                pts = [(parts.pointN(i).x(), parts.pointN(i).y())
                       for i in range(parts.numPoints())]
                if len(pts) >= 2:
                    yield pts
        except Exception:
            return

    def _bend_points(self, lyr, min_deg):
        """Rule 6 — direction changes: vertices where the line turns > min_deg.

        Returns (x, y, turn_degrees). Only interior vertices of each part are
        considered (a 3-point angle). Small zig-zags from snapping are ignored
        by requiring both adjacent segments to be non-trivial in length.
        """
        if lyr is None:
            return []
        out = []
        min_len = 1.0e-6
        for f in lyr.getFeatures():
            g = f.geometry()
            for pts in self._line_parts_xy(g):
                for i in range(1, len(pts) - 1):
                    ax, ay = pts[i - 1]
                    bx, by = pts[i]
                    cx, cy = pts[i + 1]
                    v1x, v1y = bx - ax, by - ay
                    v2x, v2y = cx - bx, cy - by
                    l1 = math.hypot(v1x, v1y)
                    l2 = math.hypot(v2x, v2y)
                    if l1 < min_len or l2 < min_len:
                        continue
                    if l1 < self.BEND_MIN_LEG_M or l2 < self.BEND_MIN_LEG_M:
                        continue  # snap-artefact zigzag, not a real direction change
                    dot = (v1x * v2x + v1y * v2y) / (l1 * l2)
                    dot = max(-1.0, min(1.0, dot))
                    turn = math.degrees(math.acos(dot))
                    if turn > min_deg:
                        out.append((bx, by, round(turn, 1)))
        return out

    def _drop_transition_points(self, dist_lyr, drop_lyr, radius):
        """Rule 4 — distribution→drop transitions.

        A Handhole is needed where a distribution duct terminates and drop
        ducts begin.  For every distribution-duct endpoint, count how many
        DISTINCT drop ducts tap within `radius`; yield (x, y, n_drops). The
        caller gates on n_drops so isolated single drop-taps (served with a
        direct joint) don't spawn handholes everywhere. Endpoints are gathered
        per part so every tap-off point is considered.
        """
        if dist_lyr is None or drop_lyr is None or drop_lyr.featureCount() == 0:
            return []
        # Spatial index of drop ducts
        drop_index = QgsSpatialIndex()
        drop_geoms = {}
        for f in drop_lyr.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            drop_index.addFeature(f)
            drop_geoms[f.id()] = g
        out = []
        seen = set()
        for f in dist_lyr.getFeatures():
            g = f.geometry()
            for pts in self._line_parts_xy(g):
                for (x, y) in (pts[0], pts[-1]):
                    key = (round(x, 1), round(y, 1))
                    if key in seen:
                        continue
                    rect = QgsRectangle(x - radius, y - radius, x + radius, y + radius)
                    qpt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                    ndrops = 0
                    for hfid in drop_index.intersects(rect):
                        dg = drop_geoms.get(hfid)
                        if dg is not None and dg.distance(qpt) <= radius:
                            ndrops += 1
                    seen.add(key)
                    out.append((x, y, ndrops))
        return out

    HDD_SNAP_M = 30.0          # tangent midpoints sit mid-road; allow a wider snap

    def _pull_points(self, lyr, spacing_m, min_run_m, end_skip_m):
        """Rule 8 — intermediate pull structures along long straight runs.

        The duct layers are stored as MultiLineStrings whose parts come out
        of the route union in ARBITRARY order.  Concatenating them blindly
        creates phantom jump segments between disconnected parts and pull
        candidates landed in the middle of nowhere (the 'chambers not on
        the trench' bug).  Now the parts are first chained into connected
        runs (greedy nearest-endpoint continuation) and each run is walked
        independently — candidates only ever sit on real duct geometry.
        Returns (x, y, 0) candidates.
        """
        if lyr is None or spacing_m <= 0:
            return []
        out = []
        seen = set()
        jump_tol = 1.0  # parts whose endpoints are farther apart are separate runs
        for f in lyr.getFeatures():
            g = f.geometry()
            parts = [list(p) for p in self._line_parts_xy(g) if len(p) >= 2]
            if not parts:
                continue
            # ── chain the parts into connected runs ──
            # Greedy: start from the part with the globally lowest x (any
            # deterministic anchor), then repeatedly append the part whose
            # start/end continues the current chain end.
            used = [False] * len(parts)
            runs = []

            def _dist(a, b):
                return math.hypot(a[0] - b[0], a[1] - b[1])

            for _ in range(len(parts)):
                # anchor: first unused part (deterministic)
                try:
                    i0 = used.index(False)
                except ValueError:
                    break
                used[i0] = True
                run = list(parts[i0])
                extended = True
                while extended:
                    extended = False
                    best_j, best_rev, best_d = None, False, jump_tol
                    for j in range(len(parts)):
                        if used[j]:
                            continue
                        pj = parts[j]
                        d_fwd = _dist(run[-1], pj[0])
                        if d_fwd < best_d:
                            best_d, best_j, best_rev = d_fwd, j, False
                        d_rev = _dist(run[-1], pj[-1])
                        if d_rev < best_d:
                            best_d, best_j, best_rev = d_rev, j, True
                    if best_j is not None:
                        pj = parts[best_j]
                        run.extend(pj[1:] if not best_rev else list(reversed(pj))[1:])
                        used[best_j] = True
                        extended = True
                runs.append(run)

            # Walk each run independently; never bridge across runs.
            for pts in runs:
                if len(pts) < 2:
                    continue
                total = 0.0
                segs = []
                for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                    seg = math.hypot(bx - ax, by - ay)
                    segs.append((seg, (ax, ay), (bx, by)))
                    total += seg
                if total < min_run_m:
                    continue
                cum = 0.0
                nxt = spacing_m
                for seg, (ax, ay), (bx, by) in segs:
                    end = cum + seg
                    while nxt <= end - 1e-9:
                        t = (nxt - cum) / seg if seg > 0 else 0.0
                        x = ax + t * (bx - ax)
                        y = ay + t * (by - ay)
                        if nxt > end_skip_m and (total - nxt) > end_skip_m:
                            key = (round(x, 1), round(y, 1))
                            if key not in seen:
                                seen.add(key)
                                out.append((x, y, 0))
                        nxt += spacing_m
                    cum = end
        return out

    def _count_ducts_near(self, index, feature_geoms, x, y, radius):
        """Count duct features whose geometry passes within radius of (x, y)."""
        pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        buf = pt.buffer(radius, 8)
        ids = index.intersects(buf.boundingBox())
        n = 0
        for fid in ids:
            g = feature_geoms.get(fid)
            if g is not None and g.intersects(buf):
                n += 1
        return n

    def _nearest_trench(self, trench_lyr, x, y, tol):
        if trench_lyr is None:
            return ""
        pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        best = ""
        best_d = tol
        for f in trench_lyr.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            d = g.distance(pt)
            if d <= best_d:
                best_d = d
                for cand in ("id", "SRC_ID", "POLYGON_ID"):
                    if f.fields().indexOf(cand) >= 0:
                        v = f[cand]
                        if v not in (None, ""):
                            best = str(v)
                            break
        return best

    def _dissolve_union(self, lyr):
        """Union of all geometries in a layer (or None when empty/invalid)."""
        if lyr is None or not lyr.isValid() or lyr.featureCount() == 0:
            return None
        u = QgsGeometry()
        for f in lyr.getFeatures():
            g = f.geometry()
            if g is None or g.isEmpty():
                continue
            if u.isEmpty():
                u = QgsGeometry(g)
            else:
                u = u.combine(g)
        return u if (u and not u.isEmpty()) else None

    def _snap_out_of_building(self, pt, bldg_union):
        """Radial search for the nearest point outside the building union.
        Returns the original point when no exit is found within SNAP_OUT_MAX_M."""
        if bldg_union is None:
            return pt
        g = QgsGeometry.fromPointXY(QgsPointXY(pt[0], pt[1]))
        if not bldg_union.contains(g):
            return pt
        step = self.SNAP_OUT_STEP_M
        for r_m in range(step, self.SNAP_OUT_MAX_M + 1, step):
            for ang in range(0, 360, 30):
                a = math.radians(ang)
                qx = pt[0] + r_m * math.cos(a)
                qy = pt[1] + r_m * math.sin(a)
                qg = QgsGeometry.fromPointXY(QgsPointXY(qx, qy))
                if not bldg_union.contains(qg):
                    return (qx, qy)
        return pt  # keep original when no nearby exit (better than teleporting)

    def _snap_onto_trenches(self, pt, trench_lines, max_m=None):
        """Snap a chamber ONTO the nearest trench path (chambers are the
        openings of the UG trench — they must lie on it).

        trench_lines: list of QgsGeometry. Returns the projected point as
        (x, y); the original point when the trench network is empty or the
        nearest trench is farther than ``max_m`` (default TRENCH_SNAP_M).
        HDD pits use a larger cap: the tangent midpoint sits mid-road while
        the trench follows the footway."""
        if not trench_lines:
            return pt
        cap = self.TRENCH_SNAP_M if max_m is None else max_m
        g = QgsGeometry.fromPointXY(QgsPointXY(pt[0], pt[1]))
        best_d, best_xy = None, None
        for tg in trench_lines:
            try:
                d = tg.distance(g)
            except Exception:
                continue
            if best_d is None or d < best_d:
                best_d = d
                best_xy = None
                try:
                    # QgsGeometry.closestPoint() is NOT available on this
                    # QGIS build — project the point onto the line with
                    # lineLocatePoint + interpolate instead.
                    along = tg.lineLocatePoint(g)
                    proj = tg.interpolate(along)
                    if proj and not proj.isEmpty():
                        p = proj.asPoint()
                        best_xy = (p.x(), p.y())
                except Exception:
                    best_xy = None
        if best_d is None or best_d > cap or best_xy is None:
            return pt
        return best_xy

    # ── main ─────────────────────────────────────────────────────────────

    def processAlgorithm(self, params, context, feedback):
        feeder = self._layer(params, self.P_FEEDER_DUCTS, context)
        dist = self._layer(params, self.P_DIST_DUCTS, context)
        drop = self._layer(params, self.P_DROP_DUCTS, context)
        pdp_lyr = self._layer(params, self.P_PDP, context)
        tangents = self._layer(params, self.P_TANGENTS, context)
        trenches = self._layer(params, self.P_TRENCHES, context)
        aoi_lyr = self._layer(params, self.P_AOI, context)
        bldg_lyr = self._layer(params, self.P_BUILDINGS, context)

        crs = None
        for lyr in (feeder, dist, drop, pdp_lyr, tangents, trenches):
            if lyr is not None and lyr.isValid():
                crs = lyr.crs()
                break
        if crs is None:
            raise QgsProcessingException(
                self.tr("At least one input layer is required."))

        # ── gather raw candidates ────────────────────────────────────────
        candidates = []
        # Tuples: (x, y, priority, type, equipment, weight, min_spacing_m, reason)
        # Priority: Chamber=3 > Manhole=2 > Handhole=1 (denser junctions win)

        # Rule 1 + 3 — Chamber at every PDP (splitter location; feeder and
        # distribution meet there, so it doubles as the F2D transition point).
        if pdp_lyr is not None:
            for f in pdp_lyr.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                try:
                    pt = g.asPoint()
                except Exception:
                    continue
                pid = ""
                if f.fields().indexOf("PDP_ID") >= 0:
                    pid = str(f["PDP_ID"] or "")
                candidates.append((pt.x(), pt.y(), 3, "Chamber", pid, 999,
                                   self.MIN_STRUCTURE_SEPARATION_M, "Splitter/F2D (PDP)"))

        # Rule 7 — HDD entry/exit pits at used drill crossings.
        # A drill (HDD) trench needs a chamber at BOTH ends so the cable can
        # be pulled through — one mid-road manhole is not enough. The drill
        # legs arrive merged into Final_Trenches as chained segments tagged
        # HDD; an endpoint where two legs chain together (another endpoint
        # within JOINT_TOL_M) is an INTERIOR joint, not an entry/exit. Only
        # single-incident endpoints get a pit. The tangent-crossing layer
        # (perpendicular drill segments) is kept as a fallback for runs
        # where the trench layer does not carry the drill legs.
        DRILL_DEDUP_M = 60.0
        JOINT_TOL_M = 2.0
        seen_drill = []

        # (a) Primary: entry/exit pits from the HDD legs in Final_Trenches.
        hdd_occ = []  # every HDD leg endpoint: (x, y, occurrence index)
        if trenches is not None and trenches.isValid() and \
                trenches.fields().indexOf("trench_type") >= 0:
            for f in trenches.getFeatures():
                tt = str(f["trench_type"] or "").strip().lower()
                if tt != "hdd":
                    continue
                g = f.geometry()
                if g is None or g.isEmpty() or \
                        g.type() != QgsWkbTypes.LineGeometry:
                    continue
                parts = []
                try:
                    cg = g.constGet()
                    if QgsWkbTypes.isMultiType(cg.wkbType()):
                        for part in cg.parts():
                            pts = [(part.pointN(i).x(), part.pointN(i).y())
                                   for i in range(part.numPoints())]
                            if len(pts) >= 2:
                                parts.append(pts)
                    else:
                        pts = [(cg.pointN(i).x(), cg.pointN(i).y())
                               for i in range(cg.numPoints())]
                        if len(pts) >= 2:
                            parts.append(pts)
                except Exception:
                    continue
                for pts in parts:
                    hdd_occ.append((pts[0][0], pts[0][1], len(hdd_occ)))
                    hdd_occ.append((pts[-1][0], pts[-1][1], len(hdd_occ)))
        for x, y, idx in hdd_occ:
            neighbours = sum(
                1 for x2, y2, j in hdd_occ
                if j != idx and (x2 - x) ** 2 + (y2 - y) ** 2 <= JOINT_TOL_M ** 2)
            if neighbours:
                continue  # interior joint — legs chain here, not an opening
            too_close = any((x - sx) ** 2 + (y - sy) ** 2 < 4.0
                            for sx, sy in seen_drill)
            if not too_close:
                candidates.append((x, y, 3, "Chamber", "", 1000,
                                   self.HDD_PIT_SPACING_M, "HDD pit"))
                seen_drill.append((x, y))

        # (b) Fallback: tangent-crossing midpoints, only when the trench
        # layer carried no HDD legs at all (drills available solely as
        # perpendicular tangent segments). Dedup so a row of pits on one
        # HDD run collapses to a single manhole.
        if not hdd_occ and tangents is not None:
            for f in tangents.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                try:
                    if g.type() == QgsWkbTypes.LineGeometry:
                        pt = g.centroid().asPoint()
                    else:
                        pt = g.asPoint()
                except Exception:
                    continue
                px, py = pt.x(), pt.y()
                too_close = False
                for sx, sy in seen_drill:
                    if ((px - sx) ** 2 + (py - sy) ** 2) ** 0.5 < DRILL_DEDUP_M:
                        too_close = True
                        break
                if not too_close:
                    candidates.append((px, py, 3, "Manhole", "", 1000,
                                       self.HDD_PIT_SPACING_M, "HDD pit"))
                    seen_drill.append((px, py))

        # Rule 2 — Branching points: Manhole at feeder junctions, Handhole at
        # distribution junctions (>= JUNCTION_MIN_DUCTS distinct ducts).
        for x, y, w in self._junction_points(feeder, self.JUNCTION_RADIUS_M):
            if w >= self.JUNCTION_MIN_DUCTS:
                candidates.append((x, y, 2, "Manhole", "", w,
                                   self.JUNCTION_SPACING_M, "Branching junction"))
        for x, y, w in self._junction_points(dist, self.JUNCTION_RADIUS_M):
            if w >= self.JUNCTION_MIN_DUCTS:
                candidates.append((x, y, 1, "Handhole", "", w,
                                   self.HANDHOLE_SPACING_M, "Branching junction"))

        # Rule 9 — Trench intersections ("chambers at the intersections").
        # Rule 2 above only counts DUCTS, so a point where the trench network
        # genuinely branches but fewer than three distinct ducts pass got no
        # structure. Any point where >= TRENCH_JUNCTION_MIN distinct trench runs
        # meet gets a Handhole, or a Manhole when the junction is dense enough
        # (>= 4 runs) — an HDD crossing meeting the open cut, a garden leg
        # meeting the mains, a distribution spine leaving the backbone.
        # The structure is snapped onto the trench path in the constraint pass,
        # so it lands exactly on the intersection.
        for x, y, w in self._junction_points(trenches, self.JUNCTION_RADIUS_M):
            if w >= self.TRENCH_JUNCTION_MIN:
                ctype = "Manhole" if w >= self.TRENCH_JUNCTION_MIN + 1 else "Handhole"
                candidates.append((x, y, 2, ctype, "", w,
                                   self.JUNCTION_SPACING_M,
                                   "Trench intersection"))

        # Rule 4 — Distribution→Drop transition: Handhole only where a
        # cluster of drop ducts taps off (>= DROP_TRANSITION_MIN_DROPS within
        # CONN_RADIUS_M). Single drop-taps need no structure — the drop
        # connects directly in a shallow joint.
        for x, y, ndrops in self._drop_transition_points(dist, drop, self.CONN_RADIUS_M):
            if ndrops >= self.DROP_TRANSITION_MIN_DROPS:
                candidates.append((x, y, 1, "Handhole", "", ndrops,
                                   self.DROP_TRANSITION_SPACING_M, "Drop transition"))

        # Rule 6 — Direction changes > 45°: Manhole on feeder bends,
        # Handhole on distribution bends.
        for x, y, deg in self._bend_points(feeder, self.BEND_ANGLE_DEG):
            candidates.append((x, y, 2, "Manhole", "", int(deg),
                               self.BEND_SPACING_M, "Direction change"))
        for x, y, deg in self._bend_points(dist, self.BEND_ANGLE_DEG):
            candidates.append((x, y, 1, "Handhole", "", int(deg),
                               self.BEND_SPACING_M, "Direction change"))

        # Rule 8 — Long straight routes: intermediate pull Manholes on feeder
        # runs, Handholes on distribution runs, every ~250 m.
        for x, y, _w in self._pull_points(
                feeder, self.PULL_SPACING_M, self.PULL_MIN_RUN_M, self.PULL_END_SKIP_M):
            candidates.append((x, y, 2, "Manhole", "", 1,
                               self.PULL_SPACING_M, "Pull point"))
        for x, y, _w in self._pull_points(
                dist, self.PULL_SPACING_M, self.PULL_MIN_RUN_M, self.PULL_END_SKIP_M):
            candidates.append((x, y, 1, "Handhole", "", 1,
                               self.PULL_SPACING_M, "Pull point"))

        # ── boundary + building constraints (HLD review) ─────────────────
        # Chambers must be inside the design boundary and out of buildings,
        # EXCEPT high-density PDP chambers (>50 HP) which may stay in-building.
        aoi_union = self._dissolve_union(aoi_lyr)
        bldg_union = self._dissolve_union(bldg_lyr)

        # ── trench-path snapping (chambers ARE the trench openings) ──────
        trench_lines = []
        if trenches is not None and trenches.isValid():
            for f in trenches.getFeatures():
                g = f.geometry()
                if g and not g.isEmpty() and g.type() == QgsWkbTypes.LineGeometry:
                    trench_lines.append(g)

        high_density_pdps = set()
        if pdp_lyr is not None and bldg_union is not None:
            f_pid = "PDP_ID" if (pdp_lyr.fields().indexOf("PDP_ID") >= 0) else None
            f_hh = None
            for nm in ("HH", "hh", "HP", "hp", "HOMES", "UNITS"):
                if pdp_lyr.fields().indexOf(nm) >= 0:
                    f_hh = nm
                    break
            if f_pid and f_hh:
                for f in pdp_lyr.getFeatures():
                    try:
                        if int(float(f[f_hh] or 0)) > self.PDP_INBUILDING_HH:
                            high_density_pdps.add(str(f[f_pid] or ""))
                    except Exception:
                        continue
        dropped_boundary = 0
        snapped_building = 0
        snapped_trench = 0
        # ALWAYS run the constraint loop — the trench-path snap is mandatory
        # (chambers are the openings of the UG trench), while the boundary and
        # building rules apply only when those layers are supplied.
        constrained = []
        for cand in candidates:
            x, y = cand[0], cand[1]
            # HDD pits are placed at the midpoint of the road-crossing tangent
            # line, which sits mid-road while the trench follows the footway —
            # they get a wider snap cap so they still end up ON the path.
            reason0 = cand[7] if len(cand) > 7 else ""
            cap = self.HDD_SNAP_M if reason0 == "HDD pit" else None
            # Rule: snap ONTO the trench path first — the chamber is the
            # opening of the UG trench and must lie on it.
            if trench_lines:
                nx, ny = self._snap_onto_trenches((x, y), trench_lines, max_m=cap)
                if (nx, ny) != (x, y):
                    snapped_trench += 1
                    x, y = nx, ny
            # Rule: keep within the design boundary (small edge tolerance).
            if aoi_union is not None:
                ptg = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                if not aoi_union.contains(ptg):
                    if aoi_union.distance(ptg) > self.INSIDE_TOL_M:
                        dropped_boundary += 1
                        continue
            # Rule: snap chambers out of buildings. High-density PDP
            # chambers (splitter in-building exemption) stay put.
            if bldg_union is not None:
                equip = cand[4] if len(cand) > 4 else ""
                if not (equip and any(equip.startswith(hd) for hd in high_density_pdps)):
                    nx, ny = self._snap_out_of_building((x, y), bldg_union)
                    if (nx, ny) != (x, y):
                        snapped_building += 1
                        x, y = nx, ny
            # Final re-snap: a building snap-out may have pulled the
            # chamber off the trench — chambers must LIE on the path.
            if trench_lines:
                x, y = self._snap_onto_trenches((x, y), trench_lines, max_m=cap)
            cand = (x, y) + tuple(cand[2:])
            constrained.append(cand)
        candidates = constrained

        # ── collapse duplicates (highest priority, densest first) ────────
        kept = self._place_structures(candidates)

        # ── build spatial index of all ducts for CONN_DUCTS ─────────────
        duct_index = QgsSpatialIndex()
        duct_geoms = {}
        for lyr in (feeder, dist):
            if lyr is None:
                continue
            for f in lyr.getFeatures():
                g = f.geometry()
                if g is None or g.isEmpty():
                    continue
                fid = f.id()
                duct_index.addFeature(f)
                duct_geoms[fid] = g

        # ── output ───────────────────────────────────────────────────────
        out_fields = build_fields(THIN_PROFILES["CHAMBER"])
        sink, out_id = self.parameterAsSink(
            params, self.OUT_CHAMBERS, context,
            out_fields, QgsWkbTypes.Point, crs,
        )

        counters = {"HH": 0, "DHH": 0, "MH": 0}
        reason_counts = {}
        written = 0
        for cand in kept:
            x, y, prio, ctype, equip, weight = cand[:6]
            reason = cand[7] if len(cand) > 7 else ""
            # ── Standard catalogue: HH / DHH / MH (fixed sizes) ──
            code = self.REASON_TYPE.get(reason, "HH")
            if reason == "Branching junction" and prio < 2:
                code = "HH"  # distribution-level junctions stay handholes
            subtype = "Bore" if reason == "HDD pit" else self.SUBTYPE_BY_CODE.get(code, "Handhole")
            type_name, size_str = self.CHAMBER_CATALOGUE[code]
            counters[code] += 1
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            struct_id = f"{code}-{counters[code]:04d}"
            conn = self._count_ducts_near(duct_index, duct_geoms, x, y, self.CONN_RADIUS_M)
            # Fixed catalogue sizes — no Small/Medium/Large buckets.
            size = size_str

            feat = QgsFeature(out_fields)
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
            feat[COMMON_FIELDS.STRUCT_ID] = struct_id
            feat[COMMON_FIELDS.CHAMBER_TYPE] = code  # HH | DHH | MH
            feat[COMMON_FIELDS.SUBTYPE] = subtype    # Bore | Handhole | Manhole
            feat[COMMON_FIELDS.REASON] = reason
            feat[COMMON_FIELDS.PARENT_TRENCH] = self._nearest_trench(
                trenches, x, y, self.TRENCH_JOIN_M)
            feat[COMMON_FIELDS.CONN_DUCTS] = conn
            feat[COMMON_FIELDS.SIZE] = size
            feat[COMMON_FIELDS.EQUIPMENT] = (equip or "") + (("; " + reason) if reason else "")
            feat[COMMON_FIELDS.CAPACITY_USED] = 0
            feat[COMMON_FIELDS.CAPACITY_TOTAL] = conn
            feat[COMMON_FIELDS.INFRA_STATUS] = InfraStatus.PROPOSED
            feat[COMMON_FIELDS.VERIFY_STATUS] = VerifyStatus.VERIFIED
            feat[COMMON_FIELDS.STAGE] = "Civil"
            if sink is not None:
                sink.addFeature(feat, QgsFeatureSink.FastInsert)
                written += 1

        rc = ", ".join(f"{k}: {v}" for k, v in sorted(reason_counts.items()))
        stats = getattr(self, "_place_stats", {}) or {}
        feedback.pushInfo(self.tr(
            f"Chamber layer: {written} planned structures "
            f"(MH: {counters['MH']}, DHH: {counters['DHH']}, HH: {counters['HH']}).\n"
            f"  Placement reasons: {rc or 'none'}\n"
            f"  Separation: {stats.get('blocked_close', 0)} candidate(s) dropped inside an "
            f"existing keep-out (floor {self.MIN_STRUCTURE_SEPARATION_M:g} m, "
            f"HDD pit keep-out {self.HDD_PIT_SPACING_M:g} m); "
            f"{stats.get('merged_splitter', 0)} splitter location(s) merged into the HDD pit "
            f"they sit on.\n"
            f"  Boundary: {dropped_boundary} candidate(s) outside design boundary removed; "
            f"{snapped_building} chamber(s) snapped out of buildings; "
            f"{snapped_trench} chamber(s) snapped onto trench paths (tol {self.TRENCH_SNAP_M} m)."))

        result = {}
        if out_id:
            result[self.OUT_CHAMBERS] = out_id
        return result
