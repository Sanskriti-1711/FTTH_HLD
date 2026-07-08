# -*- coding: utf-8 -*-
"""
utils.graph
Graph construction and routing helpers for FTTH Duct/Trench algorithms
"""

import heapq
from collections import defaultdict

def add_edge(adj, edge_geom, edge_len, u, v, geom):
    """Add an undirected edge to the graph."""
    if u == v:
        return
    seg_id = tuple(sorted([u, v]))
    L = geom.length()
    if L <= 1e-6:
        return
    if seg_id in edge_len and edge_len[seg_id] <= L + 1e-9:
        return
    edge_geom[seg_id] = geom
    edge_len[seg_id] = L
    adj[u].append((v, seg_id, L))
    adj[v].append((u, seg_id, L))


def dijkstra_with_parents(root, adj):
    """Standard Dijkstra returning distances and parent map."""
    dist = {root: 0.0}
    parent = {}
    heap = [(0.0, str(root), root)]
    while heap:
        d, t, u = heapq.heappop(heap)
        if d > dist.get(u, 1e18) + 1e-9:
            continue
        for v, seg_id, w in adj.get(u, []):
            cand = d + w
            better = cand + 1e-9 < dist.get(v, 1e18)
            tie = abs(cand - dist.get(v, 1e18)) <= 1e-9 and str(seg_id) < str((parent.get(v) or (None, "zz"))[1])
            if better or tie:
                dist[v] = cand
                parent[v] = (u, seg_id)
                heapq.heappush(heap, (cand, str(seg_id), v))
    return dist, parent


def reconstruct_path(parent, goal, root):
    """Reconstruct path from Dijkstra parent map."""
    path = []
    cur = goal
    while cur != root:
        if cur not in parent:
            return None
        prev, seg_id = parent[cur]
        path.append(seg_id)
        cur = prev
    path.reverse()
    return path
