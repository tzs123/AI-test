"""
安全修复说明：
原代码对所有 /api/ 路径禁用 CSRF 检查，这是一个严重的安全漏洞。

修复方案：
1. 完全移除全局 CSRF 禁用逻辑
2. 对于使用 JWT 认证的 REST API，CSRF 保护通常不需要（JWT 不依赖 cookie）
3. 如果确实需要禁用 CSRF，应在具体视图上使用 @csrf_exempt 装饰器

安全建议：
- 对于 Session 认证的 API，必须保留 CSRF 保护
- 对于 JWT 认证的 API，可以不使用 CSRF 保护
- 永远不要全局禁用 CSRF 保护
"""
from django.utils.deprecation import MiddlewareMixin

class DisableCSRFMiddleware(MiddlewareMixin):
    """
    [已弃用] 此中间件已被禁用
    
    安全修复：原实现存在严重安全漏洞，对所有 /api/ 路径禁用 CSRF 检查。
    
    替代方案：
    1. 对于 JWT 认证的 API：不需要 CSRF 保护（JWT 存储在 localStorage/sessionStorage）
    2. 对于 Session 认证的 API：必须保留 CSRF 保护
    3. 如需禁用特定视图的 CSRF：使用 @csrf_exempt 装饰器
    
    此中间件已不再执行任何操作，保留仅为向后兼容。
    """
    def process_request(self, request):
        # 安全修复：不再禁用 CSRF 检查
        # 如果需要禁用特定路径的 CSRF，请在具体视图上使用 @csrf_exempt 装饰器
        pass
