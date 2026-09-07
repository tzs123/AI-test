import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest
import yaml

from backend import db, settings, step_store
from backend.cases import service as case_service
from backend.projects import service as project_service
from backend.web_recorder import BrowserRecordingSession, PROBE_ELEMENT_SCRIPT, _element_signature
from core.web_flow_runner import WebFlowRunner, mobile_context_options, resolve_variables, score_web_candidate
from core import web_flow_runner
from backend import url_security


class _FakeLocator:
    def __init__(self, count=1, tag="div"):
        self._count = count
        self.first = self
        self.tag = tag
        self.clicked = False
        self.selected_options = []

    def count(self):
        return self._count

    def wait_for(self, **_kwargs):
        if self._count <= 0:
            raise RuntimeError("not found")

    def nth(self, _index):
        return self

    def is_visible(self):
        return self._count > 0

    def drag_to(self, target, **_kwargs):
        self.dragged_to = target

    def click(self, **_kwargs):
        self.clicked = True

    def evaluate(self, script, *_args):
        if "HTMLSelectElement" in str(script):
            return self.tag == "select"
        return False

    def select_option(self, **kwargs):
        self.selected_options.append(kwargs)


class _FakeMouse:
    def __init__(self):
        self.wheels = []
        self.moves = []
        self.downs = 0
        self.ups = 0

    def move(self, x, y, steps=None):
        self.moves.append((x, y, steps))

    def down(self):
        self.downs += 1

    def up(self):
        self.ups += 1

    def wheel(self, delta_x, delta_y):
        self.wheels.append((delta_x, delta_y))


class _FakeScope:
    def __init__(self, counts=None, healing_candidates=None):
        self.counts = counts or {}
        self.healing_candidates = healing_candidates or []

    def get_by_test_id(self, value):
        return _FakeLocator(self.counts.get(("testid", value), 0))

    def get_by_role(self, role, name=None, exact=None):
        return _FakeLocator(self.counts.get(("role", role, name), 0))

    def locator(self, value):
        return _FakeLocator(self.counts.get(("locator", value), 1 if value == "#healed" else 0))

    def evaluate(self, _script, _arg=None):
        return self.healing_candidates


class _FakePage:
    def __init__(self, scope):
        self.main_frame = scope
        self.mouse = _FakeMouse()

    def wait_for_timeout(self, milliseconds):
        time.sleep(min(milliseconds, 10) / 1000)


def _touch_picker_markup(scrollable_page: bool = False) -> str:
    body_style = "margin: 0; min-height: 1400px;" if scrollable_page else "margin: 0; height: 812px; overflow: hidden;"
    page_filler = "<div style='height:1400px;background:linear-gradient(#fff,#eef2ff)'></div>" if scrollable_page else ""
    return """
    <style>
      body { %s }
      .dialog { position: fixed; left: 0; right: 0; bottom: 0; height: 220px; background: white; }
      .toolbar { height: 44px; display: flex; justify-content: space-between; align-items: center; padding: 0 16px; }
      .picker { height: 160px; overflow: hidden; touch-action: none; border-top: 1px solid #eee; }
      .track { transform: translateY(0); }
      .item { height: 44px; display: flex; align-items: center; justify-content: center; }
    </style>
    %s
    <div class="dialog" role="dialog">
      <div class="toolbar"><button>取消</button><button>确认</button></div>
      <div class="picker" id="picker">
        <div class="track" id="track">
          <div class="item">北京市</div><div class="item">天津市</div><div class="item">河北省</div>
          <div class="item">山西省</div><div class="item">内蒙古</div><div class="item">辽宁省</div>
          <div class="item">吉林省</div><div class="item">黑龙江省</div>
        </div>
      </div>
    </div>
    <div id="offset">0</div>
    <script>
      let offset = 0;
      let startY = 0;
      let startOffset = 0;
      const picker = document.querySelector('#picker');
      const track = document.querySelector('#track');
      const output = document.querySelector('#offset');
      function render() {
        track.style.transform = `translateY(${offset}px)`;
        output.textContent = String(Math.round(offset));
      }
      picker.addEventListener('touchstart', event => {
        startY = event.touches[0].clientY;
        startOffset = offset;
      });
      picker.addEventListener('touchmove', event => {
        offset = Math.max(-220, Math.min(0, startOffset + event.touches[0].clientY - startY));
        render();
        event.preventDefault();
      });
    </script>
    """ % (body_style, page_filler)


def _multi_event_picker_markup() -> str:
    return """
    <style>
      body { margin: 0; height: 812px; overflow: hidden; }
      .dialog { position: fixed; left: 0; right: 0; bottom: 0; height: 220px; background: white; }
      .toolbar { height: 44px; }
      .picker { height: 160px; overflow: hidden; touch-action: none; }
      .track { transform: translateY(0); }
      .item { height: 44px; display: flex; align-items: center; justify-content: center; }
    </style>
    <div class="dialog" role="dialog">
      <div class="toolbar">选择</div>
      <div class="picker" id="picker">
        <div class="track" id="track">
          <div class="item">A</div><div class="item">B</div><div class="item">C</div><div class="item">D</div>
        </div>
      </div>
    </div>
    <script>
      window.__eventCounts = { wheel: 0, pointermove: 0, mousemove: 0, touchmove: 0 };
      window.__offset = 0;
      let startY = 0;
      const picker = document.querySelector('#picker');
      const track = document.querySelector('#track');
      function render(offset) {
        window.__offset = Math.round(offset);
        track.style.transform = `translateY(${window.__offset}px)`;
      }
      picker.addEventListener('wheel', event => {
        window.__eventCounts.wheel += 1;
        render(window.__offset - event.deltaY);
      });
      picker.addEventListener('pointerdown', event => {
        startY = event.clientY;
      });
      picker.addEventListener('pointermove', event => {
        if (!event.buttons) return;
        window.__eventCounts.pointermove += 1;
        render(event.clientY - startY);
      });
      picker.addEventListener('mousedown', event => {
        startY = event.clientY;
      });
      picker.addEventListener('mousemove', event => {
        if (!event.buttons) return;
        window.__eventCounts.mousemove += 1;
        render(event.clientY - startY);
      });
      picker.addEventListener('touchstart', event => {
        startY = event.touches[0].clientY;
      });
      picker.addEventListener('touchmove', event => {
        window.__eventCounts.touchmove += 1;
        render(event.touches[0].clientY - startY);
        event.preventDefault();
      });
    </script>
    """


def _popup_text_picker_markup() -> str:
    return """
    <style>
      body { margin: 0; height: 812px; overflow: hidden; }
      .dialog { position: fixed; left: 0; right: 0; bottom: 0; height: 220px; background: white; }
      .toolbar { height: 44px; display: flex; align-items: center; justify-content: space-between; padding: 0 16px; }
      .picker { height: 160px; overflow: hidden; touch-action: none; }
      .track { transform: translateY(0); }
      .item { height: 44px; display: flex; align-items: center; justify-content: center; }
    </style>
    <div class="dialog" role="dialog">
      <div class="toolbar"><button>取消</button><button>确认</button></div>
      <div class="picker" id="picker">
        <div class="track" id="track">
          <div class="item">北京市</div><div class="item">天津市</div><div class="item">河北省</div>
          <div class="item">山西省</div><div class="item">内蒙古</div><div class="item">辽宁省</div>
          <div class="item">吉林省</div><div class="item">黑龙江省</div>
        </div>
      </div>
    </div>
    <div id="selected"></div>
    <script>
      let offset = 0;
      let startY = 0;
      let startOffset = 0;
      const picker = document.querySelector('#picker');
      const track = document.querySelector('#track');
      function render() {
        track.style.transform = `translateY(${offset}px)`;
      }
      function move(clientY) {
        offset = Math.max(-220, Math.min(0, startOffset + clientY - startY));
        render();
      }
      picker.addEventListener('touchstart', event => {
        startY = event.touches[0].clientY;
        startOffset = offset;
      });
      picker.addEventListener('touchmove', event => {
        move(event.touches[0].clientY);
        event.preventDefault();
      });
      picker.addEventListener('pointerdown', event => {
        startY = event.clientY;
        startOffset = offset;
      });
      picker.addEventListener('pointermove', event => {
        if (event.buttons) move(event.clientY);
      });
      picker.addEventListener('mousedown', event => {
        startY = event.clientY;
        startOffset = offset;
      });
      picker.addEventListener('mousemove', event => {
        if (event.buttons) move(event.clientY);
      });
      document.querySelectorAll('.item').forEach(item => {
        item.addEventListener('click', () => {
          document.querySelector('#selected').textContent = item.textContent.trim();
        });
      });
    </script>
    """


def _popup_multi_column_picker_markup() -> str:
    return """
    <style>
      body { margin: 0; height: 812px; overflow: hidden; }
      .dialog { position: fixed; left: 0; right: 0; bottom: 0; height: 220px; background: white; }
      .toolbar { height: 44px; display: flex; align-items: center; justify-content: space-between; padding: 0 16px; }
      .columns { display: flex; height: 160px; }
      .picker-column { flex: 1; overflow: hidden; touch-action: none; }
      .track { transform: translateY(0); }
      .item { height: 44px; display: flex; align-items: center; justify-content: center; }
    </style>
    <div class="dialog" role="dialog">
      <div class="toolbar"><button>取消</button><button>确认</button></div>
      <div class="columns">
        <div class="picker-column" data-target="province">
          <div class="track">
            <div class="item">北京市</div><div class="item">天津市</div><div class="item">河北省</div>
            <div class="item">山西省</div><div class="item">浙江省</div>
          </div>
        </div>
        <div class="picker-column" data-target="city">
          <div class="track">
            <div class="item">宁波市</div><div class="item">温州市</div><div class="item">绍兴市</div>
            <div class="item">杭州市</div><div class="item">湖州市</div>
          </div>
        </div>
        <div class="picker-column" data-target="district">
          <div class="track">
            <div class="item">上城区</div><div class="item">西湖区</div><div class="item">滨江区</div>
            <div class="item">拱墅区</div><div class="item">萧山区</div>
          </div>
        </div>
      </div>
    </div>
    <div id="province"></div><div id="city"></div><div id="district"></div>
    <script>
      document.querySelectorAll('.picker-column').forEach(column => {
        let offset = 0;
        let startY = 0;
        let startOffset = 0;
        const track = column.querySelector('.track');
        const selected = document.querySelector(`#${column.dataset.target}`);
        function render() {
          track.style.transform = `translateY(${offset}px)`;
        }
        function move(clientY) {
          offset = Math.max(-176, Math.min(0, startOffset + clientY - startY));
          render();
        }
        column.addEventListener('touchstart', event => {
          startY = event.touches[0].clientY;
          startOffset = offset;
        });
        column.addEventListener('touchmove', event => {
          move(event.touches[0].clientY);
          event.preventDefault();
        });
        column.addEventListener('pointerdown', event => {
          startY = event.clientY;
          startOffset = offset;
        });
        column.addEventListener('pointermove', event => {
          if (event.buttons) move(event.clientY);
        });
        column.addEventListener('mousedown', event => {
          startY = event.clientY;
          startOffset = offset;
        });
        column.addEventListener('mousemove', event => {
          if (event.buttons) move(event.clientY);
        });
        column.querySelectorAll('.item').forEach(item => {
          item.addEventListener('click', () => {
            selected.textContent = item.textContent.trim();
          });
        });
      });
    </script>
    """


def _vant_area_picker_markup() -> str:
    return """
    <style>
      body { margin: 0; height: 812px; overflow: hidden; }
      .van-popup { position: fixed; left: 0; right: 0; bottom: 0; height: 260px; background: white; }
      .van-picker__toolbar { height: 44px; display: flex; align-items: center; justify-content: space-between; padding: 0 16px; }
      .van-picker__columns { display: flex; height: 216px; }
      .van-picker-column { flex: 1; overflow: hidden; touch-action: none; }
      .van-picker-column__item { height: 44px; display: flex; align-items: center; justify-content: center; }
    </style>
    <div class="van-popup">
      <div class="van-picker">
        <div class="van-picker__toolbar"><button class="van-picker__cancel">取消</button><button class="van-picker__confirm">确认</button></div>
        <div class="van-picker__columns">
          <div class="van-picker-column" data-column="province"></div>
          <div class="van-picker-column" data-column="city"></div>
          <div class="van-picker-column" data-column="county"></div>
        </div>
      </div>
    </div>
    <div id="province"></div><div id="city"></div><div id="county"></div>
    <script>
      const areaList = {
        province_list: {'110000': '北京市', '120000': '天津市', '130000': '河北省', '330000': '浙江省'},
        city_list: {'110100': '市辖区', '120100': '市辖区', '130100': '石家庄市', '330100': '杭州市', '330200': '宁波市'},
        county_list: {'110101': '东城区', '110102': '西城区', '330102': '上城区', '330105': '拱墅区'}
      };
      const state = {provinceCode: '110000', cityCode: '110100', countyCode: '110101'};
      const picker = document.querySelector('.van-picker');
      const columns = Array.from(document.querySelectorAll('.van-picker-column'));
      const entriesByPrefix = (list, prefix) => Object.entries(list)
        .filter(([code]) => code.startsWith(prefix))
        .map(([code, text]) => ({code, text}));
      const provinceOptions = () => Object.entries(areaList.province_list).map(([code, text]) => ({code, text}));
      const cityOptions = () => entriesByPrefix(areaList.city_list, state.provinceCode.slice(0, 2));
      const countyOptions = () => entriesByPrefix(areaList.county_list, state.cityCode.slice(0, 4));
      function renderColumn(index, options) {
        columns[index].innerHTML = options.map(item => `<div class="van-picker-column__item" data-value="${item.code}">${item.text}</div>`).join('');
      }
      function render() {
        renderColumn(0, provinceOptions());
        renderColumn(1, cityOptions());
        renderColumn(2, countyOptions());
        document.querySelector('#province').textContent = areaList.province_list[state.provinceCode] || '';
        document.querySelector('#city').textContent = areaList.city_list[state.cityCode] || '';
        document.querySelector('#county').textContent = areaList.county_list[state.countyCode] || '';
        window.__selectedArea = {
          province: document.querySelector('#province').textContent,
          city: document.querySelector('#city').textContent,
          county: document.querySelector('#county').textContent
        };
      }
      function firstCode(options) {
        return options.length ? options[0].code : '';
      }
      function setColumnValue(index, value) {
        if (index === 0) {
          const province = provinceOptions().find(item => item.text === value);
          if (!province) return;
          state.provinceCode = province.code;
          state.cityCode = firstCode(cityOptions());
          state.countyCode = firstCode(countyOptions());
        } else if (index === 1) {
          const city = cityOptions().find(item => item.text === value);
          if (!city) return;
          state.cityCode = city.code;
          state.countyCode = firstCode(countyOptions());
        } else if (index === 2) {
          const county = countyOptions().find(item => item.text === value);
          if (!county) return;
          state.countyCode = county.code;
        }
        render();
      }
      picker.__vue__ = {
        areaList,
        setColumnValue,
        setColumnIndex(index, itemIndex) {
          const options = index === 0 ? provinceOptions() : index === 1 ? cityOptions() : countyOptions();
          const item = options[itemIndex];
          if (item) setColumnValue(index, item.text);
        }
      };
      render();
    </script>
    """


