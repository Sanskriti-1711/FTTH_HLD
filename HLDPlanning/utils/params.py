# utils/params.py
# Canonical names & IDs shared across all layers and the OneClick pipeline.

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
    OBJECT = "hldplanning:01_object_layer"
    POLYGON = "hldplanning:02_polygon_layer"
    NETWORK = "hldplanning:03_network_layer"
    TRENCH  = "hldplanning:04_trench_layer"
    DUCT    = "hldplanning:05_duct_layer"
    CABLE   = "hldplanning:06_cable_layer"

# Vendor/customer profile keys that can be overridden per deployment
class PROFILE_KEYS:
    DEFAULT_COUNTRY = "default_country"     # e.g., "Germany"
    NOMINATIM_EMAIL = "nominatim_email"     # fallback email for UA
    NOMINATIM_MIN_DELAY = "nominatim_min_delay"  # seconds (>= 1.0)
    OBJECT_INCLUDE_ALL = "object_include_all"    # bool
    ADDR_PREFIX = "addr_prefix"                   # e.g., "ADDR"
