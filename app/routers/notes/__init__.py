"""笔记路由包：按职责拆为子模块；本文件聚合为单一 router。

依赖（require_login / csrf_protect）挂在父 router 上，include 时自动应用到全部子路由。
注册顺序保持拆分前的物理顺序（/notes/reorder、/notes/batch 等具体路径
仍先于 /notes/{note_id} 匹配）。
"""

from fastapi import APIRouter, Depends

from ...deps import csrf_protect, require_login

router = APIRouter(dependencies=[Depends(require_login), Depends(csrf_protect)])

from . import batch, detail, export, flags, links, lock, trash, versions, views  # noqa: E402

for _module in (views, batch, trash, detail, flags, links, versions, lock, export):
    router.include_router(_module.router)
