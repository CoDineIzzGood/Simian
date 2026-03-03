from dataclasses import dataclass

@dataclass
class SRMState:
    theta: float = 0.0
    phi: float = 0.0
    uncertainty: float = 0.0

STATE = SRMState()
