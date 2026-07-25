"""The agent's action space — typed, isolated, testable tools over the golden
sources (BQ period_features, GCS narrative, FMP fundamentals, scoring outputs).

Every tool obeys the ADR-2 contract standard: complete/non-contradictory inputs,
illegal states unrepresentable (status enum), and a provenance envelope so the
judge can verify grounding. `feature_history` is the reference pattern.
"""
