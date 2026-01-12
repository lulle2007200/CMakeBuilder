import logging
import sys
from typing import Any, Final

# import jsonschema
# from rich.console import Console
# from rich.logging import RichHandler

from . import __name__

CRITICAL: Final = logging.CRITICAL
ERROR: Final = logging.ERROR
WARNING: Final = logging.WARNING
INFO: Final = logging.INFO
DEBUG: Final = logging.DEBUG
NOTSET: Final = logging.NOTSET

class SuppressingFormatter(logging.Formatter):
    def formatException(self, exc_info):
        etype, value = exc_info

        import traceback
        filtered_tb = []
        for fram in traceback.extract_tb(tb):
            if "jsonschema" not in frame.filename:
                filtered_tb.append(frame)

        formatted = "Traceback (most recent call last):"
        formatted += "".join(traceback.format_lit(filtered_tb))
        formatted += f"etype.__name__: {value}"
        return formatted

class Logger(logging.Logger):
    """
    Configure rich-based logging for the application.

    Args:
        level: The logging level to use
        colors: Whether to use colors in the console output
    """

    def __init__(self, level: int = WARNING, colors: bool = True):
        super().__init__(name=__name__, level=level)

        handler = logging.StreamHandler(stream=sys.stderr)
        formatter = SuppressingFormatter("[%(name)s] %(message)s")
        handler.setFormatter(formatter)

        self.addHandler(handler)

        # self.pare
        # nt = logging.root

        # self.console = Console(color_system="auto" if colors else None)

        # handler = RichHandler(console=self.console, rich_tracebacks=True, tracebacks_suppress=[jsonschema])
        # formatter = logging.Formatter("[%(name)s]   %(message)s")
        # handler.setFormatter(formatter)

        # self.addHandler(handler)

    def getChild(self, name: str) -> Any:
        full_name = name if name.startswith(self.name) else f"{self.name}.{name}"
        child = self.manager.getLogger(full_name)
        child.parent = self
        return child
