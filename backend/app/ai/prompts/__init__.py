"""Prompt 构造。

集中在一个模块里，方便对比不同版本的措辞效果，也便于将来做 prompt 版本管理。
"""

from app.ai.prompts.base import BASE_SYSTEM_PROMPT, JSON_KEYWORD, assert_json_keyword
from app.ai.prompts.extraction import (
    PromptPair,
    build_extraction_prompt,
    build_repair_prompt,
)

__all__ = [
    "BASE_SYSTEM_PROMPT",
    "JSON_KEYWORD",
    "PromptPair",
    "assert_json_keyword",
    "build_extraction_prompt",
    "build_repair_prompt",
]
