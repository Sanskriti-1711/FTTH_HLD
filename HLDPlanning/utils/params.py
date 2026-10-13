# utils/params.py
# Canonical names & IDs shared across all layers and the OneClick pipeline.

import os


class LAYERNAMES:
    OBJECT = "01_object_layer"
    AOI = "AOI"
    ROADS = "Roads"
    BUILDINGS = "Buildings"
    POLYGON = "02_polygon_layer"
    NETWORK = "Network_Elements"
    TRENCH_FINAL = "Final_Trenches"
    TRENCH_TANGENT = "Final_Tangent_Trenches"

class FIELD:
    ADDR_ID = "ADDR_ID"
    # The household count.  Named for what it is: `HH` read as a household
    # COUNT was routinely mistaken for an identifier, and the served layer
    # states each building's total under `households`.  `HH` remains accepted
    # as a legacy INPUT alias (see sheet_utils.EXPECTED_MAP).
    HH = "households"
    LAT = "LATITUDE"
    LON = "LONGITUDE"
    GEOCODE_STATUS = "GEOCODE_STATUS"
    GEOCODE_SOURCE = "SOURCE"
    GEOCODE_Q = "GEOCODE_Q"

class ALG:
    BROWNFIELD = "hldplanning:00_brownfield_layer"
    OBJECT = "hldplanning:01_object_layer"
    POLYGON = "hldplanning:02_polygon_layer"
    NETWORK = "hldplanning:03_network_layer"
    TRENCH  = "hldplanning:04_trench_layer"
    # Designer-backed trench stage — same parameters and outputs as TRENCH.
    TRENCH_DESIGN = "hldplanning:04_trench_design_layer"
    DUCT    = "hldplanning:05_duct_layer"
    CABLE   = "hldplanning:06_cable_layer"
    CHAMBER = "hldplanning:07_chamber_layer"
    POLE    = "hldplanning:08_pole_layer"
    AERIAL  = "hldplanning:09_aerial_drop_layer"

class TRENCH_ENGINE:
    """The single trench implementation used by the production pipeline.

    The civil designer routes the plan over the street graph, types every span
    Open Cut / HDD / Garden and cuts node-to-node spans (HDD ends landing on
    open cut). The legacy sidewalk/graph stage remains available as a direct
    QGIS algorithm for comparison, but the end-to-end pipeline always selects
    this designer so a stale environment setting cannot send runs down the
    known-failing legacy path.
    """

    ENV = "TRENCH_ENGINE"
    DESIGN = "design"
    ENGINES = (DESIGN,)
    DEFAULT = DESIGN

    @classmethod
    def resolve(cls, raw=None):
        """Resolve the one supported pipeline engine.

        Returns ``(design, invalid_raw)``. Any explicitly set legacy/unknown
        value is reported as invalid but cannot select another implementation.
        """
        if raw is None:
            raw = os.environ.get(cls.ENV) or ""
        value = raw.strip().lower()
        if not value or value == cls.DESIGN:
            return cls.DESIGN, None
        return cls.DESIGN, raw.strip()

    @classmethod
    def algorithm_id(cls, engine=None):
        """Return the production trench algorithm (always the civil designer)."""
        return ALG.TRENCH_DESIGN


class AERIAL_DECISION:
    """How the docx's aerial business decision is made, and its permit input.

    ``docs/aerial planning.docx`` gives TWO ways to decide a drop, and both are
    implemented (``design/aerial_feasibility``):

    * ``chain`` — the IF-chain in its own order (the default);
    * ``score`` — "Instead of many IF conditions, assign points", where the
      facts add up instead of the first one that fires deciding.

    Selected the same way the trench engine is (``TRENCH_ENGINE``), because the
    engine's run parameters are a fixed set the upload form knows nothing
    about: an environment setting on the engine, or ``--aerial-decision-model``
    on the standalone designer. Nothing about the failure mode changes if the
    setting is wrong — the file carries the whole decision either way.
    """

    MODEL_ENV = "AERIAL_DECISION"
    CHAIN = "chain"
    SCORE = "score"
    MODELS = (CHAIN, SCORE)
    DEFAULT = CHAIN

    # The project-wide permit lead time, used as the FALLBACK for a premise no
    # permit-delay area covers. 0/blank means "unknown", never a measured zero.
    PERMIT_DAYS_ENV = "AERIAL_PERMIT_DELAY_DAYS"

    @classmethod
    def resolve_model(cls, raw=None):
        """Return ``(model, invalid_raw)`` — the valid models, nothing else."""
        if raw is None:
            raw = os.environ.get(cls.MODEL_ENV) or ""
        value = raw.strip().lower()
        if not value:
            return cls.DEFAULT, None
        if value in cls.MODELS:
            return value, None
        return cls.DEFAULT, raw.strip()

    @classmethod
    def permit_days(cls, raw=None):
        """The project-wide permit lead time in days, 0.0 when not declared."""
        if raw is None:
            raw = os.environ.get(cls.PERMIT_DAYS_ENV) or ""
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return 0.0


# Vendor/customer profile keys that can be overridden per deployment
class PROFILE_KEYS:
    DEFAULT_COUNTRY = "default_country"     # e.g., "Germany"
    NOMINATIM_EMAIL = "nominatim_email"     # fallback email for UA
    NOMINATIM_MIN_DELAY = "nominatim_min_delay"  # seconds (>= 1.0)
    OBJECT_INCLUDE_ALL = "object_include_all"    # bool
    ADDR_PREFIX = "addr_prefix"                   # e.g., "ADDR"
