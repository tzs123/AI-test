"""统一加载器：定位器 + 用例 + 配置（兼容旧入口）。"""
import os
import yaml


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_locator(page_name: str) -> dict:
    """读取 config/locator.yaml 中某页面的定位元素。"""
    path = os.path.join(_project_root(), "config", "locator.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get(page_name, {})


def load_cases(filepath: str):
    """加载测试用例 YAML（兼容旧 core.run_main 调用，统一委托给 utils.yaml_loader）。"""
    from utils.yaml_loader import load_cases as _load
    return _load(filepath)
