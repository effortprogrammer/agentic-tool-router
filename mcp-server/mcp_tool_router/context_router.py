"""
Context Mode v2: Tiered Context Router for mcpflow-router

Routes tool outputs through L0/L1/L2 tiers to reduce context bloat
while preserving information access.

Integration: This module is called from opencode_gateway_server.py
to process tool outputs in streaming responses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Callable, Literal, Any

_log = logging.getLogger("mcpflow-gateway")

# === Configuration ===

L0_THRESHOLD = int(os.environ.get("CONTEXT_MODE_L0_THRESHOLD", "1024"))     # 1 KB
L1_THRESHOLD = int(os.environ.get("CONTEXT_MODE_L1_THRESHOLD", "10240"))    # 10 KB

CTX_DIR = os.environ.get("CONTEXT_MODE_DIR", "/tmp/ctx")
MAX_CTX_SIZE_MB = int(os.environ.get("CONTEXT_MODE_MAX_MB", "100"))

Tier = Literal["L0", "L1", "L2"]

# Global lock for singleton initialization
_init_lock = threading.Lock()


# === Context Store ===

@dataclass
class ContextStore:
    """Manages /tmp/ctx/ storage for original outputs."""
    
    base_dir: str = CTX_DIR
    max_size_bytes: int = MAX_CTX_SIZE_MB * 1024 * 1024
    _index: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    
    def __post_init__(self):
        os.makedirs(self.base_dir, exist_ok=True)
        self._load_index()
    
    def _index_path(self) -> str:
        return os.path.join(self.base_dir, "index.json")
    
    def _load_index(self):
        with self._lock:
            try:
                with open(self._index_path(), 'r') as f:
                    self._index = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                self._index = {}
    
    def _save_index(self):
        """Atomic write to index file to prevent corruption."""
        try:
            # Write to temp file first, then atomic rename
            fd, tmp_path = tempfile.mkstemp(
                dir=self.base_dir, 
                prefix=".index_", 
                suffix=".tmp"
            )
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(self._index, f, indent=2)
                os.replace(tmp_path, self._index_path())
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            _log.warning("failed to save context index: %s", e)
    
    def save(self, content: str, prefix: str = "out", metadata: dict = None) -> str:
        """Save content and return path. Thread-safe."""
        content_bytes = content.encode('utf-8')
        h = hashlib.sha256(content_bytes).hexdigest()[:8]
        filename = f"{prefix}_{h}.txt"
        path = os.path.join(self.base_dir, filename)
        
        with self._lock:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                
                self._index[filename] = {
                    "size": len(content_bytes),
                    "lines": content.count('\n') + 1,
                    "prefix": prefix,
                    "metadata": metadata or {},
                }
                self._save_index()
                self._cleanup_if_needed()
            except Exception as e:
                _log.warning("failed to save context file: %s", e)
        
        return path
    
    def _cleanup_if_needed(self):
        """Remove oldest files if total size exceeds budget. Must hold lock."""
        total = sum(entry.get("size", 0) for entry in self._index.values())
        if total <= self.max_size_bytes:
            return
        
        files_with_mtime = []
        for filename in self._index:
            path = os.path.join(self.base_dir, filename)
            try:
                mtime = os.path.getmtime(path)
                files_with_mtime.append((filename, mtime))
            except OSError:
                pass
        
        files_with_mtime.sort(key=lambda x: x[1])
        
        for filename, _ in files_with_mtime:
            if total <= self.max_size_bytes:
                break
            path = os.path.join(self.base_dir, filename)
            try:
                total -= self._index.get(filename, {}).get("size", 0)
                os.remove(path)
                del self._index[filename]
                _log.debug("context cleanup: removed %s", filename)
            except OSError:
                pass
        
        self._save_index()


# === Output Router ===

@dataclass
class RouterConfig:
    """Configuration for output routing."""
    l0_threshold: int = L0_THRESHOLD
    l1_threshold: int = L1_THRESHOLD
    enabled: bool = True
    tool_overrides: dict[str, Tier] = field(default_factory=dict)


class OutputRouter:
    """Routes tool outputs through tiered context processing."""
    
    def __init__(
        self, 
        store: ContextStore,
        config: RouterConfig = None,
        agent_fn: Callable[[str], str] = None,
    ):
        self.store = store
        self.config = config or RouterConfig()
        self.agent_fn = agent_fn
    
    def route_tier(self, content: str, tool_name: str = None) -> Tier:
        """Determine which tier to use for this content."""
        if tool_name and tool_name in self.config.tool_overrides:
            return self.config.tool_overrides[tool_name]
        
        size = len(content.encode('utf-8'))
        
        if size < self.config.l0_threshold:
            return "L0"
        elif size < self.config.l1_threshold:
            return "L1"
        else:
            return "L2"
    
    def process(self, content: str, tool_name: str = "unknown") -> str:
        """Process tool output through the appropriate tier."""
        if not self.config.enabled:
            return content
        
        tier = self.route_tier(content, tool_name)
        
        if tier == "L0":
            return content
        
        # Save original for L1 and L2
        path = self.store.save(
            content, 
            prefix=self._sanitize_prefix(tool_name),
            metadata={"tool": tool_name, "tier": tier}
        )
        
        if tier == "L1":
            result = self._summarize_l1(content, path, tool_name)
        else:
            # L2: Use agent if available, otherwise fall back to L1
            result = self._process_l2(content, path, tool_name)
        
        _log.debug(
            "context_router: %s %s -> %s (%d -> %d bytes)",
            tool_name, tier, "summarized", len(content), len(result)
        )
        
        return result
    
    def _sanitize_prefix(self, tool_name: str) -> str:
        """Sanitize tool name for use as filename prefix."""
        return re.sub(r'[^a-zA-Z0-9_-]', '_', tool_name)[:20]
    
    def _summarize_l1(self, content: str, path: str, tool_name: str) -> str:
        """Generate L1 algorithmic summary with file reference."""
        lines = content.splitlines()
        line_count = len(lines)
        size_bytes = len(content.encode('utf-8'))
        size_str = self._human_size(size_bytes)
        
        output = []
        output.append(f"[{tool_name}: {size_str}, {line_count} lines]")
        
        if lines:
            output.append("┌ " + self._truncate(lines[0], 70))
            for i in range(1, min(3, line_count)):
                output.append("│ " + self._truncate(lines[i], 70))
        
        if line_count > 6:
            output.append("│ ...")
        
        if line_count > 3:
            start = max(3, line_count - 3)
            for i in range(start, line_count):
                output.append("│ " + self._truncate(lines[i], 70))
        
        output.append(f"└ Full: {path}")
        
        return "\n".join(output)
    
    def _process_l2(self, content: str, path: str, tool_name: str) -> str:
        """
        Process L2 tier output.
        
        Currently falls back to L1 algorithmic summary.
        Agent delegation is planned for future versions.
        """
        # TODO: Implement agent delegation when agent_fn interface is finalized
        # For now, L2 uses enhanced L1 summary with additional stats
        if self.agent_fn is not None:
            try:
                return self._delegate_to_agent(content, path, tool_name)
            except Exception as e:
                _log.warning("L2 agent delegation failed, falling back to L1: %s", e)
        
        return self._summarize_l1(content, path, tool_name)
    
    def _delegate_to_agent(self, content: str, path: str, tool_name: str) -> str:
        """Delegate to agent for intelligent summarization."""
        lines = len(content.splitlines())
        size_str = self._human_size(len(content.encode('utf-8')))
        
        prompt = f"""Analyze the output stored at {path} ({size_str}, {lines} lines).
