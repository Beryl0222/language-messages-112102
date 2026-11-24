"""本地双语导览短句工作台。

资料只读；所有判断都由 ``data`` 下的交换资料推导，不连接外部服务。
"""

from .model import (
    DataError,
    Screen,
    Tables,
    Template,
    VisitRequest,
    display_width,
    load_requests,
    load_tables,
)
from .semantics import Analysis, Issue, analyze_copy
from .selection import RequestPlan, plan_request, plan_requests, render
from .submission import PairAudit, audit_submission, delivery_report, preview_regression

__all__ = [
    "Analysis",
    "DataError",
    "Issue",
    "PairAudit",
    "RequestPlan",
    "Screen",
    "Tables",
    "Template",
    "VisitRequest",
    "analyze_copy",
    "audit_submission",
    "delivery_report",
    "display_width",
    "load_requests",
    "load_tables",
    "preview_regression",
    "render",
    "plan_request",
    "plan_requests",
]
