"""cua -- computer-use automation for legacy back-office software.

The model *discovers* a flow once by driving a real UI (``cua.discovery``).
The successful run is frozen into a typed, versioned ``CapabilityArtifact``
(``cua.schema``). A deterministic engine *replays* that artifact with no LLM in
the loop (``cua.replay``), classifying runtime outcomes against the artifact's
own taxonomy. Guardrails (``cua.guardrails``) gate every action; evidence
(``cua.evidence``) records every step; escalation (``cua.escalation``) hands the
live session to a human when the system is stuck.
"""

__version__ = "0.1.0"
