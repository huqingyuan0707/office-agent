"""office-agent 通用壳（FastAPI）

子模块按需导入，不在此预导入，避免包导入即拉起 Web 栈：
- ``office_agent_server.app``：应用工厂与 ``app`` 实例
- ``office_agent_server.db`` / ``models``：会话与数据模型
- ``office_agent_server.plugins``：plugins/ 目录下的工具插件装载器
"""

__version__ = "0.1.0"