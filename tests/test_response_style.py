from __future__ import annotations

import unittest

from app.llm import OUTPUT_CONTRACT, STYLE_PROMPTS, build_system_prompt
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

    def test_every_style_carries_the_shared_output_contract(self) -> None:
        for style in ("normal", "funny", "roast"):
            prompt = build_system_prompt(style)
            with self.subTest(style=style):
                self.assertIn(OUTPUT_CONTRACT, prompt)
                self.assertIn("<群組名稱> Summary | <摘要區間> | <訊息總數> 則訊息", prompt)
                self.assertIn("連結只能逐字使用對話紀錄中標示為「討論連結」的網址", prompt)

    def test_styles_do_not_leak_each_others_persona(self) -> None:
        prompts = {style: build_system_prompt(style) for style in ("normal", "funny", "roast")}

        self.assertIn("資深摘要編輯", prompts["normal"])
        self.assertNotIn("資深摘要編輯", prompts["roast"])
        self.assertNotIn("嘴砲王", prompts["normal"])
        self.assertNotIn("嘴砲王", prompts["funny"])

    def test_roast_prompt_drops_the_neutral_tone_but_keeps_the_factual_floor(self) -> None:
        roast_prompt = build_system_prompt("roast")

        self.assertNotIn("語氣自然、精簡", roast_prompt)
        self.assertNotIn("省略寒暄、附和", roast_prompt)
        self.assertIn("不能編造沒發生過的事", roast_prompt)
        self.assertIn("不要杜撰因果、動機、情緒、共識或後續結果", roast_prompt)


if __name__ == "__main__":
    unittest.main()