This is output from the "{tool_name}" tool.

Provide a structured summary:
1. RESULT: Key findings or output (max 500 chars)
2. STATS: Relevant counts, sizes, or metrics
3. NOTABLE: Anything unusual, errors, or important details

Be concise. The user can access {path} directly for full details."""

        summary = self.agent_fn(prompt)
        return f"[{tool_name}: {size_str} → agent summary]\n\n{summary}\n\n→ Full: {path}"
    
    def _truncate(self, text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len - 3] + "..."
    
    def _human_size(self, size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"


# === Stream Processor ===

class StreamProcessor:
    """
    Processes streaming SSE responses to route tool outputs.
    
    Handles chunk boundaries correctly by buffering incomplete events.
    SSE events are delimited by double newlines (\\n\\n).
    """
    
    def __init__(self, router: OutputRouter):
        self.router = router
        self._buffer = b""
        self._lock = threading.Lock()
    
    def process_chunk(self, chunk: bytes) -> bytes:
        """
        Process a streaming chunk, routing tool outputs through tiers.
        
        Buffers incomplete SSE events to handle chunk boundaries correctly.
        Thread-safe.
        """
        if not chunk:
            return chunk
        
        # Quick check: skip if definitely no tool result markers
        # (check both buffer and new chunk)
        combined = self._buffer + chunk
        if b'"tool_result"' not in combined and b'"tool.result"' not in combined:
            # No tool results - pass through but still handle buffering for consistency
            return self._passthrough_with_buffer(chunk)
        
        with self._lock:
            return self._process_buffered(chunk)
    
    def _passthrough_with_buffer(self, chunk: bytes) -> bytes:
        """Pass through chunks that don't contain tool results."""
        # If we have buffered data, we need to handle it properly
        with self._lock:
            if self._buffer:
                # Append new chunk and try to extract complete events
                self._buffer += chunk
                return self._extract_complete_events()
            return chunk
    
    def _process_buffered(self, chunk: bytes) -> bytes:
        """Process chunk with proper SSE event buffering."""
        self._buffer += chunk
        return self._extract_complete_events()
    
    def _extract_complete_events(self) -> bytes:
        """Extract and process complete SSE events from buffer."""
        output_chunks = []
        
        # SSE events are separated by \n\n
        while b'\n\n' in self._buffer:
            event, self._buffer = self._buffer.split(b'\n\n', 1)
            processed = self._process_sse_event(event)
            output_chunks.append(processed)
            output_chunks.append(b'\n\n')
        
        return b''.join(output_chunks)
    
    def _process_sse_event(self, event: bytes) -> bytes:
        """Process a single complete SSE event."""
        try:
            # Decode with error handling for chunk boundaries
            text = event.decode('utf-8')
        except UnicodeDecodeError:
            # Incomplete UTF-8 sequence - return as-is
            _log.debug("UTF-8 decode error in SSE event, passing through")
            return event
        
        lines = text.split('\n')
        processed_lines = []
        
        for line in lines:
            processed_line = self._process_sse_line(line)
            processed_lines.append(processed_line)
        
        return '\n'.join(processed_lines).encode('utf-8')
    
    def _process_sse_line(self, line: str) -> str:
        """Process a single SSE line."""
        if not line.startswith('data: '):
            return line
        
        json_str = line[6:]
        if not json_str.strip():
            return line
        
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return line
        
        if not isinstance(data, dict):
            return line
        
        event_type = data.get('type', '')
        
        if event_type in ('tool_result', 'tool.result'):
            data = self._process_tool_result(data)
            return 'data: ' + json.dumps(data, ensure_ascii=False)
        
        return line
    
    def _process_tool_result(self, data: dict) -> dict:
        """Route tool result content through the tiered system."""
        content = data.get('content', '')
        if not content or not isinstance(content, str):
            return data
        
        tool_name = data.get('tool', data.get('name', 'unknown'))
        
        # Route through tiered context
        processed = self.router.process(content, tool_name=tool_name)
        data['content'] = processed
        
        return data
    
    def flush(self) -> bytes:
        """
        Flush any remaining buffered data.
        Call this when the stream ends to get any incomplete events.
        """
        with self._lock:
            if self._buffer:
                remaining = self._buffer
                self._buffer = b""
                # Process remaining as a single event (may be incomplete)
                return self._process_sse_event(remaining)
            return b""