def _vant_dom_only_area_picker_markup() -> str:
    return """
    <style>
      body { margin: 0; height: 812px; overflow: hidden; }
      .van-popup { position: fixed; left: 0; right: 0; bottom: 0; height: 260px; background: white; }
      .van-picker__toolbar { height: 44px; }
      .van-picker__columns { display: flex; height: 216px; }
      .van-picker-column { flex: 1; overflow: hidden; }
      .van-picker-column__wrapper { margin: 0; padding: 0; list-style: none; transform: translate3d(0, 86px, 0); }
      .van-picker-column__item { height: 44px; display: flex; align-items: center; justify-content: center; }
    </style>
    <div class="van-popup" role="dialog">
      <div class="van-picker">
        <div class="van-picker__toolbar"></div>
        <div class="van-picker__columns">
          <div class="van-picker-column" data-column="province"><ul class="van-picker-column__wrapper"></ul></div>
          <div class="van-picker-column" data-column="city"><ul class="van-picker-column__wrapper"></ul></div>
          <div class="van-picker-column" data-column="county"><ul class="van-picker-column__wrapper"></ul></div>
        </div>
      </div>
    </div>
    <script>
      const state = {province: '北京市', city: '市辖区', county: '东城区'};
      const data = {
        provinces: ['北京市', '天津市', '浙江省'],
        cities: {
          '北京市': ['市辖区'],
          '天津市': ['市辖区'],
          '浙江省': ['宁波市', '杭州市']
        },
        counties: {
          '市辖区': ['东城区'],
          '宁波市': ['海曙区'],
          '杭州市': ['上城区', '拱墅区']
        }
      };
      const columns = Array.from(document.querySelectorAll('.van-picker-column'));
      const optionsFor = index => index === 0
        ? data.provinces
        : (index === 1 ? data.cities[state.province] : data.counties[state.city]);
      const selectedFor = index => index === 0 ? state.province : (index === 1 ? state.city : state.county);
      function renderColumn(index) {
        const options = optionsFor(index);
        const selected = selectedFor(index);
        const selectedIndex = Math.max(0, options.indexOf(selected));
        const wrapper = columns[index].querySelector('.van-picker-column__wrapper');
        wrapper.innerHTML = options.map(item =>
          `<li role="button" class="van-picker-column__item${item === selected ? ' van-picker-column__item--selected' : ''}"><div class="van-ellipsis">${item}</div></li>`
        ).join('');
        wrapper.style.transform = `translate3d(0, ${86 - selectedIndex * 44}px, 0)`;
      }
      function render() {
        renderColumn(0);
        renderColumn(1);
        renderColumn(2);
      }
      columns.forEach((column, index) => {
        column.addEventListener('click', event => {
          const item = event.target.closest('.van-picker-column__item');
          if (!item) return;
          const value = item.textContent.trim();
          if (index === 0) {
            state.province = value;
            state.city = data.cities[value][0];
            state.county = data.counties[state.city][0];
          } else if (index === 1) {
            state.city = value;
            state.county = data.counties[value][0];
          } else {
            state.county = value;
          }
          render();
        });
      });
      window.__selectedArea = state;
      render();
    </script>
    """


def test_flow_goto_rejects_private_ssrf_target(monkeypatch):
    monkeypatch.setattr(url_security.settings, "ALLOW_PRIVATE_URLS", False)
    monkeypatch.setattr(url_security.settings, "ALLOWED_URL_HOSTS", [])
    monkeypatch.setattr(url_security.settings, "ALLOWED_URL_CIDRS", [])
    page = _NoNativeHistoryPage("about:blank")
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "base_url": "http://127.0.0.1",
        "steps": [],
    })

    with pytest.raises(ValueError, match="受保护"):
        runner.execute_step({"action": "goto", "url": "http://127.0.0.1/admin"})


class _NoNativeHistoryPage:
    def __init__(self, url):
        self.url = url
        self.visited = []

    def go_back(self, **_kwargs):
        return None

    def go_forward(self, **_kwargs):
        return None

    def goto(self, url, **_kwargs):
        self.url = url
        self.visited.append(url)
        return None


def test_resolve_variables_recursively(monkeypatch):
    monkeypatch.setenv("LOGIN_USER", "runnergo")
    value = {
        "username": "${LOGIN_USER}",
        "nested": ["prefix-${LOGIN_USER}", {"missing": "${NOT_CONFIGURED}"}],
    }
    assert resolve_variables(value) == {
        "username": "runnergo",
        "nested": ["prefix-runnergo", {"missing": "${NOT_CONFIGURED}"}],
    }


def test_resolve_variables_uses_runtime_context_dot_paths_case_insensitively(monkeypatch):
    monkeypatch.setenv(
        "RUNTIME_CONTEXT_JSON",
        json.dumps({
            "local": {
                "VERIFICATION_CODE": "${dataAssets.testData.CODE}",
                "random_phone": "${dataAssets.testData.phone}",
            },
            "data": {"phone": "1234567", "expected_submit_result": "手机号长度错误"},
            "dataAssets": {"testData": {"code": "123456", "phone": "18877570399"}},
        }),
    )

    assert resolve_variables("${VERIFICATION_CODE}") == "123456"
    assert resolve_variables("${random_phone}") == "18877570399"
    assert resolve_variables("phone=${dataAssets.testData.phone}") == "phone=18877570399"
    assert resolve_variables("${data.phone}") == "1234567"
    assert resolve_variables("${data.expected_submit_result}") == "手机号长度错误"


def test_resolve_variables_falls_back_to_parameterized_row_data(monkeypatch):
    monkeypatch.delenv("RUNTIME_CONTEXT_JSON", raising=False)
    monkeypatch.delenv("RUNTIME_VARIABLES_JSON", raising=False)
    monkeypatch.setenv(
        "RUNTIME_DATA_ROW_JSON",
        json.dumps({"generatedData": {"phone": "13508657000"}}),
    )

    assert resolve_variables("${dataAssets.generatedData.phone}") == "13508657000"


def test_page_text_can_assert_api_and_database_runtime_data(monkeypatch, tmp_path):
    class BodyLocator:
        def __init__(self, page):
            self.page = page

        def inner_text(self, **_kwargs):
            return self.page.body_text

    class RuntimeAssertionPage:
        url = "https://example.test"

        def __init__(self):
            self.body_text = ""
            self.element_text = ""

        def locator(self, selector):
            assert selector == "body"
            return BodyLocator(self)

        def screenshot(self, **_kwargs):
            return b"png"

        def is_closed(self):
            return False

    class BoundLocator:
        def __init__(self, page):
            self.page = page

        def inner_text(self, **_kwargs):
            return self.page.element_text

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.test/profile"
        text = '{"data":{"name":"Alice"}}'

        def json(self):
            return json.loads(self.text)

    monkeypatch.setattr("core.mixed_steps.requests.request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("core.mixed_steps.validate_outbound_url", lambda value: value)

    db_path = tmp_path / "page_assert.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE users(name TEXT)")
    connection.execute("INSERT INTO users(name) VALUES(?)", ("Bob",))
    connection.commit()
    connection.close()

    page = RuntimeAssertionPage()
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})
    runner.locate = lambda _element, _timeout: BoundLocator(page)
    runner._wait_for_blocking_ui = lambda _timeout: {"cleared": True, "waited_ms": 0}
    runner.execute_step({
        "action": "api",
        "url": "https://example.test/profile",
        "extract": {"apiName": "$.data.name"},
    })
    page.element_text = "Alice"
    runner.execute_step({
        "action": "assert_text",
        "element": {"name": "用户姓名"},
        "expected": "${apiName}",
        "operator": "equals",
    })
    assert runner.last_locator_diagnostics["page_data_assertion"]["actual"] == "Alice"

    runner.execute_step({
        "action": "db",
        "connection": {"driver": "sqlite", "path": str(db_path)},
        "sql": "SELECT name, 2 AS expected_total FROM users",
        "extract": {
            "dbName": "$.first.name",
            "dbTotal": "$.first.expected_total",
        },
    })
    page.element_text = "Customer: Bob"
    runner.execute_step({
        "action": "assert_text",
        "element": {"name": "客户名称"},
        "expected": "${dbName}",
        "operator": "contains",
    })
    assert runner.last_locator_diagnostics["page_data_assertion"]["expected"] == "Bob"

    page.element_text = "3"
    runner.execute_step({
        "action": "assert_text",
        "element": {"name": "页面数量"},
        "expected": "${dbTotal}",
        "operator": "gt",
    })
    assert runner.last_locator_diagnostics["page_data_assertion"]["actual"] == "3"
    assert runner.last_locator_diagnostics["page_data_assertion"]["expected"] == 2


def test_jmeter_step_runs_as_non_browser_action_and_attaches_runtime_data(monkeypatch):
    class FakePage:
        url = "about:blank"

        def is_closed(self):
            return False

        def screenshot(self, **_kwargs):
            return b"png"

    def fake_jmeter(_step, _runtime_context):
        return {
            "script": "cases/pay.jmx",
            "jtl": "/tmp/runnergo-jmeter-out.jtl",
            "exit_code": 0,
            "duration_ms": 321.5,
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr(web_flow_runner, "_run_jmeter_step", fake_jmeter)
    runner = WebFlowRunner(FakePage(), {"flow_version": 2, "steps": []})

    runner.execute_step({
        "action": "jmeter",
        "name": "支付链路 JMeter 并发",
        "script": "cases/pay.jmx",
        "properties": {"users": "50"},
    })

    assert runner.last_locator_diagnostics["selected_strategy"] == "jmeter"
    assert runner.last_locator_diagnostics["exit_code"] == 0
    assert runner.last_locator_diagnostics["duration_ms"] == 321.5
    assert runner.last_locator_diagnostics["smart_wait_before"]["reason"] == "non-browser-step"
    assert runner.runtime_outputs[-1]["step"] == "支付链路 JMeter 并发"
    assert runner.runtime_outputs[-1]["result"]["script"] == "cases/pay.jmx"


def test_jmeter_step_failure_keeps_runtime_result_and_diagnostics(monkeypatch):
    class FakePage:
        url = "about:blank"

        def is_closed(self):
            return False

        def screenshot(self, **_kwargs):
            return b"png"

    def failing_jmeter(_step, _runtime_context):
        exc = RuntimeError("JMeter 执行失败，退出码 1")
        setattr(exc, "runtime_result", {
            "script": "cases/pay.jmx",
            "exit_code": 1,
            "duration_ms": 88.0,
            "stderr": "java.net.ConnectException",
        })
        raise exc

    monkeypatch.setattr(web_flow_runner, "_run_jmeter_step", failing_jmeter)
    runner = WebFlowRunner(FakePage(), {"flow_version": 2, "steps": []})

    with pytest.raises(RuntimeError, match="JMeter 执行失败"):
        runner.execute_step({
            "action": "jmx",
            "name": "支付链路 JMeter 并发",
            "script": "cases/pay.jmx",
        })

    assert runner.last_locator_diagnostics["matched"] is False
    assert runner.last_locator_diagnostics["selected_strategy"] == "jmeter"
    assert runner.last_locator_diagnostics["exit_code"] == 1
    assert "java.net.ConnectException" in runner.last_locator_diagnostics["stderr"]
    assert runner.runtime_outputs[-1]["result"]["exit_code"] == 1


def test_successful_step_attaches_screenshot(page, monkeypatch):
    attachments = []

    class FakeAllure:
        attachment_type = SimpleNamespace(PNG="image/png", JSON="application/json")

        @staticmethod
        def attach(body, name="", attachment_type=None):
            attachments.append({"body": body, "name": name, "type": attachment_type})

    monkeypatch.setattr(web_flow_runner, "allure", FakeAllure)
    page.set_content("<main>ready</main>")
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "wait", "name": "等待页面稳定", "seconds": 0})

    screenshot = next(item for item in attachments if item["type"] == "image/png")
    assert screenshot["name"] == "执行成功-等待页面稳定"
    assert screenshot["body"].startswith(b"\x89PNG")


