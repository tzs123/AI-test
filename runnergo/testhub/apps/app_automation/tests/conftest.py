# -*- coding: utf-8 -*-
"""
pytest 配置文件
"""
import pytest
import os
import django

# 配置 Django 设置
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
django.setup()


@pytest.fixture(autouse=True)
def allow_runner_database_access(django_db_blocker):
    """
    APP automation pytest files are execution entrypoints, not isolated unit
    tests. They must read the platform database records created from the UI/API.
    """
    with django_db_blocker.unblock():
        yield


def pytest_addoption(parser):
    """添加命令行选项"""
    parser.addoption("--device-id", action="store", default=None, help="设备ID")
    parser.addoption("--package-name", action="store", default=None, help="应用包名")
    parser.addoption("--ios-browser-start-url", action="store", default=None, help="iOS Safari 起始URL")


@pytest.fixture(scope="session")
def device_id(request):
    """设备ID fixture"""
    return request.config.getoption("--device-id") or os.environ.get('APP_DEVICE_ID')


@pytest.fixture(scope="session")
def package_name(request):
    """应用包名 fixture"""
    return request.config.getoption("--package-name") or os.environ.get('APP_PACKAGE_NAME')


@pytest.fixture(scope="session")
def device_platform():
    return os.environ.get('APP_DEVICE_PLATFORM', 'android')


@pytest.fixture(scope="session")
def ios_wda_url():
    return os.environ.get('APP_IOS_WDA_URL', '')


@pytest.fixture(scope="session")
def ios_wda_bundle_id():
    return os.environ.get('APP_IOS_WDA_BUNDLE_ID', '')


@pytest.fixture(scope="session")
def ios_browser_start_url(request):
    return request.config.getoption("--ios-browser-start-url") or os.environ.get('APP_IOS_BROWSER_START_URL', '')


@pytest.fixture(scope="session")
def test_case_id():
    """测试用例ID fixture"""
    return os.environ.get('APP_TEST_CASE_ID')


@pytest.fixture(scope="session")
def execution_id():
    """执行记录ID fixture"""
    return os.environ.get('APP_EXECUTION_ID')


@pytest.fixture(scope="session")
def username():
    """执行用户名 fixture"""
    return os.environ.get('APP_USERNAME', 'unknown')
