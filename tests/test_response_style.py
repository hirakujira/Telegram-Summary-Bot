from __future__ import annotations

import unittest

from app.llm import STYLE_PROMPTS, build_system_prompt
from app.response_style import normalize_response_style


class ResponseStyleTests(unittest.TestCase):
    def test_normalizes_supported_style(self) -> None:
        self.assertEqual(normalize_response_style(" FUNNY "), "funny")

    def test_rejects_unknown_style(self) -> None:
        with self.assertRaises(ValueError):
            normalize_response_style("formal")

    def test_builds_style_specific_prompts(self) -> None:
        normal_prompt = build_system_prompt("normal")
        funny_prompt = build_system_prompt("funny")
        roast_prompt = build_system_prompt("roast")

        self.assertIn(STYLE_PROMPTS["normal"], normal_prompt)
        self.assertIn(STYLE_PROMPTS["funny"], funny_prompt)
        self.assertIn(STYLE_PROMPTS["roast"], roast_prompt)
        self.assertIn("不得杜撰", funny_prompt)
        self.assertIn("不得人身攻擊", roast_prompt)


if __name__ == "__main__":
    unittest.main()
