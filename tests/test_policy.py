"""Policy loader + the capability-doc invariants we care about."""
from dsa_operator.policy import load_policy


def test_loads_shipped_policy():
    pol = load_policy()
    assert pol.version >= 1
    assert pol.mode in ("shadow", "live")


def test_readonly_actions_present():
    pol = load_policy()
    for a in ("get_fleet_status", "get_array_pointing", "get_mon",
              "query_injections"):
        assert pol.is_read_only_action(a)


def test_code_update_and_policy_edit_always_approval():
    pol = load_policy()
    for a in ("update_fleet_code", "set_policy"):
        assert pol.actions[a]["target"] == "approval"
        assert pol.actions[a]["commissioning"] == "approval"


def test_pointing_envelope_uses_ovro_latitude():
    pol = load_policy()
    assert pol.pointing["lat_ovro_deg"] == 37.23
    assert pol.pointing["el_min_deg"] < pol.pointing["el_max_deg"]


def test_read_and_control_namespaces_disjoint():
    pol = load_policy()
    assert pol.read_only.isdisjoint(set(pol.actions))
