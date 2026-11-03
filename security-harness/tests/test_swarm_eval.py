"""Swarm evaluation: confirmed-per-dollar, the width-yield curve, and the scale policy."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))
import swarm_eval as se  # noqa: E402


def test_confirmed_per_dollar_basic_and_zero_cost():
    assert se.confirmed_per_dollar(6, 3.0) == 2.0
    assert se.confirmed_per_dollar(5, 0) == 0.0


def test_width_yield_curve_aggregates_and_sorts():
    runs = [{"width": 8, "confirmed": 4, "cost": 2.0},
            {"width": 8, "confirmed": 2, "cost": 2.0},
            {"width": 32, "confirmed": 10, "cost": 10.0}]
    curve = se.width_yield_curve(runs)
    assert [p["width"] for p in curve] == [8, 32]
    assert curve[0]["confirmed"] == 6 and curve[0]["confirmed_per_dollar"] == 1.5


def test_choose_width_picks_the_plateau():
    curve = se.width_yield_curve([
        {"width": 8, "confirmed": 4, "cost": 2.0},     # 2.0/$
        {"width": 32, "confirmed": 20, "cost": 8.0},   # 2.5/$  (still climbing)
        {"width": 128, "confirmed": 40, "cost": 40.0}, # 1.0/$  (worse -> stop at 32)
    ])
    choice = se.choose_width(curve)
    assert choice["width"] == 32


def test_choose_width_still_climbing_returns_widest():
    curve = se.width_yield_curve([
        {"width": 8, "confirmed": 2, "cost": 2.0},     # 1.0/$
        {"width": 32, "confirmed": 16, "cost": 8.0},   # 2.0/$ (climbing)
    ])
    assert se.choose_width(curve)["width"] == 32


def test_choose_width_no_data():
    assert se.choose_width([])["width"] == 0
