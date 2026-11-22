"""Tests for SoC energy accounting and the charger counter."""

import pytest

from custom_components.ford_ev.accounting import SocAccountingState, apply_soc_change, charger_increment

CAP = 60.0


def test_first_soc_is_baseline():
    st = apply_soc_change(SocAccountingState(), 50, CAP, plugged=False, home=True)
    assert (st.last_soc, st.energy_used, st.energy_added) == (50, 0, 0)


def test_drop_counts_used():
    st = apply_soc_change(SocAccountingState(last_soc=50), 40, CAP, False, True)
    assert st.energy_used == pytest.approx(6.0)


def test_plugged_rise_counts_added_and_home():
    st = apply_soc_change(SocAccountingState(last_soc=40, regen_credit=2), 90, CAP, True, True)
    assert st.energy_added == pytest.approx(30.0)
    assert st.energy_added_home == pytest.approx(30.0)
    assert st.regen_credit == 0


def test_plugged_rise_away_not_home():
    st = apply_soc_change(SocAccountingState(last_soc=40), 50, CAP, True, False)
    assert st.energy_added == pytest.approx(6.0)
    assert st.energy_added_home == 0


def test_large_unplugged_rise_counts_added():
    st = apply_soc_change(SocAccountingState(last_soc=40), 45, CAP, False, False)
    assert st.energy_added == pytest.approx(3.0)


def test_small_unplugged_rise_is_regen_credit_then_offsets_drop():
    st = apply_soc_change(SocAccountingState(last_soc=40), 42, CAP, False, False)
    assert st.regen_credit == 2
    assert st.energy_added == 0
    st = apply_soc_change(st, 37, CAP, False, False)
    assert st.energy_used == pytest.approx(1.8)
    assert st.regen_credit == 0


def test_regen_credit_capped():
    st = apply_soc_change(SocAccountingState(last_soc=10, regen_credit=99), 13, CAP, False, False)
    assert st.regen_credit == 100


def test_input_state_not_mutated():
    before = SocAccountingState(last_soc=50)
    apply_soc_change(before, 40, CAP, False, True)
    assert before.last_soc == 50 and before.energy_used == 0


@pytest.mark.parametrize(("last", "reading", "expected"), [(None, 3.0, 0.0), (1.0, 2.5, 1.5), (3.0, 3.0, 0.0), (3.0, 0.5, 0.5)])
def test_charger_increment(last, reading, expected):
    assert charger_increment(last, reading) == pytest.approx(expected)
