import numpy as np

def project_4d_to_3d(V4, d_w: float=3.0):
    V4 = np.asarray(V4, dtype=float)
    w = V4[:,3]
    denom = (1.0 - w/float(d_w))
    denom[denom == 0] = 1e-6
    factor = 1.0/denom
    return V4[:,:3] * factor[:,None]
