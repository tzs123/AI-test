"""
项目级 pytest 配置文件
配置 Django 测试环境
"""
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