# === Singleton Instances ===

_store_instance: ContextStore | None = None
_router_instance: OutputRouter | None = None
_processor_instance: StreamProcessor | None = None


def _get_store() -> ContextStore:
    global _store_instance
    if _store_instance is None:
        with _init_lock:
            # Double-check after acquiring lock
            if _store_instance is None:
                _store_instance = ContextStore()
    return _store_instance


def _get_router() -> OutputRouter:
    global _router_instance
    if _router_instance is None:
        with _init_lock:
            if _router_instance is None:
                store = _get_store()
                config = _load_config()
                _router_instance = OutputRouter(store, config)
    return _router_instance


def _load_config() -> RouterConfig:
    """Load config from environment."""
    config = RouterConfig()
    
    if os.environ.get("CONTEXT_MODE_DISABLED"):
        config.enabled = False
    
    # Tool overrides: "glob:L0,playwright:L2"
    overrides_str = os.environ.get("CONTEXT_MODE_TOOL_OVERRIDES", "")
    if overrides_str:
        for item in overrides_str.split(","):
            if ":" in item:
                tool, tier = item.split(":", 1)
                tier = tier.strip().upper()
                if tier in ("L0", "L1", "L2"):
                    config.tool_overrides[tool.strip()] = tier  # type: ignore[assignment]
    
    return config


def get_stream_processor() -> StreamProcessor:
    """Get or create the singleton stream processor."""
    global _processor_instance
    if _processor_instance is None:
        with _init_lock:
            if _processor_instance is None:
                router = _get_router()
                _processor_instance = StreamProcessor(router)
    return _processor_instance


def process_stream_chunk(chunk: bytes) -> bytes:
    """
    Main entry point for stream processing.
    Call this from opencode_gateway_server.py streaming loop.
    """
    if os.environ.get("CONTEXT_MODE_DISABLED"):
        return chunk
    return get_stream_processor().process_chunk(chunk)
