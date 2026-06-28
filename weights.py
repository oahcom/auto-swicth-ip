#!/usr/bin/env python3
"""
Shared weight computation module.

Single source of truth for weight calculation used by both:
- proxy_pool.py (OKZProxyPool)
- daemon.py (daemon main loop)

Weight formula:
    weight = ip_score * delay_score * freq_score

Where:
    ip_score: 1.0 (has country), 0.5 (has city only), 0.1 (no geo data)
    delay_score: exp(-delay_ms / DELAY_TAU)  if delay > 0, else DEAD_DELAY_SCORE
    freq_score: 1.0 / sqrt(usage_count + 1)  -- gentler penalty than linear
    MIN_WEIGHT floor: 0.0001
"""

import math

IP_SCORE_HAS_COUNTRY = 1.0
IP_SCORE_HAS_CITY = 0.5
IP_SCORE_NONE = 0.1
DEAD_PENALTY = 0.01
MIN_WEIGHT = 0.0001
DEAD_DELAY_SCORE = 0.001  # 无延迟数据（死节点）的延迟分
DELAY_TAU = 300  # ms, 控制延迟衰减速度: 300ms→0.37, 600ms→0.14, 1000ms→0.036


def compute_node_weight(
    ip_score: float,
    delay_ms: int | None,
    usage_count: int,
) -> float:
    """Compute weight for a single node.

    Args:
        ip_score: 0.1 to 1.0 (from node_info table: country=1.0, city=0.5, none=0.1)
        delay_ms: delay in milliseconds from sing-box delay test, or None if unknown
        usage_count: number of times this node has been selected

    Returns:
        Weight >= MIN_WEIGHT
    """
    # Delay score: exp decay — 100ms→0.72, 300ms→0.37, 1000ms→0.036
    if delay_ms is not None and delay_ms > 0:
        delay_score = math.exp(-delay_ms / DELAY_TAU)
    else:
        delay_score = DEAD_DELAY_SCORE

    # Usage frequency penalty: more calls = lower weight
    # Use sqrt for gentler penalty: calls=0 -> 1.0, calls=10 -> 0.3, calls=100 -> 0.1
    freq_score = 1.0 / math.sqrt(usage_count + 1)

    weight = ip_score * delay_score * freq_score
    return max(weight, MIN_WEIGHT)


def compute_all_weights(
    nodes: list[str],
    ip_scores: dict[str, float],
    delays: dict[str, int],
    usage_counts: dict[str, int],
) -> list[tuple[str, float]]:
    """Compute weights for all nodes, returning list of (node, weight) tuples sorted by weight desc."""
    weights = []
    for node in nodes:
        ip_score = ip_scores.get(node, IP_SCORE_NONE)
        delay = delays.get(node)
        calls = usage_counts.get(node, 0)
        w = compute_node_weight(ip_score, delay, calls)
        weights.append((node, w))

    # Sort by weight descending for debugging/logging
    weights.sort(key=lambda x: -x[1])
    return weights


def compute_ip_score(country: str | None, city: str | None) -> float:
    """Compute IP quality score from geo data."""
    if country:
        return IP_SCORE_HAS_COUNTRY
    if city:
        return IP_SCORE_HAS_CITY
    return IP_SCORE_NONE