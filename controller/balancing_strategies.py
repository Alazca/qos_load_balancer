"""
balancing_strategies.py
=======================
Pluggable load-balancing policies for the Prometheus QoS controller.

Adding a new strategy is two lines: subclass `BalancingStrategy`, register
it via `@register("name")`. The Ryu app pulls strategies by name from a
single dispatch dict, so swapping policies during evaluation is a one-line
change in the Ryu config (or `kwargs` at construction time).

Each `select()` call gets the *full* fleet of backends with their current
QoS metrics; the strategy returns the chosen backend. This separation
keeps measurement (in `metrics_collector.py`) decoupled from policy
(here), which matters for the trade-off study in your project plan.
"""

# Standard library
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Type
import itertools
import math
import random


# ---------------------------------------------------------------------------
#  Backend descriptor
# ---------------------------------------------------------------------------
@dataclass
class Backend:
    """Static + dynamic state for a single backend server."""
    name:        str          # e.g. "srv1"
    ip:          str          # e.g. "10.0.0.201"
    mac:         str          # learned at controller bring-up
    port:        int          # OF port number on s1 facing this server

    # Dynamic QoS metrics, refreshed by metrics_collector.py.
    # Defaults are deliberately neutral so a freshly-discovered backend
    # is *not* unfairly penalised before its first stats poll.
    rtt_ms:        float = 1.0    # smoothed RTT measurement
    loss_ratio:    float = 0.0    # fraction of probes lost  [0.0, 1.0]
    util_ratio:    float = 0.0    # link utilisation         [0.0, 1.0]
    active_flows:  int   = 0      # flows currently mapped here

    # Convenience property used by the QoS-weighted strategy
    @property
    def is_healthy(self) -> bool:
        """Sanity check used to skip dead backends without crashing."""
        return self.loss_ratio < 0.5 and self.rtt_ms < 1000.0


# ---------------------------------------------------------------------------
#  Strategy registry (decorator-based for elegance)
# ---------------------------------------------------------------------------
_REGISTRY: Dict[str, Type["BalancingStrategy"]] = {}


def register(name: str):
    """Class decorator: makes a strategy discoverable by string name."""
    def _wrap(cls: Type["BalancingStrategy"]) -> Type["BalancingStrategy"]:
        _REGISTRY[name] = cls
        cls.strategy_name = name
        return cls
    return _wrap


