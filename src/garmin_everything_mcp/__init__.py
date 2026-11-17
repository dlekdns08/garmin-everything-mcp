"""garmin-everything-mcp: the other 70% of your Garmin.

An MCP server that introspects ``garminconnect.Garmin`` at startup and turns
every public method into a tool, skipping the ones the popular ``garmin_mcp``
server already exposes so both can run side by side.
"""

__version__ = "0.1.0"