def test_flow_run_persists_each_step_status_and_screenshot(page, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TASK_ID", "task_steps_demo")
    monkeypatch.setenv("FLOW_CASE_FILE", "test_imported.yaml")
    page.set_content("<main>ready</main>")
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "name": "逐步骤调试",
        "steps": [
            {"id": "ready", "action": "wait", "name": "等待页面", "seconds": 0},
            {"id": "wrong_url", "action": "assert_url", "name": "校验错误地址", "value": "/success", "fail_fast": True},
            {"id": "after_assert", "action": "wait", "name": "断言后继续执行", "seconds": 0},
        ],
    })

    with pytest.raises(AssertionError, match="1 个断言失败"):
        runner.run()

    state = step_store.read("task_steps_demo")
    assert [step["status"] for step in state["steps"]] == ["passed", "failed", "passed"]
    assert state["status"] == "failed"
    assert state["steps"][0]["screenshot"].endswith("001_ready_passed.png")
    assert state["steps"][1]["screenshot"].endswith("002_wrong_url_failed.png")
    assert state["steps"][1]["diagnostics"]["soft_assertion"]["continued"] is True
    assert state["steps"][1]["is_assertion"] is True
    assert state["steps"][1]["assertion_matched"] is False
    assert state["steps"][2]["is_assertion"] is False
    assert state["steps"][2]["screenshot"].endswith("003_after_assert_passed.png")
    assert (tmp_path / "screenshots" / "task_steps_demo" / "001_ready_passed.png").exists()
    assert "校验错误地址" in (tmp_path / "logs" / "task_steps_demo.log").read_text(encoding="utf-8")


def test_flow_run_executes_focus_click_before_fill(page, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(web_flow_runner, "allure", None)
    monkeypatch.setenv("TASK_ID", "coalesced_focus_click")
    monkeypatch.setenv("FLOW_CASE_FILE", "test_address.yaml")
    page.set_content(
        """
        <label for="address">详细地址</label>
        <textarea id="address" name="address" aria-label="详细地址"></textarea>
        <script>
          window.__addressClicks = 0;
          document.querySelector('#address').addEventListener('click', () => window.__addressClicks += 1);
        </script>
        """
    )
    element = {
        "id": "wel_address",
        "name": "详细地址",
        "tag": "textarea",
        "role": "textbox",
        "accessible_name": "详细地址",
        "locators": [{"strategy": "id", "value": "address"}],
        "fingerprint": {
            "tag": "textarea",
            "role": "textbox",
            "accessible_name": "详细地址",
            "attrs": {
                "testid": "",
                "id": "address",
                "name": "address",
                "placeholder": "",
                "type": "",
            },
            "parent": {"tag": "body", "text": "详细地址"},
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "name": "输入框点击后输入",
        "steps": [
            {
                "id": "click_address",
                "action": "click",
                "name": "点击 详细地址",
                "element": element,
                "wait_after": 0.2,
            },
            {
                "id": "fill_address",
                "action": "fill",
                "name": "输入 详细地址",
                "element": element,
                "value": "西湖",
            },
        ],
    })

    runner.run()

    assert page.locator("#address").input_value() == "西湖"
    assert page.evaluate("window.__addressClicks") == 1
    state = step_store.read("coalesced_focus_click")
    assert [step["status"] for step in state["steps"]] == ["passed", "passed"]
    assert "coalesced_with_next_fill" not in state["steps"][0]["diagnostics"]


def test_focus_click_coalescing_keeps_virtual_keyboard_open_step():
    element = {
        "id": "wel_plate",
        "tag": "input",
        "role": "textbox",
        "input_type": "text",
        "fingerprint": {
            "tag": "input",
            "role": "textbox",
            "control_type": "plate_input",
            "attrs": {"id": "plate", "name": "plate"},
        },
    }

    assert not web_flow_runner.is_redundant_focus_click(
        {"action": "click", "element": element},
        {"action": "fill", "element": element, "value": "浙A12345"},
    )


def test_robust_click_caps_dom_fallback_timeout():
    class DetachedTarget:
        def __init__(self):
            self.evaluate_timeout = None

        def click(self, **kwargs):
            if kwargs.get("force"):
                raise RuntimeError("force click detached")
            raise RuntimeError("normal click detached")

        def bounding_box(self, **_kwargs):
            raise RuntimeError("bounding box detached")

        def evaluate(self, _script, **kwargs):
            self.evaluate_timeout = kwargs.get("timeout")
            raise RuntimeError("DOM click detached")

    target = DetachedTarget()
    runner = WebFlowRunner(_FakePage(_FakeScope()), {
        "flow_version": 2,
        "steps": [],
        "timeout": 10000,
    })

    with pytest.raises(RuntimeError, match="normal click detached"):
        runner._robust_click(target, 10000)

    assert target.evaluate_timeout == 5000
    assert runner.last_locator_diagnostics["click_fallback"]["method"] == "failed"


def test_flow_run_skips_optional_branch_when_future_step_is_visible(page, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(web_flow_runner, "allure", None)
    monkeypatch.setenv("TASK_ID", "conditional_branch_visible")
    monkeypatch.setenv("FLOW_CASE_FILE", "test_conditional_branch.yaml")
    page.set_content(
        '<textarea id="address" aria-label="详细地址"></textarea>'
    )
    address = {
        "id": "wel_address",
        "tag": "textarea",
        "role": "textbox",
        "accessible_name": "详细地址",
        "locators": [{"strategy": "id", "value": "address"}],
        "fingerprint": {
            "tag": "textarea",
            "role": "textbox",
            "accessible_name": "详细地址",
            "attrs": {"id": "address", "name": "", "testid": "", "placeholder": "", "type": ""},
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    missing = {
        "id": "wel_first_branch",
        "tag": "button",
        "role": "button",
        "accessible_name": "首次申请",
        "locators": [{"strategy": "id", "value": "first-branch"}],
    }
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "name": "条件分支跳转",
        "step_execution_mode": "adaptive",
        "steps": [
            {
                "id": "first_branch",
                "action": "click",
                "name": "首次申请步骤",
                "element": missing,
                "skip_to_if_step_visible": [
                    {"step_id": "missing_future", "timeout": 200},
                    {"step_id": "click_address", "timeout": 300},
                ],
            },
            {"id": "missing_future", "action": "click", "element": missing},
            {"id": "click_address", "action": "click", "element": address},
            {"id": "fill_address", "action": "fill", "element": address, "value": "西湖"},
        ],
    })

    runner.run()

    assert page.locator("#address").input_value() == "西湖"
    state = step_store.read("conditional_branch_visible")
    assert [step["status"] for step in state["steps"]] == [
        "skipped", "skipped", "passed", "passed",
    ]
    assert state["steps"][0]["diagnostics"]["selected_strategy"] == "conditional_branch"


def test_flow_run_keeps_optional_branch_when_future_step_is_absent(page, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(web_flow_runner, "allure", None)
    monkeypatch.setenv("TASK_ID", "conditional_branch_absent")
    monkeypatch.setenv("FLOW_CASE_FILE", "test_conditional_branch.yaml")
    page.set_content(
        """
        <button id="start">首次申请</button>
        <script>
          document.querySelector('#start').addEventListener('click', () => {
            document.body.insertAdjacentHTML(
              'beforeend', '<textarea id="address" aria-label="详细地址"></textarea>'
            );
          });
        </script>
        """
    )
    start = {
        "id": "wel_start",
        "tag": "button",
        "role": "button",
        "accessible_name": "首次申请",
        "locators": [{"strategy": "id", "value": "start"}],
    }
    address = {
        "id": "wel_address",
        "tag": "textarea",
        "role": "textbox",
        "accessible_name": "详细地址",
        "locators": [{"strategy": "id", "value": "address"}],
        "fingerprint": {
            "tag": "textarea",
            "role": "textbox",
            "accessible_name": "详细地址",
            "attrs": {"id": "address", "name": "", "testid": "", "placeholder": "", "type": ""},
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "name": "保留首次申请分支",
        "step_execution_mode": "adaptive",
        "steps": [
            {
                "id": "start",
                "action": "click",
                "element": start,
                "skip_to_if_step_visible": {"step_id": "click_address", "timeout": 200},
            },
            {"id": "click_address", "action": "click", "element": address},
            {"id": "fill_address", "action": "fill", "element": address, "value": "西湖"},
        ],
    })

    runner.run()

    assert page.locator("#address").input_value() == "西湖"
    state = step_store.read("conditional_branch_absent")
    assert [step["status"] for step in state["steps"]] == [
        "passed", "passed", "passed",
    ]


def test_flow_run_auto_skips_absent_page_steps_until_current_page_step(page, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(web_flow_runner, "allure", None)
    monkeypatch.setenv("TASK_ID", "auto_page_skip")
    monkeypatch.setenv("FLOW_CASE_FILE", "test_auto_page_skip.yaml")
    page.set_content(
        """
        <label>民族
          <input id="nation" name="picker" placeholder="请选择" aria-label="民族" />
        </label>
        <button id="sign">去签署</button>
        """
    )
    marriage = {
        "id": "wel_marriage",
        "tag": "input",
        "role": "textbox",
        "accessible_name": "婚姻状况",
        "locators": [
            {"strategy": "role", "role": "textbox", "name": "婚姻状况", "value": "婚姻状况"},
            {"strategy": "placeholder", "value": "请选择"},
            {"strategy": "name", "value": "picker"},
        ],
        "fingerprint": {
            "tag": "input",
            "role": "textbox",
            "accessible_name": "婚姻状况",
            "attrs": {
                "id": "van-field-11-input",
                "name": "picker",
                "placeholder": "请选择",
                "testid": "",
                "type": "text",
            },
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    popup_option = {
        "id": "wel_marriage_option",
        "tag": "li",
        "role": "button",
        "accessible_name": "已婚有子女",
        "locators": [{"strategy": "text", "value": "已婚有子女"}],
    }
    education = {
        **marriage,
        "id": "wel_education",
        "accessible_name": "教育程度",
        "fingerprint": {
            **marriage["fingerprint"],
            "accessible_name": "教育程度",
            "attrs": {
                **marriage["fingerprint"]["attrs"],
                "id": "van-field-12-input",
            },
        },
    }
    education_option = {
        "id": "wel_education_option",
        "tag": "li",
        "role": "button",
        "accessible_name": "大专",
        "locators": [{"strategy": "text", "value": "大专"}],
    }
    sign = {
        "id": "wel_sign",
        "tag": "button",
        "role": "button",
        "accessible_name": "去签署",
        "locators": [{"strategy": "id", "value": "sign"}],
        "fingerprint": {
            "tag": "button",
            "role": "button",
            "accessible_name": "去签署",
            "attrs": {"id": "sign", "name": "", "testid": "", "placeholder": "", "type": ""},
        },
    }
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "name": "自动跳过旧页面步骤",
        "step_execution_mode": "adaptive",
        "steps": [
            {"id": "marriage", "action": "click", "name": "点击 婚姻状况", "element": marriage},
            {"id": "marriage_option", "action": "popup_select_text", "name": "弹框选择 已婚有子女", "element": popup_option, "value": "已婚有子女"},
            {"id": "education", "action": "click", "name": "点击 教育程度", "element": education},
            {"id": "education_option", "action": "popup_select_text", "name": "弹框选择 大专", "element": education_option, "value": "大专"},
            {"id": "sign", "action": "click", "name": "点击 去签署", "element": sign},
        ],
    })

    runner.run()

    state = step_store.read("auto_page_skip")
    assert [step["status"] for step in state["steps"]] == [
        "skipped", "skipped", "skipped", "skipped", "passed",
    ]
    assert state["steps"][0]["diagnostics"]["selected_strategy"] == "auto_page_skip"
    assert state["steps"][0]["diagnostics"]["auto_page_skip"]["resume_step_id"] == "sign"
    current_probe = state["steps"][0]["diagnostics"]["auto_page_skip"]["current_probe"]
    assert current_probe["matched"] is False


def test_robust_fill_retries_when_loading_render_resets_value(page):
    page.set_content(
        """
        <div id="field-root">
          <textarea id="address" name="address" aria-label="详细地址"></textarea>
        </div>
        <script>
          window.__fillCount = 0;
          const bind = () => {
            const field = document.querySelector('#address');
            field.addEventListener('input', () => {
              window.__fillCount += 1;
              if (window.__fillCount !== 1) return;
              const loading = document.createElement('div');
              loading.className = 'van-toast--loading';
              loading.textContent = '加载中...';
              Object.assign(loading.style, {
                position: 'fixed', inset: '0', display: 'block', background: 'rgba(0,0,0,.2)'
              });
              document.body.appendChild(loading);
              setTimeout(() => {
                document.querySelector('#field-root').innerHTML =
                  '<textarea id="address" name="address" aria-label="详细地址"></textarea>';
                loading.remove();
                bind();
              }, 180);
            });
          };
          bind();
        </script>
        """
    )
    element = {
        "id": "wel_address",
        "tag": "textarea",
        "role": "textbox",
        "accessible_name": "详细地址",
        "locators": [{"strategy": "id", "value": "address"}],
        "fingerprint": {
            "tag": "textarea",
            "role": "textbox",
            "accessible_name": "详细地址",
            "attrs": {
                "id": "address", "name": "address", "testid": "", "placeholder": "", "type": "",
            },
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 3000})

    runner.execute_step({"action": "fill", "element": element, "value": "西湖", "timeout": 3000})

    assert page.locator("#address").input_value() == "西湖"
    assert page.evaluate("window.__fillCount") == 2
    stability = runner.last_locator_diagnostics["fill_stability"]
    assert stability["verified"] is True
    assert len(stability["attempts"]) == 2


def test_assert_text_falls_back_to_visible_page_text_when_bound_element_changed(page):
    page.set_content(
        """
        <button id="submit">已提交</button>
        <div id="agreement" style="display:none">数字证书授权使用协议</div>
        <div class="van-toast--loading" style="position:fixed;inset:0">加载中...</div>
        <script>
          setTimeout(() => {
            document.querySelector('.van-toast--loading').remove();
            document.querySelector('#agreement').style.display = 'block';
          }, 180);
        </script>
        """
    )
    stale_button = {
        "id": "wel_sign",
        "tag": "button",
        "role": "button",
        "accessible_name": "去签署",
        "locators": [{"strategy": "id", "value": "submit"}],
        "fingerprint": {
            "tag": "button",
            "role": "button",
            "accessible_name": "去签署",
            "text": "去签署",
            "attrs": {"id": "submit", "name": "", "testid": "", "placeholder": "", "type": ""},
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 2000})

    runner.execute_step({
        "action": "assert_text",
        "element": stale_button,
        "value": "数字证书授权使用协议",
        "timeout": 2000,
    })

    fallback = runner.last_locator_diagnostics["assert_text_fallback"]
    assert fallback["matched"] is True
    assert fallback["bound_element_text"] == "已提交"


def test_assert_text_falls_back_to_ocr_for_rasterized_contract(page, monkeypatch):
    page.set_content(
        """
        <button id="submit">已提交</button>
        <img class="contractPage"
             src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
             style="width:375px;height:485px">
        """
    )
    stale_button = {
        "id": "wel_sign",
        "tag": "button",
        "role": "button",
        "accessible_name": "去签署",
        "locators": [{"strategy": "id", "value": "submit"}],
    }

    class OcrResult:
        returncode = 0
        stdout = "数字证书授权使用协议".encode("utf-8")
        stderr = b""

    monkeypatch.setattr(web_flow_runner.shutil, "which", lambda _: "/usr/bin/tesseract")
    monkeypatch.setattr(
        web_flow_runner.subprocess,
        "run",
        lambda *args, **kwargs: OcrResult(),
    )
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 1000})

    runner.execute_step({
        "action": "assert_text",
        "element": stale_button,
        "value": "数字证书授权使用协议",
        "timeout": 1000,
    })

    fallback = runner.last_locator_diagnostics["assert_text_fallback"]
    assert fallback["strategy"] == "page_image_ocr"
    assert runner.last_locator_diagnostics["assert_text_image_ocr"]["matched"] is True


def test_assert_error_fails_when_ocr_upload_error_toast_is_detected(page):
    page.set_content(
        """
        <main>
          <div class="van-toast" style="position:fixed;left:12px;top:12px">OCR识别失败，请重新上传</div>
        </main>
        """
    )
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 1000})

    with pytest.raises(AssertionError, match="检测到错误提示: OCR识别失败，请重新上传"):
        runner.execute_step({
            "action": "assert_error",
            "value": "识别失败",
            "timeout": 1000,
        })

    result = runner.last_locator_diagnostics["assert_error"]
    assert result["matched"] is True
    assert "OCR识别失败" in result["message"]