def build(name: str, **kwargs) -> "BalancingStrategy":
    """Factory: `build('qos_weighted', alpha=0.6)` -> instance."""
    if name not in _REGISTRY:
        raise ValueError(
            f"unknown strategy '{name}'; available: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name](**kwargs)


def available() -> List[str]:
    """Names of all registered strategies (for `--list-strategies` etc.)."""
    return sorted(_REGISTRY.keys())


# ---------------------------------------------------------------------------
#  Abstract base
# ---------------------------------------------------------------------------
class BalancingStrategy(ABC):
    """All concrete strategies implement `select()` and nothing else."""

    strategy_name: str = "abstract"  # overwritten by @register

    @abstractmethod
    def select(self, backends: List[Backend]) -> Backend:
        """Pick one backend from the supplied healthy list."""
        ...

    # ----- helpers shared by subclasses ------------------------------------
    @staticmethod
    def _healthy(backends: List[Backend]) -> List[Backend]:
        """Filter dead backends; fall back to the full list if all are dead
        so the load balancer never returns None (which would cause flow
        installation to crash mid-PacketIn handling)."""
        live = [b for b in backends if b.is_healthy]
        return live if live else backends


# ---------------------------------------------------------------------------
#  Concrete strategies
# ---------------------------------------------------------------------------
@register("round_robin")
class RoundRobin(BalancingStrategy):
    """Classic stateless round-robin -- the baseline against which all
    QoS-aware strategies should be compared in your evaluation."""

    def __init__(self):
        # itertools.cycle would be cleaner but we need to re-key when the
        # backend list changes (e.g. a server fails health checks), so we
        # keep an explicit counter instead.
        self._counter = itertools.count()

    def select(self, backends: List[Backend]) -> Backend:
        live = self._healthy(backends)
        return live[next(self._counter) % len(live)]


@register("least_connections")
class LeastConnections(BalancingStrategy):
    """Pick the backend with the fewest active flows. Cheap and effective
    when request durations vary; loses to QoS-weighted under heterogeneous
    backends or asymmetric link conditions."""

    def select(self, backends: List[Backend]) -> Backend:
        live = self._healthy(backends)
        # Tiebreak on RTT so that two equally-loaded backends are
        # discriminated by latency rather than insertion order.
        return min(live, key=lambda b: (b.active_flows, b.rtt_ms))


@register("random_choice")
class RandomChoice(BalancingStrategy):
    """Uniform random. Useful as a sanity bound -- anything that performs
    *worse* than random has a bug, not a strategy."""

    def select(self, backends: List[Backend]) -> Backend:
        return random.choice(self._healthy(backends))


@register("qos_weighted")
class QoSWeighted(BalancingStrategy):
    """
    Composite QoS-aware policy. Each backend gets a *score* in [0, 1] from
    a weighted blend of the three measured metrics:

        score = w_lat * (1 - latency_norm)
              + w_loss * (1 - loss_ratio)
              + w_util * (1 - util_ratio)

    The chosen backend is sampled proportional to its score, *not* taken
    as the argmax -- pure argmax is brittle (one slightly-better backend
    absorbs every new flow until it tips, then everything migrates).

    Tunables (all keyword-only) so experiments can sweep them without
    editing this file:
        w_lat, w_loss, w_util : metric weights, summed and renormalised
        latency_ceiling_ms    : RTT above this contributes 0 (clip-and-scale)
        epsilon               : minimum score floor; prevents starvation of
                                a recovering backend
    """

    def __init__(self,
                 w_lat: float  = 0.5,
                 w_loss: float = 0.3,
                 w_util: float = 0.2,
                 latency_ceiling_ms: float = 200.0,
                 epsilon: float = 0.05):

        # Renormalise weights so callers can pass un-normalised priorities.
        total = w_lat + w_loss + w_util
        if total <= 0:
            raise ValueError("at least one weight must be positive")
        self.w_lat,  self.w_loss, self.w_util = (
            w_lat / total, w_loss / total, w_util / total
        )

        self.latency_ceiling_ms = latency_ceiling_ms
        self.epsilon            = epsilon

    # ---------- internals ---------------------------------------------------
    def _score(self, b: Backend) -> float:
        """Map raw metrics onto a single fitness scalar in [epsilon, 1]."""
        # Latency: clip to ceiling, then invert so lower RTT -> higher score
        lat_norm = min(b.rtt_ms / self.latency_ceiling_ms, 1.0)
        lat_term = 1.0 - lat_norm

        # Loss and utilisation are already in [0, 1]; invert the same way
        loss_term = 1.0 - b.loss_ratio
        util_term = 1.0 - b.util_ratio

        raw = (self.w_lat  * lat_term
             + self.w_loss * loss_term
             + self.w_util * util_term)

        # Epsilon floor avoids a permanently-zero backend (e.g. one that
        # just crossed the loss threshold but has since recovered).
        return max(raw, self.epsilon)

    # ---------- public API --------------------------------------------------
    def select(self, backends: List[Backend]) -> Backend:
        live   = self._healthy(backends)
        scores = [self._score(b) for b in live]
        total  = math.fsum(scores)

        # Inverse-CDF sampling. random.choices() would also work but doing
        # it by hand keeps the dependency surface minimal (Ryu ships a
        # vanilla Python and adding choices weights pulls cpython 3.6+).
        pick = random.uniform(0.0, total)
        running = 0.0
        for backend, score in zip(live, scores):
            running += score
            if pick <= running:
                return backend

        # Numerical safety net; should be unreachable.
        return live[-1]
