# -*- coding: utf-8 -*-
"""
Network Manager: Single source of truth for network generation and ID assignment.

Responsibilities:
- Assign POLYGON_ID, PDP_ID immediately upon creation
- Register polygons, PDPs, and address associations
- Batch-update object layer with polygon/PDP IDs
- Write all features to output sinks in one final pass
- Maintain the NetworkRegistry

Usage:
    manager = NetworkManager(crs, feedback)
    for polygon in polygons:
        addresses_in_polygon = [...]
        manager.register_polygon(polygon, addresses_in_polygon)
    manager.update_object_layer(object_layer)
    manager.write_polygons_to_sink(sink)
    manager.write_pdps_to_sink(sink)
    registry = manager.get_registry()  # for downstream algorithms
"""

from qgis.core import QgsFeature, QgsFields, QgsField, QgsGeometry
from qgis.PyQt.QtCore import QVariant
from .network_registry import NetworkRegistry


class NetworkManager:
    """Manages the full lifecycle of network/polygon/PDP/address associations."""

    def __init__(self, crs, feedback=None):
        """
        Initialize the network manager.

        Args:
            crs: QgsCoordinateReferenceSystem for all output layers
            feedback: Optional QgsProcessingFeedback for progress reporting
        """
        self.crs = crs
        self.feedback = feedback
        self._registry = NetworkRegistry()
        self._polygon_counter = 0
        self._pdp_counter = 0
        self._polygon_prefix = "POLY"
        self._pdp_prefix = "PDP"
        self._mfg_prefix = "MFG"
        self._mfg = None

    def set_id_prefixes(self, polygon_prefix="POLY", pdp_prefix="PDP"):
        """Set custom prefixes for generated IDs (e.g., 'BE' -> 'BE00001')."""
        self._polygon_prefix = polygon_prefix
        self._pdp_prefix = pdp_prefix

    def _ensure_unique_polygon_id(self, polygon_id):
        """Ensure polygon_id is unique in the registry; append suffix for duplicates."""
        base = str(polygon_id)
        if self._registry.get_network(base) is None:
            return base

        i = 2
        while True:
            candidate = f"{base}__{i}"
            if self._registry.get_network(candidate) is None:
                self._report(
                    f"Duplicate polygon id '{base}' detected; using unique id '{candidate}'. "
                    "Consider providing a unique POLY_ID field for perfect 1:1 alignment."
                )
                return candidate
            i += 1

    def register_polygon(self, polygon_geom, pdp_geom, addresses_in_polygon, polygon_id_override=None):
        """
        Register a new polygon with its PDP and addresses.
        
        Immediately assigns POLYGON_ID and PDP_ID.
        
        Args:
            polygon_geom: QgsGeometry of the polygon
            pdp_geom: QgsGeometry of the PDP point
            addresses_in_polygon: List of address IDs inside this polygon
            polygon_id_override: Optional external polygon ID to preserve source IDs
        
        Returns:
            Tuple (polygon_id, pdp_id)
        """
        self._pdp_counter += 1

        if polygon_id_override is not None and str(polygon_id_override).strip() != "":
            polygon_id = str(polygon_id_override).strip()
        else:
            self._polygon_counter += 1
            polygon_id = f"{self._polygon_prefix}{self._polygon_counter:05d}"
        polygon_id = self._ensure_unique_polygon_id(polygon_id)
        pdp_id = f"{self._pdp_prefix}{self._pdp_counter:05d}"
        
        self._registry.register_network(
            polygon_id=polygon_id,
            polygon_geom=polygon_geom,
            pdp_id=pdp_id,
            pdp_geom=pdp_geom,
            addresses=addresses_in_polygon,
        )
        
        self._report(f"Registered {polygon_id} with PDP {pdp_id} ({len(addresses_in_polygon)} addresses)")
        return polygon_id, pdp_id

    def register_mfg(self, mfg_geom, mfg_id_override=None):
        """
        Register the (single) MFG point and assign its canonical MFG_ID.

        Args:
            mfg_geom: QgsGeometry of the MFG point
            mfg_id_override: Optional external MFG ID to preserve source IDs

        Returns:
            The assigned MFG_ID string (e.g. "MFG00001")
        """
        if mfg_id_override is not None and str(mfg_id_override).strip() != "":
            mfg_id = str(mfg_id_override).strip()
        else:
            mfg_id = f"{self._mfg_prefix}00001"
        self._mfg = {"mfg_id": mfg_id, "mfg_geom": mfg_geom}
        self._report(f"Registered MFG {mfg_id}")
        return mfg_id

    def get_mfg(self):
        """Return the registered MFG as {'mfg_id', 'mfg_geom'}, or None."""
        return self._mfg

    def update_object_layer(self, object_layer, addr_id_field="ADDR_ID", use_feature_id=False, mfg_id=None):
        """
        Update the object layer with POLYGON_ID and PDP_ID for all addresses.

        This is a batch operation: all addresses are updated in one pass.

        Args:
            object_layer: QgsVectorLayer to update (must have addr_id_field, POLYGON_ID, PDP_ID)
            addr_id_field: Name of the address ID field
            use_feature_id: If True, match addresses using QgsFeature.id() instead of an attribute field
            mfg_id: Optional MFG_ID to stamp on every feature (single-MFG networks)

        Returns:
            dict with sync statistics: expected, updated, unmatched, and matching mode details
        """
        # Add fields if they don't exist
        provider = object_layer.dataProvider()

        if object_layer.fields().indexOf("POLYGON_ID") < 0:
            provider.addAttributes([QgsField("POLYGON_ID", QVariant.String)])
        if object_layer.fields().indexOf("PDP_ID") < 0:
            provider.addAttributes([QgsField("PDP_ID", QVariant.String)])
        if mfg_id is not None and object_layer.fields().indexOf("MFG_ID") < 0:
            provider.addAttributes([QgsField("MFG_ID", QVariant.String)])

        object_layer.updateFields()
        
        # Build a lookup: address_id -> (polygon_id, pdp_id)
        updates = {}
        for net_id in self._registry.polygon_ids():
            net = self._registry.get_network(net_id)
            for addr_id in net["addresses"]:
                updates[addr_id] = (net_id, net["pdp_id"])

        # Also keep a string-keyed lookup to tolerate text/integer field type mismatches.
        updates_str = {str(k): v for k, v in updates.items()}

        expected = len(updates)
        has_addr_field = object_layer.fields().indexOf(addr_id_field) >= 0

        def _lookup(key):
            if key in updates:
                return updates[key]
            key_s = str(key)
            return updates_str.get(key_s)
        
        # Update features via a single batch provider call
        poly_idx = object_layer.fields().indexOf("POLYGON_ID")
        pdp_idx = object_layer.fields().indexOf("PDP_ID")
        mfg_idx = object_layer.fields().indexOf("MFG_ID") if mfg_id is not None else -1
        attr_changes = {}
        updated_count = 0
        matched_by_field = 0
        matched_by_feature_id = 0
        for feature in object_layer.getFeatures():
            match = None

            # Primary mode: explicit feature id matching.
            if use_feature_id:
                match = _lookup(feature.id())
                if match is not None:
                    matched_by_feature_id += 1

            # Secondary mode: match using attribute field.
            elif has_addr_field:
                try:
                    match = _lookup(feature[addr_id_field])
                except Exception:
                    match = None
                if match is not None:
                    matched_by_field += 1

            # Fallback mode: if field-based matching failed (or field missing), try feature id.
            if match is None and not use_feature_id:
                match = _lookup(feature.id())
                if match is not None:
                    matched_by_feature_id += 1

            changes = {}
            if match is not None:
                poly_id, pdp_id = match
                changes[poly_idx] = poly_id
                changes[pdp_idx] = pdp_id
                updated_count += 1
            # MFG_ID applies to every object in a single-MFG network
            if mfg_idx >= 0:
                changes[mfg_idx] = mfg_id
            if changes:
                attr_changes[feature.id()] = changes

        committed = provider.changeAttributeValues(attr_changes) if attr_changes else True
        object_layer.updateFields()

        stats = {
            "expected": expected,
            "updated": updated_count,
            "unmatched": max(0, expected - updated_count),
            "matched_by_field": matched_by_field,
            "matched_by_feature_id": matched_by_feature_id,
            "addr_id_field": addr_id_field,
            "used_feature_id_mode": bool(use_feature_id),
            "commit_ok": bool(committed),
        }

        self._report(
            "Object sync: expected={expected}, updated={updated}, unmatched={unmatched}, "
            "field_matches={field}, fid_matches={fid}, commit_ok={commit_ok}".format(
                expected=stats["expected"],
                updated=stats["updated"],
                unmatched=stats["unmatched"],
                field=stats["matched_by_field"],
                fid=stats["matched_by_feature_id"],
                commit_ok=stats["commit_ok"],
            )
        )
        return stats

    def write_polygons_to_sink(self, sink, polygon_id_field="POLYGON_ID"):
        """
        Write all polygons to an output sink.
        
        Args:
            sink: QgsFeatureSink for polygon features
            polygon_id_field: Name of the polygon ID field in the sink
        """
        written = 0
        for poly_id in self._registry.polygon_ids():
            net = self._registry.get_network(poly_id)
            
            # Create feature with POLYGON_ID, PDP_ID, address count
            feat = QgsFeature()
            feat.setGeometry(net["polygon_geom"])
            feat.setAttributes([
                poly_id,
                net["pdp_id"],
                net["address_count"],
            ])
            sink.addFeature(feat)
            written += 1
        
        self._report(f"Wrote {written} polygon features to sink")

    def write_pdps_to_sink(self, sink):
        """
        Write all PDPs to an output sink.
        
        Args:
            sink: QgsFeatureSink for PDP point features
        """
        written = 0
        for poly_id in self._registry.polygon_ids():
            net = self._registry.get_network(poly_id)
            
            # Create PDP feature
            feat = QgsFeature()
            feat.setGeometry(net["pdp_geom"])
            feat.setAttributes([
                net["pdp_id"],
                poly_id,
                net["address_count"],
            ])
            sink.addFeature(feat)
            written += 1
        
        self._report(f"Wrote {written} PDP features to sink")

    def get_registry(self):
        """Return the underlying NetworkRegistry for downstream algorithms."""
        return self._registry

    def summary(self):
        """Get a summary of registered networks."""
        return self._registry.summary()

    def _report(self, msg):
        """Report a message (optional feedback)."""
        if self.feedback:
            self.feedback.pushInfo(msg)
