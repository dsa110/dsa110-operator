"""Local, non-LLM monitoring loops (Phase 5).

The standing health monitor, periodic injection health-checks, and
auto-recovery playbook. These run as deterministic local code and feed the
agent (and the web console) only compact, redacted summaries — never raw
telemetry. Every mutating action they propose goes through the same
``ControlEngine`` gauntlet as a human-driven one.
"""
from dsa_operator.monitor.health import (
    LEVEL_ALERT,
    LEVEL_OK,
    LEVEL_WARN,
    HealthFinding,
    HealthReport,
    HealthThresholds,
    evaluate_health,
)
from dsa_operator.monitor.injection import (
    DEFAULT_PROBE,
    InjectionHealthCheck,
    InjectionResult,
)
from dsa_operator.monitor.recovery import RecoveryPlaybook, RecoveryProposal
from dsa_operator.monitor.supervisor import (
    AutonomyConfig,
    AutonomySupervisor,
    SupervisorTick,
)

__all__ = [
    "LEVEL_OK", "LEVEL_WARN", "LEVEL_ALERT",
    "HealthThresholds", "HealthFinding", "HealthReport", "evaluate_health",
    "RecoveryPlaybook", "RecoveryProposal",
    "InjectionHealthCheck", "InjectionResult", "DEFAULT_PROBE",
    "AutonomyConfig", "AutonomySupervisor", "SupervisorTick",
]
