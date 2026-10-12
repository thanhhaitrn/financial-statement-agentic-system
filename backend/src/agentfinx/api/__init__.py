"""Local web app for AgentFinX: JSON API and queue workers behind the Next.js UI.

Three Python processes share one machine: ``agentfinx serve`` (the API, never
runs the model), ``agentfinx worker --queue chat`` and ``agentfinx worker
--queue import``; the UI in ``web/`` proxies ``/api`` to the API. See
``docs/web-app.md``.
"""
