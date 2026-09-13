"""大模型调用层。

对外只暴露 :class:`~app.ai.base.LLMClient` 协议，具体实现（真实 DeepSeek 客户端 /
测试用的假客户端）可以互换，业务代码不感知。
"""
