"""运维与调试脚本。

用 ``uv run python -m scripts.<name>`` 的方式执行（在 backend 目录下）。

为什么不直接 ``uv run python scripts/<name>.py``：那样 sys.path[0] 会是
``scripts/`` 而不是 ``backend/``，``app`` 包就导入不到了。用 ``-m`` 执行时
sys.path[0] 是当前目录，导入天然可用，不需要在脚本里做 sys.path 手脚。
"""
