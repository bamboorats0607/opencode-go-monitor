"""core 包：T0 基础设施模块（设置 / 端口管理 / 退避重试 / SSE 看门狗）。

- settings.py     —— AppSettings：QSettings 完整客制化设置封装
- port_manager.py —— PortManager：主监听端口退避池探测 + 扩展固定口预检
- retry_policy.py —— 手写指数退避重试（红线：禁止 tenacity）
- watchdog.py     —— SSE 反代心跳看门狗（超时触发 on_stall）
"""
