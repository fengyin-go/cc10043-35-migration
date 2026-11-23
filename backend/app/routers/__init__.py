"""业务路由汇总：迁移专用路由先注册，避免被通用 /api/{module} 动态路径抢先匹配。"""
from __future__ import annotations

from app.routers import checkpoint_migration, resource

ROUTERS = [checkpoint_migration, resource]
