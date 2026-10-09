"""水华队长 AI Referee Sidecar (ADR-002).

An ON-DEMAND, user-triggered discussion referee that reads persisted FINAL
transcript and produces one structured report per request.

Boundary (hard, enforced by convention and asserted in tests):

  - This package NEVER imports `app.meeting_state`, and `app.meeting_state`
    NEVER imports this package. The two agents share nothing but the read-only
    `transcript_segment` fact source.
  - It never writes `transcript_segment`, never touches the meeting four-state
    machine, and never participates in the Finalizer / Summary path.
  - Its failures are contained: a broken referee cannot affect FINAL ingest,
    SSE broadcast, meeting state or summary generation.

`__init__` stays import-light (no submodule imports) so importing the package
can never pull in the LLM layer or the DB — the same discipline the other
packages in this codebase follow.
"""

__all__: list[str] = []
