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
    HH = "HH"
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
    """Which algorithm answers the pipeline's trench stage.

    Two engines produce the same trench-stage contract — the same parameters in
    and the same layers out — so the stage can be switched without touching any
    other stage:

        legacy  hldplanning:04_trench_layer         sidewalk/graph trenches
        design  hldplanning:04_trench_design_layer  civil trench designer

    ``design`` is the default: the designer routes the plan over the street
    graph, types every span Open Cut / HDD / Garden and cuts node-to-node spans
    (HDD ends landing on open cut), which is the network the platform's trench
    design page shows.  ``legacy`` stays reachable so a run that goes wrong is
    switched back with one environment variable instead of a revert.

    Selection is per process, from ``TRENCH_ENGINE``:  a QGIS run started by the
    engine backend inherits the server's environment, so setting it there
    switches every project; setting it for a single ``qgis_process`` call
    switches that run.
    """

    ENV = "TRENCH_ENGINE"
    LEGACY = "legacy"
    DESIGN = "design"
    ENGINES = (LEGACY, DESIGN)
    DEFAULT = DESIGN

    @classmethod
    def resolve(cls, raw=None):
        """Resolve the engine to run.

        Returns ``(engine, invalid_raw)`` where ``invalid_raw`` is the
        unrecognised value that was requested, or ``None``.  An unrecognised
        value falls back to the default rather than failing the run, but it is
        reported so a typo cannot silently change the design.
        """
        if raw is None:
            raw = os.environ.get(cls.ENV) or ""
        value = raw.strip().lower()
        if not value:
            return cls.DEFAULT, None
        if value in cls.ENGINES:
            return value, None
        return cls.DEFAULT, raw.strip()

    @classmethod
    def algorithm_id(cls, engine=None):
        """Processing algorithm id for ``engine`` (defaults to the current one)."""
        if engine is None:
            engine = cls.resolve()[0]
        return ALG.TRENCH_DESIGN if engine == cls.DESIGN else ALG.TRENCH


# Vendor/customer profile keys that can be overridden per deployment
class PROFILE_KEYS:
    DEFAULT_COUNTRY = "default_country"     # e.g., "Germany"
    NOMINATIM_EMAIL = "nominatim_email"     # fallback email for UA
    NOMINATIM_MIN_DELAY = "nominatim_min_delay"  # seconds (>= 1.0)
    OBJECT_INCLUDE_ALL = "object_include_all"    # bool
    ADDR_PREFIX = "addr_prefix"                   # e.g., "ADDR"