def test_assert_error_fails_for_any_visible_prompt_regardless_of_text(page):
    page.set_content(
        """
        <main>
          <div class="van-toast" style="position:fixed;left:12px;top:12px">
            当前申请状态不在可签约状态
          </div>
        </main>
        """
    )
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 200})

    with pytest.raises(AssertionError, match="检测到错误提示: 当前申请状态不在可签约状态"):
        runner.execute_step({
            "action": "assert_error",
            "value": "识别失败",
            "timeout": 200,
        })

    result = runner.last_locator_diagnostics["assert_error"]
    assert result["matched"] is True
    assert result["source"] == ".van-toast"
    assert result["message"] == "当前申请状态不在可签约状态"


def test_assert_error_ignores_loading_prompt(page):
    page.set_content(
        """
        <main>
          <div class="van-toast van-toast--loading" style="position:fixed;left:12px;top:12px">
            加载中...
          </div>
        </main>
        """
    )
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "steps": [],
        "timeout": 200,
        "smart_wait": False,
    })

    runner.execute_step({
        "action": "assert_error",
        "value": "加载中",
        "timeout": 200,
    })

    result = runner.last_locator_diagnostics["assert_error"]
    assert result["matched"] is False
    ignored = result["attempts"][-1]["ignored_loading"]
    assert ignored[0]["text"] == "加载中..."


def test_assert_error_passes_when_no_error_is_detected(page):
    page.set_content("<main><h1>提交成功</h1></main>")
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 200})

    runner.execute_step({
        "action": "assert_error",
        "value": "服务器异常",
        "timeout": 200,
    })

    result = runner.last_locator_diagnostics["assert_error"]
    assert result["matched"] is False


def test_flow_run_treats_unmatched_error_assertion_as_passed(page, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(web_flow_runner, "allure", None)
    monkeypatch.setenv("TASK_ID", "error_guard_passes")
    monkeypatch.setenv("FLOW_CASE_FILE", "test_error_guard.yaml")
    page.set_content("<main><h1>提交成功</h1></main>")
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "name": "错误拦截断言",
        "timeout": 200,
        "steps": [
            {"id": "guard", "action": "assert_error", "name": "禁止出现服务器异常", "value": "服务器异常", "timeout": 200},
            {"id": "after_guard", "action": "wait", "name": "继续执行", "seconds": 0},
        ],
    })

    runner.run()

    state = step_store.read("error_guard_passes")
    assert state["status"] == "success"
    assert [step["status"] for step in state["steps"]] == ["passed", "passed"]
    assert state["steps"][0]["assertion_matched"] is False


def test_flow_run_treats_matched_error_assertion_as_failed(page, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(web_flow_runner, "allure", None)
    monkeypatch.setenv("TASK_ID", "error_guard_fails")
    monkeypatch.setenv("FLOW_CASE_FILE", "test_error_guard.yaml")
    page.set_content(
        '<main><div class="van-toast" style="position:fixed;left:12px;top:12px">服务器异常，请联系管理员</div></main>'
    )
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "name": "错误拦截断言",
        "timeout": 200,
        "steps": [
            {"id": "guard", "action": "assert_error", "name": "禁止出现服务器异常", "value": "服务器异常", "timeout": 200},
            {"id": "after_guard", "action": "wait", "name": "继续执行", "seconds": 0},
        ],
    })

    with pytest.raises(AssertionError, match="1 个断言失败"):
        runner.run()

    state = step_store.read("error_guard_fails")
    assert state["status"] == "failed"
    assert [step["status"] for step in state["steps"]] == ["failed", "passed"]
    assert state["steps"][0]["assertion_matched"] is True
    assert "错误断言命中" in state["steps"][0]["log"]


def test_flow_run_marks_any_visible_prompt_as_matched_error_assertion(page, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(web_flow_runner, "allure", None)
    monkeypatch.setenv("TASK_ID", "any_prompt_error_guard")
    monkeypatch.setenv("FLOW_CASE_FILE", "test_error_guard.yaml")
    page.set_content(
        '<main><div class="van-toast" style="position:fixed;left:12px;top:12px">当前申请状态不在可签约状态</div></main>'
    )
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "name": "错误拦截断言",
        "timeout": 200,
        "steps": [
            {"id": "guard", "action": "assert_error", "name": "禁止出现任意页面提示", "value": "", "timeout": 200},
            {"id": "after_guard", "action": "wait", "name": "继续执行", "seconds": 0},
        ],
    })

    with pytest.raises(AssertionError, match="1 个断言失败"):
        runner.run()

    state = step_store.read("any_prompt_error_guard")
    assert state["status"] == "failed"
    assert [step["status"] for step in state["steps"]] == ["failed", "passed"]
    assert state["steps"][0]["assertion_matched"] is True
    assert state["steps"][0]["diagnostics"]["assert_error"]["message"] == "当前申请状态不在可签约状态"


def test_score_web_candidate_prefers_stable_semantics():
    fingerprint = {
        "tag": "button",
        "role": "button",
        "accessible_name": "登录",
        "text": "登录",
        "attrs": {"testid": "login-submit", "id": "", "name": "", "placeholder": "", "type": "submit"},
        "parent": {"tag": "form", "text": "账号 密码 登录"},
        "normalized_position": {"x": 0.5, "y": 0.7},
    }
    exact = {
        **fingerprint,
        "css": "[data-testid=login-submit]",
    }
    similar = {
        "tag": "button",
        "role": "button",
        "accessible_name": "注册",
        "text": "注册",
        "attrs": {"testid": "register-submit", "id": "", "name": "", "placeholder": "", "type": "submit"},
        "parent": {"tag": "form", "text": "账号 密码 注册"},
        "normalized_position": {"x": 0.7, "y": 0.7},
    }
    exact_score, _ = score_web_candidate(fingerprint, exact)
    similar_score, _ = score_web_candidate(fingerprint, similar)
    assert exact_score >= 150
    assert exact_score > similar_score + 60


def test_locator_diagnostics_records_fallback_attempts():
    scope = _FakeScope({
        ("testid", "missing"): 0,
        ("role", "button", "提交"): 1,
    })
    runner = WebFlowRunner(_FakePage(scope), {"flow_version": 2, "steps": []})

    locator = runner.locate({
        "name": "提交",
        "locators": [
            {"strategy": "testid", "value": "missing"},
            {"strategy": "role", "role": "button", "name": "提交", "value": "提交"},
        ],
    }, timeout=200)

    assert locator.count() == 1
    assert runner.last_locator_diagnostics["selected_strategy"] == "role"
    assert [item["matched"] for item in runner.last_locator_diagnostics["attempts"]] == [False, True]


def test_locator_diagnostics_reports_semantic_healing():
    fingerprint = {
        "tag": "button",
        "role": "button",
        "accessible_name": "登录",
        "text": "登录",
        "attrs": {"testid": "login-submit", "id": "", "name": "", "placeholder": "", "type": "submit"},
        "parent": {"tag": "form", "text": "账号 密码 登录"},
        "normalized_position": {"x": 0.5, "y": 0.7},
        "minimum_score": 55,
        "minimum_gap": 8,
    }
    scope = _FakeScope(healing_candidates=[{
        "css": "#healed",
        **fingerprint,
    }])
    runner = WebFlowRunner(_FakePage(scope), {"flow_version": 2, "steps": []})

    locator = runner.locate({"name": "登录", "locators": [], "fingerprint": fingerprint}, timeout=200)

    assert locator.count() == 1
    assert runner.last_locator_diagnostics["selected_strategy"] == "semantic_healing"
    assert runner.last_locator_diagnostics["attempts"][-1]["matched"] is True


def test_ai_self_healing_repairs_changed_button_locator(page, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "runtime" / "platform.db"))
    monkeypatch.setenv("PROJECT_ID", "default")
    monkeypatch.setenv("AI_SELF_HEAL_APPLY", "1")
    db.init_db()
    old_locators = [
        {"strategy": "id", "value": "pay"},
        {"strategy": "css", "value": "button#pay"},
    ]
    fingerprint = {
        "tag": "button",
        "role": "button",
        "accessible_name": "立即支付",
        "text": "立即支付",
        "attrs": {
            "testid": "",
            "id": "pay",
            "name": "",
            "placeholder": "",
            "type": "button",
        },
        "parent": {"tag": "body", "text": "立即支付"},
        "normalized_position": {"x": 0.5, "y": 0.2},
        "minimum_score": 55,
        "minimum_gap": 8,
    }
    db.execute(
        "INSERT INTO web_elements(id,project_id,name,page_url,tag_name,element_role,signature,"
        "locators,fingerprint,element_context,usage_count,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "wel_pay", "default", "立即支付", "https://example.test/pay", "button", "button",
            "sig-pay",
            json.dumps(old_locators, ensure_ascii=False),
            json.dumps(fingerprint, ensure_ascii=False),
            "{}",
            1,
            "2026-07-24T00:00:00Z",
            "2026-07-24T00:00:00Z",
        ),
    )
    page.set_content(
        """
        <button class="pay-btn" type="button" onclick="window.paid=true">立即支付</button>
        """
    )
    element = {
        "id": "wel_pay",
        "name": "立即支付",
        "tag": "button",
        "role": "button",
        "accessible_name": "立即支付",
        "locators": old_locators,
        "fingerprint": fingerprint,
    }
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "project_id": "default"})

    target = runner.locate(element, timeout=1000)
    target.click()

    repair = runner.last_locator_diagnostics["self_heal_repair"]
    assert page.evaluate("window.paid") is True
    assert runner.last_locator_diagnostics["selected_strategy"] == "semantic_healing"
    assert repair["reason"] == "元素属性变化"
    assert "id: pay ->" in repair["details"][0]
    assert repair["new_element"]["attrs"]["class"] == "pay-btn"
    assert repair["confidence_percent"] >= 90
    assert repair["should_apply"] is True
    assert repair["applied"] is True

    row = db.execute(
        "SELECT locators,element_context FROM web_elements WHERE id=?",
        ("wel_pay",),
        fetch=True,
    )[0]
    persisted_locators = json.loads(row["locators"])
    assert persisted_locators[0]["strategy"] == "css"
    assert "pay-btn" in persisted_locators[0]["value"]
    history = json.loads(row["element_context"])["self_heal_history"]
    assert history[0]["reason"] == "元素属性变化"


