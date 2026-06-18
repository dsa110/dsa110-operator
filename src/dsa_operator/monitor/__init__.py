"""Local, non-LLM monitoring loops (Phase 5).

Reserved for the standing health monitor, periodic injection health-checks,
and auto-recovery playbook. These run as deterministic local code and feed
the agent only compact, redacted summaries — never raw telemetry.
"""
