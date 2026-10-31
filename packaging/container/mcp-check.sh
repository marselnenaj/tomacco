#!/bin/sh
# Talks MCP to the image over standard input and output, as an agent does:
# initialize, then tools/list, through `docker run -i --rm IMAGE mcp`.
#   packaging/container/mcp-check.sh [IMAGE]
set -eu
image="${1:-tomacco-container:1.1.0}"
reply="$(printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"mcp-check","version":"1"}}}' \
    '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
    | docker run -i --rm "$image" mcp)"
printf '%s\n' "$reply" | grep -q '"serverInfo":{"name":"tomacco"' || { echo "no answer to initialize" >&2; exit 1; }
printf '%s\n' "$reply" | grep '"id":2' | grep -q '"name":"project.execute"' || { echo "tools/list has no project.execute" >&2; exit 1; }
tools="$(printf '%s\n' "$reply" | grep '"id":2' | grep -o '"inputSchema"' | wc -l)"
echo "ok: $image answered initialize and listed $tools tools over stdio"
