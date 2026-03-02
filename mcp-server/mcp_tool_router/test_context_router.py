"""
Tests for Context Mode v2
Run with: python -m pytest mcp_tool_router/test_context_router.py -v
"""

import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
