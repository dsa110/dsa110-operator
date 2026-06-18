"""Policy gate resolution: commissioning vs target, promotion, two-person."""
from __future__ import annotations

import textwrap

import pytest

from dsa_operator.policy import (
    GATE_APPROVAL,
    GATE_AUTONOMOUS,
    GATE_FORBIDDEN,
    load_policy,
)

POLICY_YAML = textwrap.dedent("""
    version: 9
    mode: shadow
    paused: false
    approval:
      ttl_seconds: 120
      two_person: [set_policy]
    read_only: [get_fleet_status]
    actions:
      fire_injection:    { target: autonomous, commissioning: autonomous, reversible: true }
      point_array:       { target: autonomous, commissioning: approval, reversible: true }
      update_fleet_code: { target: approval,   commissioning: approval, reversible: false }
      set_policy:        { target: approval,   commissioning: approval, reversible: false }
    pointing: { lat_ovro_deg: 37.23, el_min_deg: 30.0, el_max_deg: 125.0 }
""")


@pytest.fixture()
def policy_path(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text(POLICY_YAML)
    return p


def test_commissioning_gate_is_active_by_default(policy_path):
    pol = load_policy(policy_path, local_path=policy_path.parent / "nope.yaml")
    # point_array: commissioning=approval, target=autonomous -> active=approval
    assert pol.gate_for("point_array") == GATE_APPROVAL
    # fire_injection: both autonomous
    assert pol.gate_for("fire_injection") == GATE_AUTONOMOUS


def test_promotion_moves_to_target(tmp_path, policy_path):
    local = tmp_path / "local.yaml"
    local.write_text("promote: [point_array]\n")
    pol = load_policy(policy_path, local_path=local)
    assert pol.gate_for("point_array") == GATE_AUTONOMOUS  # promoted to target


def test_unknown_action_is_forbidden(policy_path):
    pol = load_policy(policy_path, local_path=policy_path.parent / "nope.yaml")
    assert pol.gate_for("rm_rf_slash") == GATE_FORBIDDEN
    assert not pol.is_control_action("rm_rf_slash")


def test_two_person_and_ttl(policy_path):
    pol = load_policy(policy_path, local_path=policy_path.parent / "nope.yaml")
    assert pol.needs_two_person("set_policy")
    assert pol.required_approvers("set_policy") == 2
    assert pol.required_approvers("point_array") == 1
    assert pol.approval_ttl_s == 120


def test_real_policy_file_loads():
    pol = load_policy()  # the repo's config/policy.yaml
    assert pol.mode == "shadow"
    assert pol.gate_for("update_fleet_code") == GATE_APPROVAL
    assert pol.needs_two_person("set_policy")
