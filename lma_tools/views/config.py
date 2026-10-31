"""Load and validate optional private TOML defaults."""

import math
import os
from dataclasses import dataclass

from lma_tools.config import config_path, read_section

from .fields import field_name


@dataclass(frozen=True)
class Config:
    query: tuple[str, ...] = ("collection:(etree OR taperssection)",)
    sort: str = "publicdate"
    reverse: bool = False
    timeout: float = 30.0
    view_batch_size: int = 100
    exclude_zero_views: bool = True


def load_config(path=None):
    source = config_path(path)
    if (
        not source.exists()
        and path is None
        and "LMA_TOOLS_CONFIG" not in os.environ
    ):
        return Config()
    data = read_section(source, "views")
    unknown = set(data) - set(Config.__dataclass_fields__)
    if unknown:
        raise ValueError(f"Unknown config keys: {', '.join(sorted(unknown))}")
    query = data.get("query", list(Config.query))
    if not isinstance(query, list) or not all(
        isinstance(clause, str) and clause.strip() for clause in query
    ):
        raise ValueError("query must be an array of nonempty strings")
    data["query"] = tuple(query)
    for key in ("reverse", "exclude_zero_views"):
        if key in data and not isinstance(data[key], bool):
            raise ValueError(f"{key} must be a boolean")
    timeout = data.get("timeout", Config.timeout)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number")
    batch_size = data.get("view_batch_size", Config.view_batch_size)
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("view_batch_size must be a positive integer")
    sort = data.get("sort", Config.sort)
    if not isinstance(sort, str):
        raise ValueError("sort must be a field name")
    data["sort"] = field_name(sort)
    return Config(**data)
