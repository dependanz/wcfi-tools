"""Local-only Facebook Messenger export ingestion."""

from .indexer import FbmsgExportError, build_index, scan_export
from .parser import decode_meta_text, iter_messages

__all__ = ["FbmsgExportError", "build_index", "decode_meta_text", "iter_messages", "scan_export"]
