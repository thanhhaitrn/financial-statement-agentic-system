"""Local web app for AgentFinX: same-origin frontend, JSON API, queue workers.

Three processes share one machine: ``agentfinx serve`` (API + static
frontend, never runs the model), ``agentfinx worker --queue chat`` and
``agentfinx worker --queue import``. See ``webapp/README.md``.
"""
