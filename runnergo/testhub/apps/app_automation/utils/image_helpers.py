# -*- coding: utf-8 -*-
"""
图片工具函数（Serializer 使用）
"""
import os

from django.core import signing


PUBLIC_TEMPLATE_SALT = 'app-automation-public-template'


def normalize_element_image_path(image_path):
    raw = str(image_path or '').strip().lstrip('/').replace('\\', '/')
    normalized = os.path.normpath(raw).replace(os.sep, '/')
    if normalized in {'', '.'} or normalized.startswith('../') or normalized.startswith('/'):
        return ''
    return normalized


def get_element_image_url(image_path):
    """
    构建元素图片URL（被 Serializer 使用）
    
    参数:
        image_path: 相对路径，如 "common/login.png"（已包含分类）
    
    返回:
    长期有效签名的只读 URL；资源路径仍受白名单和签名校验约束。
    """
    normalized = normalize_element_image_path(image_path)
    if not normalized:
        return None
    token = signing.dumps(normalized, salt=PUBLIC_TEMPLATE_SALT, compress=True)
    return f"/api/app-automation/templates/{token}/"
