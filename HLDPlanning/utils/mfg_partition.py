"""Deterministic MFG serving-area partitioning by capacity and road reach.

The helper is independent of QGIS geometry APIs: callers provide household
loads per premise polygon and shortest road-network distances between them.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Dict, Hashable, Iterable, List, Mapping, Sequence, Set, Tuple


MFG_MIN_HH = 2000
MFG_TARGET_HH = 3000
MFG_MAX_HH = 4000
MFG_MAX_ROAD_M = 3000.0


def _hh(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _components(nodes: Set[Hashable], adjacency: Mapping[Hashable, Set[Hashable]]) -> List[List[Hashable]]:
    remaining = set(nodes)
    components: List[List[Hashable]] = []
    while remaining:
        start = min(remaining, key=str)
        remaining.remove(start)
        component = [start]
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for neighbour in sorted(adjacency.get(current, set()) & remaining, key=str):
                remaining.remove(neighbour)
                component.append(neighbour)
                queue.append(neighbour)
        components.append(sorted(component, key=str))
    return components


def _farthest_seeds(component: Sequence[Hashable], adjacency: Mapping[Hashable, Set[Hashable]],
                    count: int, loads: Mapping[Hashable, int]) -> List[Hashable]:
    """Choose graph-dispersed seeds, with load and stable ID as tie breakers."""
    first = min(component, key=lambda node: (-loads[node], str(node)))
    seeds = [first]
    distance_to_seed: Dict[Hashable, int] = {first: 0}
    while len(seeds) < count:
        source = seeds[-1]
        queue = deque([(source, 0)])
        seen = {source}
        while queue:
            node, distance = queue.popleft()
            distance_to_seed[node] = min(distance_to_seed.get(node, distance), distance)
            for neighbour in adjacency.get(node, set()):
                if neighbour in component and neighbour not in seen:
                    seen.add(neighbour)
                    queue.append((neighbour, distance + 1))
        remaining = [node for node in component if node not in seeds]
        next_seed = min(
            remaining,
            key=lambda node: (
                -min(
                    _graph_distance(node, seed, adjacency, set(component))
                    for seed in seeds
                ),
                -loads[node],
                str(node),
            ),
        )
        seeds.append(next_seed)
    return seeds


def _graph_distance(start: Hashable, goal: Hashable,
                    adjacency: Mapping[Hashable, Set[Hashable]],
                    allowed: Set[Hashable]) -> int:
    if start == goal:
        return 0
    queue = deque([(start, 0)])
    seen = {start}
    while queue:
        node, distance = queue.popleft()
        for neighbour in adjacency.get(node, set()):
            if neighbour not in allowed or neighbour in seen:
                continue
            if neighbour == goal:
                return distance + 1
            seen.add(neighbour)
            queue.append((neighbour, distance + 1))
    return -1


def _partition_component(component: Sequence[Hashable],
                         adjacency: Mapping[Hashable, Set[Hashable]],
                         loads: Mapping[Hashable, int], target_hh: int,
                         max_hh: int) -> List[List[Hashable]]:
    total = sum(loads[node] for node in component)
    # Keep cluster loads near the target, but never choose fewer clusters than
    # the hard cap requires. The target is a balancing preference, not a cap.
    count_by_target = max(1, int(math.floor(total / float(target_hh) + 0.5)))
    count_by_cap = max(1, int(math.ceil(total / float(max_hh))))
    cluster_count = min(len(component), max(count_by_target, count_by_cap))
    seeds = _farthest_seeds(component, adjacency, cluster_count, loads)
    clusters: List[Set[Hashable]] = [{seed} for seed in seeds]
    cluster_loads = [loads[seed] for seed in seeds]
    unassigned = set(component) - set(seeds)
    local_target = total / float(cluster_count)

    while unassigned:
        choices: List[Tuple[float, int, str, int, Hashable]] = []
        for node in unassigned:
            adjacent_clusters = [
                idx for idx, cluster in enumerate(clusters)
                if adjacency.get(node, set()) & cluster
            ]
            for idx in adjacent_clusters:
                if cluster_loads[idx] + loads[node] > max_hh:
                    continue
                proposed = cluster_loads[idx] + loads[node]
                # Prefer a balanced projected load, then a lighter current
                # area, and use IDs for deterministic behavior.
                choices.append((abs(proposed - local_target), cluster_loads[idx],
                                str(node), idx, node))
        if not choices:
            # Whole-polygon capacity can prevent an assignment even when there
            # are fewer seeds than features. Start a new area at the frontier
            # of an existing cluster so this new cluster is still contiguous.
            frontier = [
                node for node in unassigned
                if any(adjacency.get(node, set()) & cluster for cluster in clusters)
            ]
            if not frontier:
                raise ValueError("connected MFG component has no assignable frontier")
            seed = min(frontier, key=lambda node: (-loads[node], str(node)))
            clusters.append({seed})
            cluster_loads.append(loads[seed])
            unassigned.remove(seed)
            local_target = total / float(len(clusters))
            continue
        _score, _load, _id, idx, node = min(choices)
        clusters[idx].add(node)
        cluster_loads[idx] += loads[node]
        unassigned.remove(node)

    return [sorted(cluster, key=str) for cluster in clusters]


def partition_mfg_service_areas(
    polygon_households: Mapping[Hashable, Any],
    road_distances: Mapping[Tuple[Hashable, Hashable], float],
    *,
    min_hh: int = MFG_MIN_HH,
    target_hh: int = MFG_TARGET_HH,
    max_hh: int = MFG_MAX_HH,
    max_road_m: float = MFG_MAX_ROAD_M,
) -> List[Dict[str, Any]]:
    """Group premise polygons into MFG service areas by load and road reach.

    ``road_distances`` contains shortest-path distances between representative
    points snapped to the road graph. Polygon topology and polygon count do not
    create MFGs: a group grows to the 4,000-HH cap or 3 km reach limit; the 3,000
    target is informational and never causes an early split. The 2,000 lower
    capacity is a review threshold; a small isolated catchment is retained and
    flagged instead of being merged across an unreachable road gap.
    """
    if min_hh <= 0 or target_hh <= 0 or max_hh <= 0 or max_road_m <= 0:
        raise ValueError("MFG capacities and road reach must be positive")
    if not min_hh <= target_hh <= max_hh:
        raise ValueError("MFG capacity must satisfy min_hh <= target_hh <= max_hh")

    loads = {key: _hh(value) for key, value in polygon_households.items()}
    remaining = set(loads)
    groups: List[List[Hashable]] = []

    def distance(left, right):
        value = road_distances.get((left, right))
        if value is None and left == right:
            return 0.0
        try:
            value = float(value)
        except (TypeError, ValueError):
            return math.inf
        return value if math.isfinite(value) and value >= 0 else math.inf

    while remaining:
        remaining_hh = sum(loads[node] for node in remaining)
        required_groups = max(1, int(math.ceil(remaining_hh / float(max_hh))))
        capacity_goal = int(math.ceil(remaining_hh / float(required_groups)))
        choices = []

        for candidate in remaining:
            reachable = [
                node for node in remaining
                if distance(candidate, node) <= max_road_m
            ]
            reachable.sort(key=lambda node: (
                distance(candidate, node), -loads[node], str(node),
            ))
            cluster = []
            cluster_hh = 0
            for node in reachable:
                if cluster_hh + loads[node] <= max_hh:
                    cluster.append(node)
                    cluster_hh += loads[node]
                    if cluster_hh >= capacity_goal:
                        break

            max_range = max((distance(candidate, node) for node in cluster), default=0.0)
            total_distance = sum(distance(candidate, node) for node in cluster)
            shortfall = max(0, capacity_goal - cluster_hh)
            choices.append((
                shortfall, abs(cluster_hh - capacity_goal), -cluster_hh,
                max_range, total_distance, str(candidate), candidate, cluster,
            ))

        (_shortfall, _balance, _load, _range, _distance, _id,
         _anchor, cluster) = min(choices)
        if not cluster:
            # Preserve an indivisible premise above the MFG cap as an explicit
            # review item instead of silently losing it.
            cluster = [min(remaining, key=lambda node: (str(node),))]
        groups.append(cluster)
        remaining.difference_update(cluster)

    result = []
    groups.sort(key=lambda group: min(str(node) for node in group))
    for index, members in enumerate(groups, 1):
        valid_anchors = [
            candidate for candidate in members
            if all(distance(candidate, member) <= max_road_m for member in members)
        ]
        medoid = min(
            valid_anchors or members,
            key=lambda candidate: (
                max(distance(candidate, member) for member in members),
                sum(distance(candidate, member) for member in members),
                str(candidate),
            ),
        )
        count = sum(loads[node] for node in members)
        range_ok = all(distance(medoid, node) <= max_road_m for node in members)
        status = "OVER_CAPACITY" if count > max_hh else (
            "UNDER_MINIMUM" if count < min_hh else "OK"
        )
        warnings = []
        if count > max_hh:
            warnings.append(f"{count} HH exceeds the {max_hh} HH MFG maximum")
        if count < min_hh:
            warnings.append(f"{count} HH is below the {min_hh} HH MFG minimum")
        if not range_ok:
            warnings.append(f"some premises exceed {max_road_m:g} m road-network reach")
        result.append({
            "mfg_id": f"MFG{index:05d}",
            "polygon_keys": tuple(sorted(members, key=str)),
            "seed_polygon": medoid,
            "hh_count": count,
            "capacity_status": status if range_ok else f"{status};REACH_REVIEW",
            "capacity_warning": "; ".join(warnings),
            "review": int(bool(warnings)),
            "target_hh": target_hh,
            "min_hh": min_hh,
            "max_hh": max_hh,
            "max_road_m": max_road_m,
            "range_m": (
                max(distance(medoid, node) for node in members)
                if all(math.isfinite(distance(medoid, node)) for node in members)
                else None
            ),
        })
    return result


def partition_mfg_areas(
    polygon_households: Mapping[Hashable, Any],
    adjacency_pairs: Iterable[Tuple[Hashable, Hashable]],
    *,
    target_hh: int = MFG_TARGET_HH,
    max_hh: int = MFG_MAX_HH,
) -> List[Dict[str, Any]]:
    """Legacy adjacency-based partitioner retained for explicit old callers.

    New HLD MFG allocation uses :func:`partition_mfg_service_areas`; this
    helper remains only for backwards-compatible callers and old tests.
    """
    if target_hh <= 0 or max_hh <= 0:
        raise ValueError("MFG target and maximum must be positive")
    loads = {key: _hh(value) for key, value in polygon_households.items()}
    adjacency: Dict[Hashable, Set[Hashable]] = {key: set() for key in loads}
    for left, right in adjacency_pairs:
        if left not in loads or right not in loads or left == right:
            continue
        adjacency[left].add(right)
        adjacency[right].add(left)

    groups: List[Tuple[List[Hashable], bool]] = []
    oversized = {key for key, count in loads.items() if count > max_hh}
    for key in sorted(oversized, key=str):
        groups.append(([key], True))

    ordinary = set(loads) - oversized
    for component in _components(ordinary, adjacency):
        groups.extend(
            (cluster, False)
            for cluster in _partition_component(component, adjacency, loads, target_hh, max_hh)
        )

    groups.sort(key=lambda item: min(str(key) for key in item[0]))
    result = []
    for index, (polygon_keys, over_capacity) in enumerate(groups, 1):
        count = sum(loads[key] for key in polygon_keys)
        result.append({
            "mfg_id": f"MFG{index:05d}",
            "polygon_keys": tuple(sorted(polygon_keys, key=str)),
            "hh_count": count,
            "capacity_status": "OVER_CAPACITY" if over_capacity else "OK",
            "capacity_warning": (
                f"Indivisible service polygon exceeds the {max_hh} HH MFG policy cap "
                f"({count} HH); split/revise the service-area polygon or approve a "
                "documented exception."
                if over_capacity else ""
            ),
            "review": 1 if over_capacity else 0,
            "target_hh": target_hh,
            "max_hh": max_hh,
        })
    return result
