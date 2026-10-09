"""产品级演示输入入口（会议室页「模拟参会人发言」）。

存在理由与边界见 ``app/demo/schemas.py`` / ``app/demo/service.py`` 的模块文档。
一句话：让产品 UI 能打出一条真实 FINAL，而**不**把 ``segment_seq`` 的所有权
从前端夺走（CLAUDE.md 硬规则）。

这个包不 import ``app.captain`` 的 service / worker / schemas —— 只 import
``app.captain.wake_word``（一个纯函数）用于如实回报「这句话有没有请求裁判」。
裁判本身由既有的 transcript 咽喉点自动触发，演示入口不参与编排。
"""

__all__: list[str] = []
