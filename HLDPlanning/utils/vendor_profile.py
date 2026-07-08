# utils/vendor_profile.py
# Small, optional config layer so vendor/customer differences live in one place.

import json, os
from .params import PROFILE_KEYS as K

_DEFAULT = {
    K.DEFAULT_COUNTRY: "Germany",
    K.NOMINATIM_EMAIL: "you@example.com",
    K.NOMINATIM_MIN_DELAY: 1.2,
    K.OBJECT_INCLUDE_ALL: True,
    K.ADDR_PREFIX: "ADDR",
}

class VendorProfile:
    """
    A tiny overlay: defaults + optional JSON override.
    JSON example:
    {
      "default_country": "Austria",
      "nominatim_email": "ops@client.de",
      "nominatim_min_delay": 1.0,
      "object_include_all": true,
      "addr_prefix": "ADR"
    }
    """
    def __init__(self, json_path: str | None = None, overrides: dict | None = None):
        cfg = dict(_DEFAULT)
        if json_path and os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except Exception:
                pass
        if overrides:
            cfg.update({k: v for k, v in overrides.items() if v is not None})
        self._cfg = cfg

    def get(self, key, default=None):
        return self._cfg.get(key, default)

    # Convenience properties
    @property
    def default_country(self): return self._cfg[K.DEFAULT_COUNTRY]
    @property
    def nominatim_email(self): return self._cfg[K.NOMINATIM_EMAIL]
    @property
    def nominatim_min_delay(self): return float(self._cfg[K.NOMINATIM_MIN_DELAY])
    @property
    def object_include_all(self): return bool(self._cfg[K.OBJECT_INCLUDE_ALL])
    @property
    def addr_prefix(self): return str(self._cfg[K.ADDR_PREFIX]).strip() or "ADDR"
