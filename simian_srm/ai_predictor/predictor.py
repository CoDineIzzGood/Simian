import math, random
from typing import Dict, Any, Tuple

class AnglePredictor:
    def __init__(self):
        self.t = 0.0

    def next_delta(self, priors: Dict[str, Any] | None=None) -> Tuple[float,float,float]:
        self.t += 0.07
        base = 0.03
        dtheta = base*math.sin(self.t) + random.uniform(-0.005, 0.005)
        dphi   = base*math.cos(self.t*0.9) + random.uniform(-0.005, 0.005)
        sigma = 0.02 + 0.01*abs(math.sin(self.t*0.5))
        if priors and isinstance(priors.get('bias'), list):
            words = {w.lower() for w in priors['bias']}
            if 'stable' in words or 'conserve' in words:
                sigma *= 0.5
            if 'rotate' in words or 'so(4)' in words:
                dtheta *= 1.2; dphi *= 1.2
        return dtheta, dphi, sigma
