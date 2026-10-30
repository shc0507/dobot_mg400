[English](0009-service-web-framework.en.md)

# 0009：服务的 Web 框架

## 状态

已接受：2026-10-30。

## 背景

位移台面板使用 Flask 和 waitress。迁入的热成像服务使用 FastAPI 和 uvicorn。

## 决定

新服务使用 Flask 和 waitress。迁入的热成像服务保留 FastAPI 和 uvicorn。机械臂核心与面板继续只用标准库。

- 新 Flask 服务提供 `create_app(service, *, token, base_path)`。共用的 `labkit` 契约蓝图与页面路由使用同一个服务对象。
- 在 `labkit` 内，仅 `labkit.flask` 导入 Flask。其他共用契约模块使用标准库。
- 服务通过 1 个锁管理设备访问。`start()` 方法启动轮询。导入模块时不得打开设备或启动线程。
- 服务入口读取 `MG400_<SERVICE>_*`，支持 `--bind`、`--port` 和 `--demo`，并执行共用访问规则。配置错误时以状态码 2 退出。
- 框架选择不改变[服务契约](0001-instrument-service-contract.md)、控制租约、停止行为或设备专用的关闭规则。

## 影响

新服务复用位移台的部署方式。热成像迁移保留原有框架，同时提供相同的外部契约。
