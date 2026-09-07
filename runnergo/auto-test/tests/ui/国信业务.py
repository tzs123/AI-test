# AUTO-GENERATED WEB FLOW RUNNER
import allure
import pytest

from core.web_flow_runner import load_web_flow, run_web_flow


FLOW_PATH = 'cases/ui/国信业务.yaml'


@allure.feature('浏览器录制用例')
@pytest.mark.ui
def test_____(browser, runtime_data_row):
    flow = load_web_flow(FLOW_PATH)
    run_web_flow(browser, flow)
