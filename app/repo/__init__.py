"""数据访问层：按分区拆为子模块；本文件重导出全部公开名。

调用方式与拆分前完全一致：`from app import repo` / `repo.create_note(...)`。
依赖方向：common ← notes ← {trash, versions(延迟), listing, tags, links, templates, stats, meta, blog_stats}
"""

from .common import *  # noqa: F401,F403
from .notes import *  # noqa: F401,F403
from .versions import *  # noqa: F401,F403
from .trash import *  # noqa: F401,F403
from .listing import *  # noqa: F401,F403
from .tags import *  # noqa: F401,F403
from .links import *  # noqa: F401,F403
from .templates import *  # noqa: F401,F403
from .stats import *  # noqa: F401,F403
from .meta import *  # noqa: F401,F403
from .blog_stats import *  # noqa: F401,F403
