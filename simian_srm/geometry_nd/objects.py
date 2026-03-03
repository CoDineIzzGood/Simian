import numpy as np
from typing import Tuple, List

def tesseract_vertices_edges(scale: float=1.0) -> Tuple[np.ndarray, List[tuple[int,int]]]:
    verts = []
    for a in (-1,1):
        for b in (-1,1):
            for c in (-1,1):
                for d in (-1,1):
                    verts.append([a,b,c,d])
    V = np.array(verts, dtype=float) * float(scale)
    edges: List[tuple[int,int]] = []
    n = len(V)
    for i in range(n):
        for j in range(i+1, n):
            if np.sum(np.abs(V[i]-V[j])>0) == 1:
                edges.append((i,j))
    return V, edges
