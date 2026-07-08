# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
from qgis.core import QgsPointXY

def quantize_xy(x: float, y: float, eps_val: float = 0.01):
    """Round coordinates to a grid of size 'eps_val' for stable graph keys."""
    return (round(x / eps_val) * eps_val, round(y / eps_val) * eps_val)

def qkey_point(pt: QgsPointXY, eps_val: float = 0.01):
    return quantize_xy(pt.x(), pt.y(), eps_val)


def grid_key(x: float, y: float, cell: float) -> tuple:
    return (int(x // cell), int(y // cell))

def build_node_grid(nodes, cell: float):
    grid = {}
    for n in nodes:
        k = grid_key(n[0], n[1], cell)
        grid.setdefault(k, []).append(n)
    return grid

def neighbors_in_grid(grid, kx, ky, radius: int = 1):
    for ix in range(kx - radius, kx + radius + 1):
        for iy in range(ky - radius, ky + radius + 1):
            if (ix, iy) in grid:
                for n in grid[(ix, iy)]:
                    yield n
