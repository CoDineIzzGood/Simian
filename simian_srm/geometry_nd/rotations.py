import numpy as np

def rotate4d_xy_zw(V4: np.ndarray, theta: float, phi: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    R_xy = np.array([[ c,-s,0,0],
                     [ s, c,0,0],
                     [ 0, 0,1,0],
                     [ 0, 0,0,1]], dtype=float)
    cz, sz = np.cos(phi), np.sin(phi)
    R_zw = np.array([[1,0, 0, 0],
                     [0,1, 0, 0],
                     [0,0, cz,-sz],
                     [0,0, sz, cz]], dtype=float)
    return (V4 @ (R_xy @ R_zw).T)
