#!/usr/bin/env python3
"""
Shared weight computation module.

Single source of truth for weight calculation used by both:
- proxy_pool.py (OKZProxyPool)
- daemon.py (daemon main loop)

Adaptive strategy:
    mode="normal":    delay优先, 同延迟按调用次数 → 快节点优先用完
    mode="pressure":  调用次数优先, 同次数按延迟 → 均匀分散

Weight formula (rank-based):
    sorted_nodes = sort by (mode key)
    weight[i] = 1 / (rank + 1)  → rank 0 gets highest probability
"""

import math

IP_SCORE_HAS_COUNTRY = 1.0
IP_SCORE_HAS_CITY = 0.5
IP_SCORE_NONE = 0.1
DEAD_PENALTY = 0.01
MIN_WEIGHT = 0.0001
DEAD_DELAY_SCORE = 0.001  # 无延迟数据（死节点）的延迟分
DELAY_TAU = 300  # ms, 控制延迟衰减速度: 300ms→0.37, 600ms→0.14, 1000ms→0.036


def compute_delay_score(delay_ms: int | None) -> float:
    """延迟评分：exp 衰减，100ms→0.72, 300ms→0.37, 1000ms→0.036"""
    if delay_ms is not None and delay_ms > 0:
        return math.exp(-delay_ms / DELAY_TAU)
    return DEAD_DELAY_SCORE


def compute_node_weight(
    ip_score: float,
    delay_ms: int | None,
    usage_count: int,
) -> float:
    """Compute weight for a single node (legacy multiplicative formula)."""
    delay_score = compute_delay_score(delay_ms)
    freq_score = 1.0 / math.sqrt(usage_count + 1)
    weight = ip_score * delay_score * freq_score
    return max(weight, MIN_WEIGHT)


def compute_all_weights(
    nodes: list[str],
    ip_scores: dict[str, float],
    delays: dict[str, int],
    usage_counts: dict[str, int],
    mode: str = "normal",
) -> list[tuple[str, float]]:
    """Compute weights for all nodes using rank-based adaptive strategy.

    mode="normal":    延迟优先 (delay_score desc, calls asc)
    mode="pressure":  调用次数优先 (calls asc, delay_score desc)

    Returns list of (node, weight) sorted by weight desc.
    """
    # Build sortable list with scores
    scored = []
    for node in nodes:
        ip_score = ip_scores.get(node, IP_SCORE_NONE)
        delay = delays.get(node)
        calls = usage_counts.get(node, 0)
        ds = compute_delay_score(delay)
        scored.append((node, ds, calls, ip_score))

    # Sort by adaptive key
    if mode == "pressure":
        # 均匀分散: 调用次数少的优先, 同次数延迟低的优先
        scored.sort(key=lambda x: (-x[1], x[2], x[3]))
    else:
        # 正常: 延迟低的优先, 同延迟调用次数少的优先
        scored.sort(key=lambda x: (x[1], -x[2], -x[3]))

    # Rank-based weights: rank 0 gets highest weight
    weights = []
    for rank, (node, ds, calls, ip_score) in enumerate(scored):
        w = 1.0 / (rank + 1)
        weights.append((node, max(w, MIN_WEIGHT)))

    return weights


def compute_ip_score(country: str | None, city: str | None) -> float:
    """Compute IP quality score from geo data."""
    if country:
        return IP_SCORE_HAS_COUNTRY
    if city:
        return IP_SCORE_HAS_CITY
    return IP_SCORE_NONE
