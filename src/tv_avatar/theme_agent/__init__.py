"""Standalone server side of the weather-backdrop agent (frontend/src/weather/).

Nothing here imports, or is imported by, the avatar backend: it is its own small
FastAPI app on its own port, so the TV app's weather backdrops need no change to
`tv_avatar.app` and no session, token or Pipecat machinery.
"""
