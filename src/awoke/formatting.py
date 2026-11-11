from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

LOG = logging.getLogger(__name__)


def format_tsunami_arrival_time(value: Any) -> str:
    text = str(value)
    try:
        return datetime.strptime(text, "%Y/%m/%d %H:%M:%S").strftime("%H:%M")
    except ValueError:
        LOG.warning("Unexpected tsunami arrival time format; displaying original value")
        return text
