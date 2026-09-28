# ABOUTME: Local visual deployment console for the Governed Inference Platform
# ABOUTME: Pure front-end over the answers.yaml (init --from-file) + deploy engine

"""`gip console` — localhost visual deployment console.

A single-page wizard served on 127.0.0.1 that produces the same answers
structure ``gip init --from-file`` consumes, persists profiles through the
same code path, and drives ``gip deploy`` for deployment. No logic is
duplicated here — validation, derivation, and persistence all live in
``cli/commands/init_answers.py`` and ``cli/commands/deploy.py``.
"""
