# -*- coding: utf-8 -*-
"""
Network Registry: Immutable shared source of truth for network state.

Stores all polygon/PDP/address relationships in memory during processing.
Eliminates need for layer scanning and synchronization.

Structure:
    networks = {
        polygon_id: {
            "polygon_geom": QgsGeometry,
            "pdp_id": str,
            "pdp_geom": QgsGeometry,
            "addresses": [addr_id, ...],
            "address_count": int,
        },
        ...
    }
"""

class NetworkRegistry:
    """Immutable registry of all polygon/PDP/address relationships."""

    def __init__(self):
        self._networks = {}  # {polygon_id: {polygon_geom, pdp_id, pdp_geom, addresses, ...}}
        self._pdp_lookup = {}  # {pdp_id: polygon_id} for reverse lookups
        self._address_lookup = {}  # {address_id: polygon_id} for reverse lookups

    def register_network(self, polygon_id, polygon_geom, pdp_id, pdp_geom, addresses):
        """
        Register a complete polygon/PDP/address network.
        
        Args:
            polygon_id: Unique polygon ID
            polygon_geom: QgsGeometry of the polygon
            pdp_id: Unique PDP ID for this polygon
            pdp_geom: QgsGeometry of the PDP point
            addresses: List of address IDs inside the polygon
        """
        if polygon_id in self._networks:
            raise ValueError(f"Polygon {polygon_id} already registered")
        
        self._networks[polygon_id] = {
            "polygon_geom": polygon_geom,
            "pdp_id": pdp_id,
            "pdp_geom": pdp_geom,
            "addresses": addresses,
            "address_count": len(addresses),
        }
        
        self._pdp_lookup[pdp_id] = polygon_id
        for addr_id in addresses:
            self._address_lookup[addr_id] = polygon_id

    def get_network(self, polygon_id):
        """Retrieve a complete network by polygon ID."""
        return self._networks.get(polygon_id)

    def get_polygon_for_pdp(self, pdp_id):
        """Look up the polygon ID for a given PDP ID."""
        return self._pdp_lookup.get(pdp_id)

    def get_polygon_for_address(self, address_id):
        """Look up the polygon ID for a given address."""
        return self._address_lookup.get(address_id)

    def all_networks(self):
        """Iterate all registered networks."""
        return self._networks.values()

    def polygon_ids(self):
        """Get all registered polygon IDs."""
        return list(self._networks.keys())

    def pdp_ids(self):
        """Get all registered PDP IDs."""
        return list(self._pdp_lookup.keys())

    def size(self):
        """Total number of networks registered."""
        return len(self._networks)

    def summary(self):
        """Return a human-readable summary."""
        total_addresses = sum(n["address_count"] for n in self._networks.values())
        return (
            f"NetworkRegistry: {len(self._networks)} polygons, "
            f"{len(self._pdp_lookup)} PDPs, {total_addresses} addresses"
        )