def test_execute_scroll_step_uses_recorded_delta():
    page = _FakePage(_FakeScope())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "scroll", "delta_x": 25, "delta_y": -600})

    assert page.mouse.wheels == [(25, -600)]


def test_execute_scroll_step_moves_to_recorded_point():
    page = _FakePage(_FakeScope())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "scroll", "x": 120, "y": 320, "delta_y": 480})

    assert page.mouse.moves == [(120.0, 320.0, None)]
    assert page.mouse.wheels == [(0, 480)]


def test_mobile_context_options_enable_touch_for_mobile_viewport():
    options = mobile_context_options({"width": 390, "height": 844})

    assert options["viewport"] == {"width": 390, "height": 844}
    assert options["is_mobile"] is True
    assert options["has_touch"] is True


def test_execute_scroll_step_can_move_touch_driven_picker(page):
    page.set_content(_touch_picker_markup())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "scroll", "x": 180, "y": 720, "delta_y": 220})

    assert int(page.locator("#offset").inner_text()) < 0


def test_execute_scroll_step_redirects_center_point_to_visible_picker(page):
    page.set_content(_touch_picker_markup())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "scroll", "x": 180, "y": 400, "delta_y": 220, "scroll_scope": "popup"})

    assert int(page.locator("#offset").inner_text()) < 0


def test_execute_popup_scroll_uses_single_touch_gesture(page):
    page.set_content(_multi_event_picker_markup())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "scroll", "x": 180, "y": 400, "delta_y": 220, "scroll_scope": "popup"})

    counts = page.evaluate("window.__eventCounts")
    assert counts["wheel"] == 0
    assert counts["pointermove"] == 0
    assert counts["mousemove"] == 0
    assert counts["touchmove"] > 0
    assert page.evaluate("window.__offset") < 0


def test_execute_popup_select_text_scrolls_and_clicks_option(page):
    page.set_content(_popup_text_picker_markup())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "popup_select_text", "value": "黑龙江省"})

    assert page.locator("#selected").inner_text() == "黑龙江省"
    assert runner.last_locator_diagnostics["selected_strategy"] == "popup_text"


def test_execute_popup_select_text_waits_for_delayed_popup(page):
    page.set_content(
        """
        <style>
          .van-popup {
            position: fixed; left: 0; right: 0; bottom: 0; height: 200px;
            display: flex; align-items: center; justify-content: center; background: white;
          }
          .option { width: 120px; height: 44px; }
        </style>
        <div id="selected"></div>
        <script>
          setTimeout(() => {
            const popup = document.createElement('div');
            popup.className = 'van-popup';
            popup.innerHTML = '<button class="option" role="button">浙江省</button>';
            popup.querySelector('.option').addEventListener('click', () => {
              document.querySelector('#selected').textContent = '浙江省';
            });
            document.body.appendChild(popup);
          }, 350);
        </script>
        """
    )
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 2000})

    started_at = time.monotonic()
    runner.execute_step({"action": "popup_select_text", "value": "浙江省", "timeout": 2000})

    assert time.monotonic() - started_at >= 0.3
    assert page.locator("#selected").inner_text() == "浙江省"
    assert runner.last_locator_diagnostics["selected_strategy"] == "popup_text"


def test_execute_popup_select_text_supports_slash_separated_path(page):
    page.set_content(_popup_multi_column_picker_markup())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "popup_select_text", "value": "浙江省/杭州市/拱墅区"})

    assert page.locator("#province").inner_text() == "浙江省"
    assert page.locator("#city").inner_text() == "杭州市"
    assert page.locator("#district").inner_text() == "拱墅区"
    assert runner.last_locator_diagnostics["result"]["target_text"] == "浙江省/杭州市/拱墅区"
    assert len(runner.last_locator_diagnostics["result"]["selected_parts"]) == 3


def test_execute_popup_select_text_uses_vant_api_for_dependent_area_columns(page):
    page.set_content(_vant_area_picker_markup())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "popup_select_text", "value": "浙江省/杭州市/拱墅区"})

    assert page.locator("#province").inner_text() == "浙江省"
    assert page.locator("#city").inner_text() == "杭州市"
    assert page.locator("#county").inner_text() == "拱墅区"


def test_execute_popup_select_text_can_resolve_city_from_vant_area_data(page):
    page.set_content(_vant_area_picker_markup())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "popup_select_text", "value": "杭州市"})

    assert page.locator("#province").inner_text() == "浙江省"
    assert page.locator("#city").inner_text() == "杭州市"


def test_execute_popup_select_text_probes_dependent_dom_columns_for_city(page):
    page.set_content(_vant_dom_only_area_picker_markup())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "popup_select_text", "value": "杭州市"})

    assert page.evaluate("window.__selectedArea") == {
        "province": "浙江省",
        "city": "杭州市",
        "county": "上城区",
    }
    result = runner.last_locator_diagnostics["result"]
    assert result["selected_path"] == ["浙江省", "杭州市"]
    assert result["method"].endswith("+dependent-probe")


def test_execute_popup_select_text_uses_vant_dom_items_without_component_api(page):
    page.set_content(_vant_dom_only_area_picker_markup())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "popup_select_text", "value": "浙江省/杭州市/拱墅区"})

    assert page.evaluate("window.__selectedArea") == {
        "province": "浙江省",
        "city": "杭州市",
        "county": "拱墅区",
    }
    methods = [part["method"] for part in runner.last_locator_diagnostics["result"]["selected_parts"]]
    assert methods == ["vant-item-click", "vant-item-click", "vant-item-click"]


def test_execute_scroll_step_keeps_scrollable_background_still_for_picker(page):
    page.set_content(_touch_picker_markup(scrollable_page=True))
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "scroll", "x": 180, "y": 720, "delta_y": 220, "scroll_scope": "popup"})

    assert int(page.locator("#offset").inner_text()) < 0
    assert page.evaluate("window.scrollY") == 0


def test_execute_scroll_without_popup_scope_can_scroll_background(page):
    page.set_content(_touch_picker_markup(scrollable_page=True))
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({"action": "scroll", "x": 180, "y": 400, "delta_y": 220})

    assert int(page.locator("#offset").inner_text()) == 0
    assert page.evaluate("window.scrollY") > 0


def test_execute_popup_scroll_scope_requires_visible_popup(page):
    page.set_content("<main style='height:1200px'>普通页面</main>")
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    with pytest.raises(ValueError, match="未检测到可滚动弹框"):
        runner.execute_step({"action": "scroll", "x": 180, "y": 400, "delta_y": 220, "scroll_scope": "popup"})


def test_browser_recorder_scroll_can_move_touch_driven_picker(page):
    page.set_content(_touch_picker_markup())
    session = BrowserRecordingSession(
        session_id="picker-scroll-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
    )

    step = session._perform_action(page, {"action": "scroll", "x": 180, "y": 720, "delta_y": 220})

    assert int(page.locator("#offset").inner_text()) < 0
    assert step["action"] == "scroll"
    assert step["x"] == 180.0
    assert step["y"] == 720.0


def test_browser_recorder_scroll_redirects_center_point_to_visible_picker(page):
    page.set_content(_touch_picker_markup())
    session = BrowserRecordingSession(
        session_id="picker-scroll-center-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
    )

    step = session._perform_action(page, {"action": "scroll", "x": 180, "y": 400, "delta_y": 220, "scroll_scope": "popup"})

    assert int(page.locator("#offset").inner_text()) < 0
    assert step["action"] == "scroll"
    assert step["scroll_scope"] == "popup"
    assert step["x"] == 180.0
    assert step["y"] == 400.0


def test_browser_recorder_popup_scroll_caps_recorded_delta(page):
    page.set_content(_touch_picker_markup())
    session = BrowserRecordingSession(
        session_id="picker-scroll-cap-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
    )

    step = session._perform_action(page, {"action": "scroll", "x": 180, "y": 400, "delta_y": 600, "scroll_scope": "popup"})

    assert step["delta_y"] == web_flow_runner.POPUP_SCROLL_DELTA_LIMIT
    assert int(page.locator("#offset").inner_text()) < 0


def test_browser_recorder_popup_select_text_records_replayable_step(page):
    page.set_content(_popup_text_picker_markup())
    session = BrowserRecordingSession(
        session_id="picker-select-text-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
    )

    step = session._perform_action(page, {"action": "popup_select_text", "value": "黑龙江省"})

    assert page.locator("#selected").inner_text() == "黑龙江省"
    assert step["action"] == "popup_select_text"
    assert step["value"] == "黑龙江省"
    assert step["option_label"] == "黑龙江省"


def test_browser_recorder_popup_select_text_records_slash_separated_path(page):
    page.set_content(_popup_multi_column_picker_markup())
    session = BrowserRecordingSession(
        session_id="picker-select-path-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
    )

    step = session._perform_action(page, {"action": "popup_select_text", "value": "浙江省/杭州市/拱墅区"})

    assert page.locator("#province").inner_text() == "浙江省"
    assert page.locator("#city").inner_text() == "杭州市"
    assert page.locator("#district").inner_text() == "拱墅区"
    assert step["action"] == "popup_select_text"
    assert step["value"] == "浙江省/杭州市/拱墅区"


def test_browser_recorder_popup_select_text_resolves_vant_area_city(page):
    page.set_content(_vant_area_picker_markup())
    session = BrowserRecordingSession(
        session_id="picker-select-vant-city-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
    )

    step = session._perform_action(page, {"action": "popup_select_text", "value": "杭州市"})

    assert page.locator("#province").inner_text() == "浙江省"
    assert page.locator("#city").inner_text() == "杭州市"
    assert step["action"] == "popup_select_text"
    assert step["value"] == "杭州市"


def test_browser_recorder_popup_select_text_probes_dependent_dom_columns(page):
    page.set_content(_vant_dom_only_area_picker_markup())
    session = BrowserRecordingSession(
        session_id="picker-select-dependent-dom-city-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
    )

    step = session._perform_action(page, {"action": "popup_select_text", "value": "杭州市"})

    assert page.evaluate("window.__selectedArea") == {
        "province": "浙江省",
        "city": "杭州市",
        "county": "上城区",
    }
    assert step["action"] == "popup_select_text"
    assert step["value"] == "杭州市"


def test_execute_coordinate_drag_step_uses_recorded_positions():
    page = _FakePage(_FakeScope())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({
        "action": "drag",
        "source_position": {"x": 10, "y": 20},
        "target_position": {"x": 80, "y": 120},
    })

    assert page.mouse.moves == [(10.0, 20.0, None), (80.0, 120.0, 12)]
    assert page.mouse.downs == 1
    assert page.mouse.ups == 1
    assert runner.last_locator_diagnostics["selected_strategy"] == "coordinate_drag"


def test_ai_repair_strategy_overrides_runtime_without_mutating_flow(monkeypatch):
    original_flow = {
        "flow_version": 2,
        "steps": [],
        "timeout": 1000,
        "smart_wait": False,
        "self_heal": {"auto_apply": False, "apply_threshold": 0.8},
    }
    monkeypatch.setenv("RUNTIME_CONTEXT_JSON", json.dumps({
        "local": {
            "_runnergo_repair_strategy": {
                "timeout_ms": 20000,
                "smart_wait": True,
                "self_heal": True,
                "self_heal_apply_threshold": 0.92,
                "retry_count": 1,
                "_policy_id": 77,
            },
        },
    }))

    runner = WebFlowRunner(_FakePage(_FakeScope()), original_flow)

    assert runner.default_timeout == 20000
    assert runner.smart_wait_enabled is True
    assert runner.self_heal_enabled is True
    assert runner.self_heal_apply is False
    assert runner.self_heal_apply_threshold == 0.92
    assert original_flow["timeout"] == 1000
    assert original_flow["smart_wait"] is False


def test_ai_repair_retry_requires_safe_action(monkeypatch):
    monkeypatch.setenv("RUNTIME_CONTEXT_JSON", json.dumps({
        "local": {"_runnergo_repair_strategy": {"retry_count": 1, "_policy_id": 77}},
    }))
    runner = WebFlowRunner(_FakePage(_FakeScope()), {
        "flow_version": 2,
        "step_execution_mode": "real",
        "steps": [{"action": "extract", "name": "只读提取", "source": "url", "save_as": "currentUrl"}],
    })
    runner.page.url = "https://example.test/current"
    runner._smart_wait_for_page = lambda *_args, **_kwargs: {"ready": True}
    original_execute = runner.execute_step
    calls = {"count": 0}

    def flaky_execute(step):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary")
        return original_execute(step)

    runner.execute_step = flaky_execute
    runner.run()

    assert calls["count"] == 2
    assert runner.step_results[0]["diagnostics"]["repair_retry"]["policy_id"] == 77
    assert WebFlowRunner._repair_retry_allowed({"action": "click"}, "click") is False
    assert WebFlowRunner._repair_retry_allowed(
        {"action": "click", "repair_retry_safe": True}, "click"
    ) is True


def test_execute_drag_step_locates_source_and_target():
    page = _FakePage(_FakeScope())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})
    source, target = _FakeLocator(), _FakeLocator()
    located = iter([source, target])

    def locate(_element, _timeout):
        runner.last_locator_diagnostics = {
            "matched": True,
            "selected_strategy": "testid",
            "attempts": [{"strategy": "testid", "matched": True}],
        }
        return next(located)

    runner.locate = locate
    runner.execute_step({
        "action": "drag",
        "element": {"name": "源元素"},
        "target_element": {"name": "目标元素"},
    })

    assert source.dragged_to is target
    assert runner.last_locator_diagnostics["selected_strategy"] == "drag"


