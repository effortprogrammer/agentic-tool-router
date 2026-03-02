"""
Tests for Context Mode v2
Run with: python -m pytest mcp_tool_router/test_context_router.py -v
"""

import os
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

from .context_router import (
    ContextStore,
    OutputRouter,
    RouterConfig,
    StreamProcessor,
    process_stream_chunk,
    L0_THRESHOLD,
    L1_THRESHOLD,
)


class TestContextStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = ContextStore(base_dir=self.temp_dir)
    
    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_save_and_retrieve(self):
        content = "Hello, World!"
        path = self.store.save(content, prefix="test")
        
        self.assertTrue(os.path.exists(path))
        with open(path, 'r') as f:
            self.assertEqual(f.read(), content)
    
    def test_unique_paths_for_different_content(self):
        path1 = self.store.save("content 1", prefix="test")
        path2 = self.store.save("content 2", prefix="test")
        
        self.assertNotEqual(path1, path2)
    
    def test_same_content_same_path(self):
        """Same content should produce same path (content-addressed)."""
        path1 = self.store.save("identical content", prefix="test")
        path2 = self.store.save("identical content", prefix="test")
        
        self.assertEqual(path1, path2)
    
    def test_thread_safety(self):
        """Test concurrent saves don't corrupt index."""
        results = []
        errors = []
        
        def save_content(i):
            try:
                path = self.store.save(f"content {i}", prefix=f"t{i}")
                results.append(path)
            except Exception as e:
                errors.append(e)
        
        threads = [threading.Thread(target=save_content, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(results), 10)
        self.assertEqual(len(set(results)), 10)  # All unique


class TestOutputRouter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = ContextStore(base_dir=self.temp_dir)
        self.config = RouterConfig()
        self.router = OutputRouter(self.store, self.config)
    
    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_l0_passthrough(self):
        """Small outputs should pass through unchanged."""
        content = "small output"
        result = self.router.process(content, tool_name="echo")
        self.assertEqual(result, content)
    
    def test_l1_summary(self):
        """Medium outputs should get summary with link."""
        lines = [f"line {i}: " + "x" * 50 for i in range(30)]
        content = "\n".join(lines)
        
        self.assertGreater(len(content), L0_THRESHOLD)
        self.assertLess(len(content), L1_THRESHOLD)
        
        result = self.router.process(content, tool_name="test")
        
        self.assertIn("[test:", result)
        self.assertIn("lines]", result)
        self.assertIn("└ Full:", result)
        self.assertLess(len(result), len(content))
    
    def test_l2_fallback_to_l1(self):
        """L2 without agent should fallback to L1."""
        content = "x" * 15000
        result = self.router.process(content, tool_name="bigcmd")
        
        self.assertIn("[bigcmd:", result)
        self.assertIn("└ Full:", result)
    
    def test_l2_with_agent(self):
        """L2 with agent should use agent summary."""
        mock_agent = MagicMock(return_value="Agent summary here")
        router = OutputRouter(self.store, self.config, agent_fn=mock_agent)
        
        content = "x" * 15000
        result = router.process(content, tool_name="bigcmd")
        
        mock_agent.assert_called_once()
        self.assertIn("agent summary", result)
        self.assertIn("Agent summary here", result)
    
    def test_l2_agent_failure_fallback(self):
        """L2 should fallback to L1 if agent fails."""
        def failing_agent(prompt):
            raise Exception("Agent unavailable")
        
        router = OutputRouter(self.store, self.config, agent_fn=failing_agent)
        
        content = "x" * 15000
        result = router.process(content, tool_name="bigcmd")
        
        # Should fallback to L1 format
        self.assertIn("[bigcmd:", result)
        self.assertIn("└ Full:", result)
        self.assertNotIn("agent summary", result)
    
    def test_tool_override(self):
        """Tool-specific overrides should be respected."""
        config = RouterConfig(tool_overrides={"glob": "L0"})
        router = OutputRouter(self.store, config)
        
        content = "x" * 15000
        tier = router.route_tier(content, tool_name="glob")
        self.assertEqual(tier, "L0")
    
    def test_disabled_router(self):
        """Disabled router should pass through everything."""
        config = RouterConfig(enabled=False)
        router = OutputRouter(self.store, config)
        
        content = "x" * 15000
        result = router.process(content, tool_name="test")
        self.assertEqual(result, content)


class TestStreamProcessor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = ContextStore(base_dir=self.temp_dir)
        self.router = OutputRouter(self.store, RouterConfig())
        self.processor = StreamProcessor(self.router)
    
    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_passthrough_non_tool_result(self):
        """Non-tool-result chunks should pass through."""
        chunk = b'data: {"type": "message.delta", "content": "hello"}\n\n'
        result = self.processor.process_chunk(chunk)
        self.assertEqual(result, chunk)
    
    def test_process_tool_result(self):
        """Tool result chunks should be processed."""
        # Create a medium-sized tool result
        content = "x" * 5000
        chunk = f'data: {{"type": "tool_result", "tool": "bash", "content": "{content}"}}\n\n'
        
        result = self.processor.process_chunk(chunk.encode('utf-8'))
        result_str = result.decode('utf-8')
        
        # Should contain summary markers
        self.assertIn("[bash:", result_str)
        self.assertIn("└ Full:", result_str)
    
    def test_chunked_event_buffering(self):
        """Events split across chunks should be handled correctly."""
        content = "x" * 5000
        full_event = f'data: {{"type": "tool_result", "tool": "bash", "content": "{content}"}}\n\n'
        full_bytes = full_event.encode('utf-8')
        
        # Split in the middle
        mid = len(full_bytes) // 2
        chunk1 = full_bytes[:mid]
        chunk2 = full_bytes[mid:]
        
        # First chunk should return empty (buffered)
        result1 = self.processor.process_chunk(chunk1)
        self.assertEqual(result1, b"")
        
        # Second chunk should return processed event
        result2 = self.processor.process_chunk(chunk2)
        result_str = result2.decode('utf-8')
        
        self.assertIn("[bash:", result_str)
        self.assertIn("└ Full:", result_str)
    
    def test_multiple_events_in_chunk(self):
        """Multiple complete events in one chunk should all be processed."""
        event1 = 'data: {"type": "message.delta", "content": "hello"}\n\n'
        event2 = 'data: {"type": "message.delta", "content": "world"}\n\n'
        chunk = (event1 + event2).encode('utf-8')
        
        result = self.processor.process_chunk(chunk)
        result_str = result.decode('utf-8')
        
        self.assertIn("hello", result_str)
        self.assertIn("world", result_str)
    
    def test_flush_incomplete_event(self):
        """Flush should return any remaining buffered data."""
        # Send incomplete event
        incomplete = b'data: {"type": "tool_result"'
        self.processor.process_chunk(incomplete)
        
        # Flush should return the incomplete data
        flushed = self.processor.flush()
        self.assertIn(b"tool_result", flushed)
    
    def test_thread_safety(self):
        """Concurrent chunk processing should be safe."""
        results = []
        errors = []
        
        def process_chunk(i):
            try:
                chunk = f'data: {{"type": "message.delta", "content": "msg{i}"}}\n\n'
                result = self.processor.process_chunk(chunk.encode('utf-8'))
                results.append(result)
            except Exception as e:
                errors.append(e)
        
        threads = [threading.Thread(target=process_chunk, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(results), 10)


class TestTierBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = ContextStore(base_dir=self.temp_dir)
        self.router = OutputRouter(self.store, RouterConfig())
    
    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_just_under_l0(self):
        self.assertEqual(
            self.router.route_tier("x" * (L0_THRESHOLD - 1)),
            "L0"
        )
    
    def test_at_l0_threshold(self):
        self.assertEqual(
            self.router.route_tier("x" * L0_THRESHOLD),
            "L1"
        )
    
    def test_just_under_l1(self):
        self.assertEqual(
            self.router.route_tier("x" * (L1_THRESHOLD - 1)),
            "L1"
        )
    
    def test_at_l1_threshold(self):
        self.assertEqual(
            self.router.route_tier("x" * L1_THRESHOLD),
            "L2"
        )


class TestUnicodeHandling(unittest.TestCase):
    """Test handling of non-ASCII content."""
    
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = ContextStore(base_dir=self.temp_dir)
        self.router = OutputRouter(self.store, RouterConfig())
        self.processor = StreamProcessor(self.router)
    
    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_korean_content(self):
        """Korean text should be handled correctly."""
        content = "안녕하세요. 한글 테스트입니다." * 100
        result = self.router.process(content, tool_name="test")
        
        # Should be summarized (content is > L0)
        self.assertIn("[test:", result)
        self.assertIn("안녕하세요", result)  # First line preview
    
    def test_emoji_content(self):
        """Emoji should be handled correctly."""
        content = "🎉🎊🎁" * 500
        result = self.router.process(content, tool_name="emoji")
        
        self.assertIn("[emoji:", result)


if __name__ == "__main__":
    unittest.main()
