"""
agent.py - Bridge / Facade to priority_engine.py
=================================================
Member 3 Deliverable: Decision and Optimization Agent

This module re-exports the core decision engine functions and configuration
from priority_engine.py to ensure full backward compatibility with team members
importing either 'agent' or 'priority_engine'.
"""

from priority_engine import (
    DEFAULT_CONFIG,
    extract_structured_info,
    evaluate_appointment,
    rank_appointments,
    propose_schedule_optimizations,
    get_priority_engine_stats,
)

__all__ = [
    "DEFAULT_CONFIG",
    "extract_structured_info",
    "evaluate_appointment",
    "rank_appointments",
    "propose_schedule_optimizations",
    "get_priority_engine_stats",
]

if __name__ == "__main__":
    from priority_engine import _run_demo
    _run_demo()
