"""
Redis 客户端工具
从 Django settings 的 REDIS_URL 创建 Redis 连接
"""
import redis as redis_lib
from django.conf import settings


def get_redis():
    """获取 Redis 连接（懒加载，复用连接）"""
    if not hasattr(get_redis, '_client'):
        get_redis._client = redis_lib.from_url(settings.REDIS_URL)
    return get_redis._client
