"""Agentic Investigator — turns `analyst_actions` escalation suggestions into
investigations the system runs itself (see docs/agentic-investigation/).

This package is the run-time agentic layer, deliberately NOT part of the batch
`orchestrate.py` DAG (ADR-1: the agent owns its own control flow at run-time).
"""