def test_execute_select_step_clicks_non_native_picker_option():
    page = _FakePage(_FakeScope())
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})
    option = _FakeLocator(tag="li")

    runner.locate = lambda _element, _timeout: option

    runner.execute_step({
        "action": "select",
        "element": {"name": "北京市"},
        "option_label": "北京市",
    })

    assert option.clicked is True
    assert option.selected_options == []


def test_execute_select_step_clicks_non_native_labeled_option(page):
    page.set_content(
        """
        <button id="custom-select">自定义下拉</button>
        <div id="menu" hidden>
          <button>选项A</button>
          <button id="option-b">选项B</button>
        </div>
        <div id="result">等待</div>
        <script>
          document.querySelector('#custom-select').addEventListener('click', () => {
            document.querySelector('#menu').hidden = false;
          });
          document.querySelector('#option-b').addEventListener('click', () => {
            document.querySelector('#result').textContent = '选项B';
          });
        </script>
        """
    )
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    runner.execute_step({
        "action": "select",
        "element": {
            "name": "自定义下拉",
            "locators": [{"strategy": "css", "value": "#custom-select"}],
        },
        "option_label": "选项B",
    })

    assert page.locator("#result").inner_text() == "选项B"
    assert runner.last_locator_diagnostics["selected_strategy"] == "visible_text"


def test_browser_recorder_select_non_native_records_selected_option(page):
    page.set_content(
        """
        <button id="custom-select">自定义下拉</button>
        <div id="menu" hidden>
          <button>选项A</button>
          <button id="option-b">选项B</button>
        </div>
        <div id="result">等待</div>
        <script>
          document.querySelector('#custom-select').addEventListener('click', () => {
            document.querySelector('#menu').hidden = false;
          });
          document.querySelector('#option-b').addEventListener('click', () => {
            document.querySelector('#result').textContent = '选项B';
          });
        </script>
        """
    )
    session = BrowserRecordingSession(
        session_id="custom-select-recorder-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
    )
    box = page.locator("#custom-select").bounding_box()

    step = session._perform_action(page, {
        "action": "select",
        "x": box["x"] + box["width"] / 2,
        "y": box["y"] + box["height"] / 2,
        "option_label": "选项B",
    })

    assert page.locator("#result").inner_text() == "选项B"
    assert step["action"] == "select"
    assert step["option_label"] == "选项B"


def test_runtime_url_replaces_recorded_origin(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://staging.example.com")
    runner = WebFlowRunner(None, {
        "flow_version": 2,
        "base_url": "https://test.example.com/app/home",
        "steps": [],
    })
    assert runner._runtime_url("https://test.example.com/order/1") == "https://staging.example.com/order/1"


def test_element_signature_distinguishes_iframe_context():
    base = {
        "page_url": "https://example.com/frame",
        "fingerprint": {
            "tag": "button",
            "role": "button",
            "accessible_name": "提交",
            "attrs": {"testid": "submit"},
        },
    }
    left = {**base, "frame_path": [{"selector": "#left-frame"}]}
    right = {**base, "frame_path": [{"selector": "#right-frame"}]}
    assert _element_signature("project", left) != _element_signature("project", right)


def test_flow_v2_generates_generic_pytest_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ROOT", str(tmp_path))
    monkeypatch.setattr(
        project_service,
        "get_project",
        lambda project_id: {"id": project_id, "case_dir": "cases"},
    )
    flow = {
        "flow_version": 2,
        "name": "登录流程",
        "base_url": "https://example.com",
        "steps": [{"action": "goto", "url": "https://example.com"}],
    }
    case_service.save_case(
        "default",
        "ui",
        "test_recorded_login.yaml",
        yaml.safe_dump(flow, allow_unicode=True, sort_keys=False),
    )
    runner_path = tmp_path / "tests" / "ui" / "test_recorded_login.py"
    content = runner_path.read_text(encoding="utf-8")
    assert "AUTO-GENERATED WEB FLOW RUNNER" in content
    assert "runtime_data_row" in content
    assert "run_web_flow(browser, flow)" in content
    assert os.path.exists(tmp_path / "cases" / "ui" / "test_recorded_login.yaml")


def test_load_web_flow_forces_page_aware_execution_for_historical_case(tmp_path):
    path = tmp_path / "historical.yaml"
    path.write_text(yaml.safe_dump({
        "flow_version": 2,
        "step_execution_mode": "adaptive",
        "steps": [{"action": "wait", "seconds": 0}],
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")

    flow = web_flow_runner.load_web_flow(str(path))

    assert flow["step_execution_mode"] == "page_aware"


def test_locate_waits_for_async_render(page):
    page.set_content(
        "<main id='app'></main>"
        "<script>setTimeout(() => { document.querySelector('#app').innerHTML = "
        "'<label>手机<input name=phone placeholder=请输入手机号></label>'; }, 80)</script>"
    )
    element = {
        "name": "手机",
        "locators": [
            {"strategy": "role", "role": "textbox", "name": "手机", "value": "手机"},
            {"strategy": "placeholder", "value": "请输入手机号"},
            {"strategy": "name", "value": "phone"},
        ],
        "fingerprint": {
            "tag": "input",
            "role": "textbox",
            "accessible_name": "手机",
            "text": "",
            "attrs": {"testid": "", "id": "", "name": "phone", "placeholder": "请输入手机号", "type": ""},
            "parent": {"tag": "label", "text": "手机"},
            "normalized_position": {"x": 0.5, "y": 0.5},
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 1000})

    locator = runner.locate(element, timeout=1000)

    assert locator.get_attribute("name") == "phone"
    assert runner.last_locator_diagnostics["matched"] is True
    assert runner.last_locator_diagnostics["elapsed_ms"] >= 50


def test_locate_rejects_reused_id_until_semantic_target_is_rendered(page):
    page.set_content(
        """
        <label>手机<input id="van-field-1-input" type="tel" placeholder="请输入手机号"></label>
        <script>
          setTimeout(() => {
            document.body.innerHTML = '<label>申请地区<input id="van-field-1-input" type="text" placeholder="请选择"></label>';
          }, 180);
        </script>
        """
    )
    element = {
        "name": "申请地区",
        "tag": "input",
        "role": "textbox",
        "input_type": "text",
        "locators": [{"strategy": "id", "value": "van-field-1-input"}],
        "fingerprint": {
            "tag": "input",
            "role": "textbox",
            "accessible_name": "申请地区",
            "text": "",
            "attrs": {
                "testid": "", "id": "van-field-1-input", "name": "",
                "placeholder": "请选择", "type": "text",
            },
            "parent": {"tag": "label", "text": "申请地区"},
            "normalized_position": {"x": 0.5, "y": 0.5},
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 1200})

    locator = runner.locate(element, timeout=1200)

    assert locator.get_attribute("placeholder") == "请选择"
    assert runner.last_locator_diagnostics["elapsed_ms"] >= 140
    validation = runner.last_locator_diagnostics["attempts"][0]["candidate_validation"]
    assert validation["matched"] is True
    assert validation["actual"]["accessible_name"] == "申请地区"


def test_locate_selects_semantic_match_from_duplicate_placeholders(page):
    page.set_content(
        """
        <label>申请地区<input id="area" placeholder="请选择"></label>
        <label>贷款用途<input id="purpose" name="picker" placeholder="请选择"></label>
        <label>婚姻状况<input id="marriage" name="picker" placeholder="请选择"></label>
        """
    )
    element = {
        "name": "贷款用途",
        "tag": "input",
        "role": "textbox",
        "input_type": "text",
        "locators": [{"strategy": "placeholder", "value": "请选择"}],
        "fingerprint": {
            "tag": "input",
            "role": "textbox",
            "accessible_name": "贷款用途",
            "text": "",
            "attrs": {
                "testid": "", "id": "purpose", "name": "picker",
                "placeholder": "请选择", "type": "text",
            },
            "parent": {"tag": "label", "text": "贷款用途"},
            "normalized_position": {"x": 0.5, "y": 0.5},
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 1000})

    locator = runner.locate(element, timeout=1000)

    assert locator.get_attribute("id") == "purpose"
    validation = runner.last_locator_diagnostics["attempts"][0]["candidate_validation"]
    assert validation["candidate_count"] == 3
    assert validation["selected_index"] == 1


def test_probe_rejects_same_name_attribute_when_accessible_name_conflicts(page):
    page.set_content(
        """
        <label>姓名<input id="borrower-name" name="name" placeholder="请输入姓名"></label>
        """
    )
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 500})
    step = {
        "action": "fill",
        "name": "输入 联系人姓名",
        "element": {
            "name": "联系人姓名",
            "locators": [{"strategy": "name", "value": "name"}],
            "fingerprint": {
                "tag": "input",
                "role": "textbox",
                "accessible_name": "联系人姓名",
                "text": "",
                "attrs": {
                    "testid": "", "id": "van-field-21-input", "name": "name",
                    "placeholder": "请填写", "type": "text",
                },
                "parent": {"tag": "label", "text": "联系人姓名"},
                "normalized_position": {"x": 0.64, "y": 0.58},
                "minimum_score": 55,
                "minimum_gap": 8,
            },
        },
    }

    probe = runner._probe_step_visible(step, timeout_ms=250)

    assert probe["matched"] is False
    validation = probe["probe"]["attempts"][0]["candidate_validation"]
    assert validation["reason"] == "semantic-conflict"
    assert validation["actual"]["accessible_name"] == "姓名"


def test_probe_rejects_shorter_address_label_for_detail_address(page):
    page.set_content(
        """
        <label>地址<textarea id="ocr-address" name="address" placeholder="请输入"></textarea></label>
        """
    )
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 500})
    step = {
        "action": "fill",
        "name": "输入 详细地址",
        "element": {
            "name": "详细地址",
            "locators": [{"strategy": "name", "value": "address"}],
            "fingerprint": {
                "tag": "textarea",
                "role": "textbox",
                "accessible_name": "详细地址",
                "text": "",
                "attrs": {
                    "testid": "", "id": "van-field-10-input", "name": "address",
                    "placeholder": "请填写包含门牌号的地址", "type": "",
                },
                "parent": {"tag": "label", "text": "详细地址"},
                "normalized_position": {"x": 0.64, "y": 0.32},
                "minimum_score": 55,
                "minimum_gap": 8,
            },
        },
    }

    probe = runner._probe_step_visible(step, timeout_ms=250)

    assert probe["matched"] is False
    validation = probe["probe"]["attempts"][0]["candidate_validation"]
    assert validation["reason"] == "semantic-conflict"
    assert validation["actual"]["accessible_name"] == "地址"


def test_auto_page_skip_single_step_when_current_step_not_on_page(page):
    page.set_content(
        """
        <label>姓名<input id="borrower-name" name="name" placeholder="请输入姓名"></label>
        <label>民族<input id="nation" name="picker" placeholder="请选择"></label>
        """
    )
    steps = [
        {
            "id": "contact_relation",
            "action": "click",
            "name": "点击 联系人关系",
            "element": {
                "name": "联系人关系",
                "locators": [{"strategy": "name", "value": "picker"}],
                "fingerprint": {
                    "tag": "input",
                    "role": "textbox",
                    "accessible_name": "联系人关系",
                    "text": "",
                    "attrs": {
                        "testid": "", "id": "van-field-22-input", "name": "picker",
                        "placeholder": "请选择", "type": "text",
                    },
                    "parent": {"tag": "label", "text": "联系人关系"},
                    "normalized_position": {"x": 0.61, "y": 0.65},
                    "minimum_score": 55,
                    "minimum_gap": 8,
                },
            },
        },
        {"id": "noop", "action": "scroll", "delta_y": 100},
    ]
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "step_execution_mode": "adaptive",
        "steps": steps,
        "timeout": 500,
    })

    target = runner._auto_skip_target(steps[0], steps, 1)

    assert target["single_step"] is True
    assert target["target_index"] == 2
    assert target["current_probe"]["matched"] is False


def test_auto_page_skip_reuses_same_page_probe_cache(page, monkeypatch):
    page.set_content("<button id='real'>提交</button>")
    steps = []
    for index in range(6):
        steps.append({
            "id": f"missing_{index}",
            "action": "click",
            "name": f"缺失步骤 {index}",
            "element": {
                "name": f"缺失步骤 {index}",
                "locators": [{"strategy": "id", "value": f"missing-{index}"}],
            },
        })
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "steps": steps,
        "timeout": 500,
        "auto_page_skip_scan_limit": 4,
    })
    monkeypatch.setattr(runner, "_auto_skip_page_signature", lambda: "same-page")
    probe_calls = []

    def fake_probe(step, timeout_ms=400):
        probe_calls.append((step["id"], timeout_ms))
        return {"matched": False, "reason": "step-not-visible"}

    monkeypatch.setattr(runner, "_probe_step_visible", fake_probe)

    first_target = runner._auto_skip_target(steps[0], steps, 1)
    second_target = runner._auto_skip_target(steps[1], steps, 2)

    assert first_target["single_step"] is True
    assert second_target["single_step"] is True
    assert [step_id for step_id, _timeout in probe_calls] == [
        "missing_0",
        "missing_1",
        "missing_2",
        "missing_3",
        "missing_4",
        "missing_5",
    ]
    assert {timeout for _step_id, timeout in probe_calls} == {200}
    assert second_target["current_probe"]["cached"] is True
    assert all(item["cached"] for item in second_target["future_probes"][:3])


