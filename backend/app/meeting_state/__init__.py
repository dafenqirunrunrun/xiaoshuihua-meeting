"""__init__ for app.meeting_state — leave empty.

Deliberately import-free: `service` imports the transcript domain and the LLM
layer, so re-exporting it here would make `import app.meeting_state` drag the
whole stack in and invite circular imports between the transcript and
meeting_state packages. Import the submodule you need explicitly.
"""
