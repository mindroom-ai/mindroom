"""Compatibility names for the shared bounded media transport."""

from mindroom.tool_system.media_transport import decode_media_result, encode_media_result

decode_browser_mcp_result = decode_media_result
encode_browser_mcp_result = encode_media_result

__all__ = ["decode_browser_mcp_result", "encode_browser_mcp_result"]