def test_auto_page_skip_rejects_upload_step_matched_to_text_input(page, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", str(tmp_path / "screenshots"))
    monkeypatch.setattr(settings, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(web_flow_runner, "allure", None)
    monkeypatch.setenv("TASK_ID", "auto_skip_upload_textbox")
    monkeypatch.setenv("FLOW_CASE_FILE", "test_auto_skip_upload_textbox.yaml")
    page.set_content(
        """
        <label>姓名<input id="borrower-name" name="name" placeholder="请输入姓名"></label>
        <button id="sign" onclick="window.signed=true">去签署</button>
        """
    )
    upload_element = {
        "name": "上传文件 input元素",
        "tag": "input",
        "role": "textbox",
        "input_type": "file",
        "locators": [{"strategy": "role", "role": "textbox", "index": 0, "value": "textbox[0]"}],
        "fingerprint": {
            "tag": "input",
            "role": "textbox",
            "accessible_name": "",
            "text": "",
            "attrs": {
                "testid": "", "id": "", "name": "",
                "placeholder": "", "type": "file",
            },
            "parent": {"tag": "div", "text": ""},
            "normalized_position": {"x": 0.28, "y": 0.53},
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    sign_element = {
        "name": "去签署",
        "tag": "button",
        "role": "button",
        "locators": [{"strategy": "id", "value": "sign"}],
        "fingerprint": {
            "tag": "button",
            "role": "button",
            "accessible_name": "去签署",
            "attrs": {"testid": "", "id": "sign", "name": "", "placeholder": "", "type": ""},
        },
    }
    steps = [
        {
            "id": "upload_front",
            "action": "upload",
            "name": "上传文件 input元素",
            "element": upload_element,
            "files": ["/tmp/id-card.png"],
        },
        {
            "id": "sign",
            "action": "click",
            "name": "点击 去签署",
            "element": sign_element,
        },
    ]
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "step_execution_mode": "adaptive",
        "steps": steps,
        "timeout": 500,
    })

    runner.run()

    assert page.evaluate("window.signed") is True
    state = step_store.read("auto_skip_upload_textbox")
    assert [step["status"] for step in state["steps"]] == ["skipped", "passed"]
    skip = state["steps"][0]["diagnostics"]["auto_page_skip"]
    assert skip["resume_step_id"] == "sign"
    assert skip["current_probe"]["matched"] is False
    assert skip["current_probe"]["probe"]["attempts"][0]["candidate_validation"]["reason"] == "type-mismatch"


def test_page_aware_summary_reports_skipped_steps_instead_of_all_passing():
    runner = WebFlowRunner(None, {
        "flow_version": 2,
        "step_execution_mode": "page_aware",
        "steps": [
            {"id": "old", "action": "click", "name": "旧页面步骤"},
            {"id": "current", "action": "goto", "name": "当前页面步骤"},
        ],
    })
    runner._conditional_skip_target = lambda *_args: None
    def auto_skip(_step, _steps, index):
        if index != 1:
            return None
        return {
            "target_index": 2,
            "target_step_id": "current",
            "target_step_name": "当前页面步骤",
            "current_probe": {"matched": False},
            "target_probe": {"matched": True},
            "future_probes": [],
        }

    runner._auto_skip_target = auto_skip
    runner.execute_step = lambda _step: None

    runner.run()

    assert runner.flow_logs[-1] == "🏁 场景执行完成: 执行 1/2 步，通过 1，失败 0，跳过 1"


def test_click_waits_for_blocking_loading_layer_to_clear(page):
    page.set_content(
        """
        <style>.loading { position:fixed; inset:0; background:rgba(0,0,0,.4); z-index:10; }</style>
        <div class="loading">加载中...</div>
        <button id="target" onclick="window.clicked=true">申请地区</button>
        <script>setTimeout(() => document.querySelector('.loading').style.display = 'none', 300)</script>
        """
    )
    element = {
        "name": "申请地区",
        "tag": "button",
        "role": "button",
        "input_type": "button",
        "locators": [{"strategy": "id", "value": "target"}],
        "fingerprint": {
            "tag": "button",
            "role": "button",
            "accessible_name": "申请地区",
            "text": "申请地区",
            "attrs": {"testid": "", "id": "target", "name": "", "placeholder": "", "type": ""},
            "parent": {"tag": "body", "text": "加载中... 申请地区"},
            "normalized_position": {"x": 0.5, "y": 0.5},
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 1200})

    started_at = time.monotonic()
    runner.execute_step({"action": "click", "element": element, "timeout": 1200})

    assert time.monotonic() - started_at >= 0.25
    assert page.evaluate("window.clicked") is True
    assert runner.last_locator_diagnostics["blocking_ui_wait"]["cleared"] is True


def test_click_waits_for_page_to_settle_after_action(page):
    page.set_content(
        """
        <button id="target" onclick="
          const loading = document.createElement('div');
          loading.className = 'van-toast--loading';
          loading.textContent = '加载中...';
          document.body.appendChild(loading);
          setTimeout(() => {
            loading.remove();
            const result = document.createElement('div');
            result.id = 'result';
            result.textContent = '页面已完成';
            document.body.appendChild(result);
          }, 300);
        ">提交</button>
        """
    )
    element = {
        "name": "提交",
        "tag": "button",
        "role": "button",
        "locators": [{"strategy": "id", "value": "target"}],
        "fingerprint": {
            "tag": "button",
            "role": "button",
            "accessible_name": "提交",
            "text": "提交",
            "attrs": {"id": "target", "type": ""},
        },
    }
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 1500})

    runner.execute_step({"action": "click", "element": element, "timeout": 1500})

    assert page.locator("#result").count() == 1
    wait = runner.last_locator_diagnostics["smart_wait_after"]
    assert wait["ready"] is True
    assert wait["saw_blocking_ui"] is True


def test_page_aware_run_waits_before_deciding_whether_step_matches(page):
    page.set_content(
        """
        <div class="van-toast--loading">加载中...</div>
        <script>
          setTimeout(() => {
            document.querySelector('.van-toast--loading').remove();
            const button = document.createElement('button');
            button.id = 'target';
            button.textContent = '继续';
            button.onclick = () => { window.clicked = true; };
            document.body.appendChild(button);
          }, 300);
        </script>
        """
    )
    element = {
        "name": "继续",
        "tag": "button",
        "role": "button",
        "locators": [{"strategy": "id", "value": "target"}],
        "fingerprint": {
            "tag": "button",
            "role": "button",
            "accessible_name": "继续",
            "text": "继续",
            "attrs": {"id": "target", "type": ""},
        },
    }
    runner = WebFlowRunner(page, {
        "flow_version": 2,
        "step_execution_mode": "page_aware",
        "timeout": 1500,
        "steps": [{"id": "continue", "action": "click", "name": "继续", "element": element}],
    })

    runner.run()

    assert page.evaluate("window.clicked") is True
    assert "跳过 0" in runner.flow_logs[-1]
    assert runner.last_locator_diagnostics["smart_wait_before"]["saw_blocking_ui"] is True


def test_click_does_not_wait_for_regular_popup_overlay(page):
    page.set_content(
        """
        <style>
          .van-overlay { position:fixed; inset:0; background:rgba(0,0,0,.4); z-index:10; }
          .van-popup { position:fixed; left:0; right:0; bottom:0; z-index:11; background:white; }
        </style>
        <div class="van-overlay"></div>
        <div class="van-popup"><button id="confirm" onclick="window.clicked=true">确认</button></div>
        """
    )
    element = {
        "name": "确认",
        "tag": "button",
        "role": "button",
        "input_type": "button",
        "locators": [{"strategy": "id", "value": "confirm"}],
        "fingerprint": {
            "tag": "button",
            "role": "button",
            "accessible_name": "确认",
            "text": "确认",
            "attrs": {"testid": "", "id": "confirm", "name": "", "placeholder": "", "type": ""},
            "parent": {"tag": "div", "text": "确认"},
            "normalized_position": {"x": 0.5, "y": 0.5},
            "minimum_score": 55,
            "minimum_gap": 8,
        },
    }
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 1000})

    started_at = time.monotonic()
    runner.execute_step({"action": "click", "element": element, "timeout": 1000})

    assert time.monotonic() - started_at < 0.5
    assert page.evaluate("window.clicked") is True
    assert "blocking_ui_wait" not in runner.last_locator_diagnostics


def test_semantic_healing_includes_recorded_list_item_tag(page):
    page.set_content("<ul><li>普通</li><li>新能源</li></ul>")
    fingerprint = {
        "tag": "li",
        "role": "listitem",
        "accessible_name": "新能源",
        "text": "新能源",
        "attrs": {"testid": "", "id": "", "name": "", "placeholder": "", "type": ""},
        "parent": {"tag": "ul", "text": "普通 新能源"},
        "normalized_position": {"x": 0.5, "y": 0.5},
        "minimum_score": 55,
        "minimum_gap": 8,
    }
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    locator, diagnostics = runner._heal_locator(page.main_frame, fingerprint)

    assert diagnostics["matched"] is True
    assert locator.inner_text() == "新能源"


def test_position_fallback_validates_and_clicks_target(page):
    page.set_content(
        "<button id='actual' style='position:fixed;left:80px;top:160px;width:120px;height:48px' "
        "onclick='window.clicked=true'>提交</button>"
    )
    box = page.locator("#actual").bounding_box()
    viewport = page.viewport_size
    position = {
        "x": (box["x"] + box["width"] / 2) / viewport["width"],
        "y": (box["y"] + box["height"] / 2) / viewport["height"],
    }
    element = {
        "tag": "button",
        "fallback_position": position,
        "fingerprint": {
            "tag": "button",
            "role": "button",
            "accessible_name": "提交",
            "text": "提交",
            "attrs": {"testid": "", "id": "", "name": "", "placeholder": "", "type": ""},
            "parent": {"tag": "body", "text": "提交"},
            "normalized_position": position,
        },
    }
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": []})

    target, diagnostics = runner._position_fallback_target(page.main_frame, element)
    target.click()

    assert diagnostics["matched"] is True
    assert page.evaluate("window.clicked") is True


def test_recorder_generates_listitem_role_and_stops_css_at_stable_id(page):
    page.set_content(
        "<div id='app'><section><ul>"
        "<li style='height:40px'></li><li style='height:40px'>新能源</li>"
        "</ul></section></div>"
    )
    box = page.locator("li").first.bounding_box()

    element = page.evaluate(
        PROBE_ELEMENT_SCRIPT,
        {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2},
    )

    assert element["role"] == "listitem"
    role_locator = next(item for item in element["locators"] if item.get("strategy") == "role")
    assert role_locator == {
        "strategy": "role",
        "role": "listitem",
        "index": 0,
        "value": "listitem[0]",
    }
    css = next(item["value"] for item in element["locators"] if item.get("strategy") == "css")
    assert css.startswith("#app ")
    assert not css.startswith("html ")

    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 500})
    target = runner.locate(element, timeout=500)
    assert target.evaluate("el => el === document.querySelector('li')") is True
    assert runner.last_locator_diagnostics["selected_strategy"] == "role"


def test_recorder_recognizes_plate_input_as_named_group(page):
    page.set_content(
        "<div class='car-number-input'>"
        "<div class='car-title'>车牌号</div>"
        "<ul class='car-input'>"
        "<li class='car-input-item' style='height:40px'></li>"
        "<li class='car-input-item' style='height:40px'></li>"
        "<p>•</p>"
        "<li class='car-input-item' style='height:40px'></li>"
        "<li class='car-input-item' style='height:40px'></li>"
        "<li class='car-input-item' style='height:40px'></li>"
        "<li class='car-input-item' style='height:40px'></li>"
        "<li class='car-input-item' style='height:40px'></li>"
        "<li class='car-input-item' style='height:40px'>新能源</li>"
        "</ul></div>"
    )
    box = page.locator("li").first.bounding_box()

    element = page.evaluate(
        PROBE_ELEMENT_SCRIPT,
        {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2},
    )

    assert element["name"] == "车牌号第1位"
    assert element["fingerprint"]["control_type"] == "plate_input"
    assert element["locators"][0] == {
        "strategy": "group_index",
        "group_text": "车牌号",
        "role": "listitem",
        "tag": "li",
        "index": 0,
        "value": "车牌号第1位",
    }

    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 500})
    target = runner.locate(element, timeout=500)
    assert target.evaluate("el => el === document.querySelector('li')") is True
    assert runner.last_locator_diagnostics["selected_strategy"] == "group_index"


def test_recorder_snaps_nearby_click_to_small_checkbox_instead_of_label_text(page):
    page.set_content(
        """
        <style>
          #agreement { margin: 40px; display: inline-flex; align-items: center; gap: 8px; }
          #native-checkbox { display: none; }
          .checkbox-icon { width: 18px; height: 18px; border: 1px solid #999; }
        </style>
        <label id="agreement">
          <input id="native-checkbox" type="checkbox">
          <span class="checkbox-icon"></span>
          <span>本人已阅读并同意签署隐私政策</span>
        </label>
        """
    )
    icon_box = page.locator(".checkbox-icon").bounding_box()
    point = {
        "x": icon_box["x"] - 8,
        "y": icon_box["y"] + icon_box["height"] / 2,
    }

    element = page.evaluate(PROBE_ELEMENT_SCRIPT, point)

    assert element["tag"] == "label"
    assert element["name"].startswith("复选框")
    assert element["fingerprint"]["control_type"] == "checkbox"
    assert not any(item.get("strategy") == "text" for item in element["locators"])

    session = BrowserRecordingSession(
        session_id="checkbox-snap-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
    )
    step = session._perform_action(page, {"action": "click", **point})

    assert page.locator("#native-checkbox").is_checked() is True
    assert step["element"]["fingerprint"]["control_type"] == "checkbox"


