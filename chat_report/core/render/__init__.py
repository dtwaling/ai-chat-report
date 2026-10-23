"""Output renderers for version 2 chat reports."""

from chat_report.core.render.digest import render_agent_digest
from chat_report.core.render.json_v2 import (
    assert_v2_report_conforms,
    render_aggregate_json,
    render_diff_json,
    render_single_session_json,
)
from chat_report.core.render.markdown import (
    render_aggregate_md,
    render_diff_md,
    render_single_session_md,
)

__all__ = (
    "assert_v2_report_conforms",
    "render_agent_digest",
    "render_aggregate_md",
    "render_aggregate_json",
    "render_diff_md",
    "render_diff_json",
    "render_single_session_md",
    "render_single_session_json",
)