def test_recorder_recognizes_specific_virtual_license_plate_keyboard_key(page):
    page.set_content(
        """
        <div class="car-keyboard" role="dialog">
          <button class="car-keyboard-grids-btn"><span>京</span></button>
          <button class="car-keyboard-grids-btn">沪</button>
          <button class="car-keyboard-grids-btn">粤</button>
          <button class="car-keyboard-grids-btn">津</button>
          <button class="car-keyboard-grids-btn">冀</button>
          <button class="car-keyboard-grids-btn">豫</button>
        </div>
        """
    )
    box = page.get_by_text("京", exact=True).bounding_box()

    element = page.evaluate(
        PROBE_ELEMENT_SCRIPT,
        {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2},
    )

    assert element["name"] == "车牌键盘按键 京"
    assert element["text"] == "京"
    assert element["fingerprint"]["control_type"] == "virtual_keyboard_key"
    assert element["fingerprint"]["keyboard"] == {"key": "京", "kind": "character"}
    assert element["locators"][0] == {
        "strategy": "keyboard_text",
        "value": "京",
        "key_kind": "character",
    }


def test_recorder_virtual_keyboard_click_uses_touch_and_tracks_latest_layout(page):
    page.set_content(
        """
        <div id="plate"></div>
        <div class="car-keyboard" role="dialog">
          <div id="province">
            <button class="car-keyboard-grids-btn">京</button>
            <button class="car-keyboard-grids-btn">沪</button>
            <button class="car-keyboard-grids-btn">粤</button>
            <button class="car-keyboard-grids-btn">津</button>
            <button class="car-keyboard-grids-btn">冀</button>
            <button class="car-keyboard-grids-btn">豫</button>
          </div>
          <div id="alpha" style="display:none">
            <button class="car-keyboard-grids-btn">A</button>
            <button class="car-keyboard-grids-btn">B</button>
            <button class="car-keyboard-grids-btn">C</button>
            <button class="car-keyboard-grids-btn">1</button>
            <button class="car-keyboard-grids-btn">2</button>
            <button class="car-keyboard-grids-btn">3</button>
          </div>
        </div>
        <script>
          const plate = document.querySelector('#plate');
          document.querySelector('#province').addEventListener('touchend', event => {
            const key = event.target.closest('.car-keyboard-grids-btn');
            if (!key) return;
            plate.textContent += key.textContent.trim();
            document.querySelector('#province').style.display = 'none';
            document.querySelector('#alpha').style.display = 'block';
          });
          document.querySelector('#alpha').addEventListener('touchend', event => {
            const key = event.target.closest('.car-keyboard-grids-btn');
            if (key) plate.textContent += key.textContent.trim();
          });
        </script>
        """
    )
    session = BrowserRecordingSession(
        session_id="keyboard-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
    )
    session._record_element_step = lambda action, element, **values: {
        "action": action,
        "name": f"点击 {element['name']}",
        "element": element,
        **values,
    }

    province_box = page.get_by_text("京", exact=True).bounding_box()
    province_step = session._perform_action(page, {
        "action": "click",
        "x": province_box["x"] + province_box["width"] / 2,
        "y": province_box["y"] + province_box["height"] / 2,
    })
    alpha_box = page.get_by_text("A", exact=True).bounding_box()
    alpha_step = session._perform_action(page, {
        "action": "click",
        "x": alpha_box["x"] + alpha_box["width"] / 2,
        "y": alpha_box["y"] + alpha_box["height"] / 2,
    })

    assert page.locator("#plate").inner_text() == "京A"
    assert province_step["name"] == "点击 车牌键盘按键 京"
    assert alpha_step["name"] == "点击 车牌键盘按键 A"
    assert province_step["element"]["fingerprint"]["control_type"] == "virtual_keyboard_key"
    assert alpha_step["element"]["fingerprint"]["keyboard"]["key"] == "A"


def test_recorder_fill_mode_converts_virtual_keyboard_confirm_to_click(page):
    page.set_content(
        """
        <div id="confirmed">no</div>
        <div class="car-keyboard" role="dialog">
          <div class="keys">京 沪 粤 津 冀 豫</div>
          <div class="car-tooltips-submit">确认</div>
        </div>
        <script>
          document.querySelector('.car-tooltips-submit').addEventListener('touchend', () => {
            document.querySelector('#confirmed').textContent = 'yes';
          });
        </script>
        """
    )
    session = BrowserRecordingSession(
        session_id="keyboard-confirm-fill-mode-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
    )
    box = page.get_by_text("确认", exact=True).bounding_box()

    step = session._perform_action(page, {
        "action": "fill",
        "value": "确认",
        "x": box["x"] + box["width"] / 2,
        "y": box["y"] + box["height"] / 2,
    })

    assert page.locator("#confirmed").inner_text() == "yes"
    assert step["action"] == "click"
    assert step["name"] == "点击 车牌键盘按键 确认"
    assert step["element"]["fingerprint"]["keyboard"] == {"key": "确认", "kind": "confirm"}


def test_runner_replays_legacy_fill_steps_on_clickable_controls(page):
    page.set_content(
        """
        <div id="confirmed">no</div>
        <div class="car-keyboard" role="dialog">
          <div class="keys">京 沪 粤 津 冀 豫</div>
          <div class="car-tooltips-submit">确认</div>
        </div>
        <button id="popup-confirm">弹框确认</button>
        <script>
          document.querySelector('.car-tooltips-submit').addEventListener('touchend', () => {
            document.querySelector('#confirmed').textContent = 'keyboard';
          });
          document.querySelector('#popup-confirm').addEventListener('click', () => {
            document.querySelector('#confirmed').textContent = 'button';
          });
        </script>
        """
    )
    keyboard_box = page.get_by_text("确认", exact=True).bounding_box()
    keyboard_element = page.evaluate(PROBE_ELEMENT_SCRIPT, {
        "x": keyboard_box["x"] + keyboard_box["width"] / 2,
        "y": keyboard_box["y"] + keyboard_box["height"] / 2,
    })
    button_box = page.get_by_text("弹框确认", exact=True).bounding_box()
    button_element = page.evaluate(PROBE_ELEMENT_SCRIPT, {
        "x": button_box["x"] + button_box["width"] / 2,
        "y": button_box["y"] + button_box["height"] / 2,
    })
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 1000})

    runner.execute_step({"action": "fill", "value": "确认", "element": keyboard_element})
    assert page.locator("#confirmed").inner_text() == "keyboard"
    assert runner.last_locator_diagnostics["legacy_fill_as_click"] is True

    runner.execute_step({"action": "fill", "value": "弹框确认", "element": button_element})
    assert page.locator("#confirmed").inner_text() == "button"
    assert runner.last_locator_diagnostics["legacy_fill_as_click"] is True


def test_browser_recorder_and_runner_support_back_forward_and_reload(page):
    first_url = "data:text/html,<title>first</title><main>first</main>"
    second_url = "data:text/html,<title>second</title><main>second</main>"
    page.goto(first_url)
    page.goto(second_url)

    session = BrowserRecordingSession(
        session_id="navigation-test",
        project_id="project-test",
        start_url=first_url,
        viewport={"width": 390, "height": 844},
    )
    back_step = session._perform_action(page, {"action": "go_back"})
    assert page.locator("main").inner_text() == "first"
    forward_step = session._perform_action(page, {"action": "go_forward"})
    assert page.locator("main").inner_text() == "second"
    reload_step = session._perform_action(page, {"action": "reload"})
    assert page.locator("main").inner_text() == "second"
    assert [back_step["action"], forward_step["action"], reload_step["action"]] == [
        "go_back", "go_forward", "reload"
    ]

    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 3000})
    runner.execute_step({"action": "go_back", "name": "浏览器后退"})
    assert page.locator("main").inner_text() == "first"
    runner.execute_step({"action": "go_forward", "name": "浏览器前进"})
    assert page.locator("main").inner_text() == "second"
    runner.execute_step({"action": "reload", "name": "刷新页面"})
    assert page.locator("main").inner_text() == "second"


def test_browser_recorder_and_runner_fall_back_to_recorded_navigation_urls():
    first_url = "https://example.test/home"
    second_url = "https://example.test/result"
    page = _NoNativeHistoryPage(second_url)
    session = BrowserRecordingSession(
        session_id="navigation-fallback-test",
        project_id="project-test",
        start_url=first_url,
        viewport={"width": 390, "height": 844},
    )
    session._reset_navigation_history(first_url)
    session._track_navigation_url(second_url)

    back_step = session._perform_action(page, {"action": "go_back"})
    assert page.url == first_url
    assert back_step["to_url"] == first_url

    forward_step = session._perform_action(page, {"action": "go_forward"})
    assert page.url == second_url
    assert forward_step["to_url"] == second_url

    replay_page = _NoNativeHistoryPage(second_url)
    runner = WebFlowRunner(replay_page, {"flow_version": 2, "steps": [], "timeout": 3000})
    runner.execute_step(back_step)
    assert replay_page.url == first_url
    runner.execute_step(forward_step)
    assert replay_page.url == second_url


def test_recording_session_inserts_manual_steps_before_or_after_existing_steps():
    session = BrowserRecordingSession(
        session_id="insert-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
        steps=[
            {"id": "goto", "action": "goto", "name": "打开页面"},
            {"id": "first", "action": "click", "name": "原步骤1"},
            {"id": "second", "action": "click", "name": "原步骤2"},
        ],
    )

    session._insert_manual_step(
        {"id": "before", "action": "click", "name": "前插定位"},
        2,
    )
    session._insert_manual_step(
        {"id": "after", "action": "fill", "name": "后插定位", "value": "RunnerGo"},
        3,
    )

    assert [step["id"] for step in session.steps] == [
        "goto", "first", "before", "after", "second"
    ]
    with pytest.raises(ValueError, match="第一位"):
        session._insert_manual_step({"action": "click", "name": "非法前插"}, 0)


def test_recording_session_continuously_inserts_recorded_steps_at_selected_position():
    session = BrowserRecordingSession(
        session_id="continuous-record-insert-test",
        project_id="project-test",
        start_url="https://example.test",
        viewport={"width": 390, "height": 844},
        steps=[
            {"id": "goto", "action": "goto", "name": "打开页面"},
            {"id": "second", "action": "click", "name": "原第2步"},
            {"id": "third", "action": "click", "name": "原第3步"},
        ],
        record_insert_index=1,
    )

    session._store_recorded_step({"id": "recorded-1", "action": "scroll"})
    session._store_recorded_step({"id": "recorded-2", "action": "click"})

    assert [step["id"] for step in session.steps] == [
        "goto", "recorded-1", "recorded-2", "second", "third"
    ]
    assert session.record_insert_index == 3


def test_plate_input_uses_virtual_keyboard_and_verifies_echo(page):
    page.set_content(
        """
        <style>.car-input-item{display:inline-block;width:30px;height:40px}</style>
        <div class="car-number-input">
          <div class="car-title">车牌号</div>
          <ul class="car-input">
            <li class="car-input-item"></li><li class="car-input-item"></li><p>•</p>
            <li class="car-input-item"></li><li class="car-input-item"></li>
            <li class="car-input-item"></li><li class="car-input-item"></li>
            <li class="car-input-item"></li><li class="car-input-item">新能源</li>
          </ul>
        </div>
        <div class="car-keyboard" style="display:none">
          <button class="car-keyboard-grids-btn">京</button>
          <button class="car-keyboard-grids-btn">A</button>
          <button class="car-keyboard-grids-btn">1</button>
          <button class="car-keyboard-grids-btn">2</button>
          <button class="car-keyboard-grids-btn">3</button>
          <button class="car-keyboard-grids-btn">4</button>
          <button class="car-keyboard-grids-btn">5</button>
          <button class="car-keyboard-change"><span class="zh active">中</span>/英</button>
          <button class="car-tooltips-submit">确认</button>
        </div>
        <script>
          let cursor = 0;
          const items = [...document.querySelectorAll('.car-input-item')];
          const keyboard = document.querySelector('.car-keyboard');
          items[0].addEventListener('click', () => keyboard.style.display = 'block');
          document.querySelectorAll('.car-keyboard-grids-btn').forEach(button => {
            button.addEventListener('touchend', () => {
              items[cursor].textContent = button.textContent.trim();
              cursor += 1;
            });
          });
          document.querySelector('.car-keyboard-change').addEventListener('touchend', event => {
            event.currentTarget.querySelector('.zh').classList.remove('active');
          });
          document.querySelector('.car-tooltips-submit').addEventListener('touchend', () => {
            keyboard.style.display = 'none';
          });
        </script>
        """
    )
    box = page.locator("li").first.bounding_box()
    element = page.evaluate(
        PROBE_ELEMENT_SCRIPT,
        {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2},
    )
    runner = WebFlowRunner(page, {"flow_version": 2, "steps": [], "timeout": 1000})

    runner.execute_step({"action": "fill", "name": "输入车牌号", "value": "京A12345", "element": element})

    assert runner._read_plate_input(page.main_frame, element) == "京A12345"
    assert runner.last_locator_diagnostics["plate_input"]["verified"] is True
