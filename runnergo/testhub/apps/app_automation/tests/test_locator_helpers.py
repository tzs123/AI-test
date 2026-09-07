# -*- coding: utf-8 -*-
from apps.app_automation.utils.locator_helpers import (
    airtest_record_position,
    fingerprint_at_point,
    fingerprint_is_replay_safe,
    normalize_locator_strategies,
    normalize_point,
    normalize_region,
    parse_ui_hierarchy,
    scale_point,
    scale_region,
    select_best_ui_node,
    strategy_fingerprint,
    xpath_to_fingerprint,
)


SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.demo" content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,1920]">
    <node index="0" text="登录" resource-id="com.demo:id/login" class="android.widget.Button" package="com.demo" content-desc="登录按钮" clickable="true" enabled="true" bounds="[240,1400][840,1540]" />
    <node index="1" text="取消" resource-id="com.demo:id/cancel" class="android.widget.Button" package="com.demo" content-desc="" clickable="true" enabled="true" bounds="[240,1580][840,1720]" />
  </node>
</hierarchy>"""

IOS_SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<AppiumAUT>
  <XCUIElementTypeApplication type="XCUIElementTypeApplication" name="示例App" enabled="true" x="0" y="0" width="390" height="844">
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="login_button" label="登录" enabled="true" x="80" y="620" width="230" height="48" />
    <XCUIElementTypeTextField type="XCUIElementTypeTextField" name="phone_input" value="13800138000" placeholderValue="请输入手机号" enabled="true" hasKeyboardFocus="true" x="32" y="220" width="326" height="44" />
    <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" name="agreement_switch" value="0" traits="ToggleButton, Button" enabled="true" visible="true" selected="false" x="32" y="672" width="337" height="43" />
  </XCUIElementTypeApplication>
</AppiumAUT>"""


def test_coordinate_normalization_and_scaling():
    assert normalize_point(540, 960, (1080, 1920)) == {"x": 0.5, "y": 0.5}
    assert normalize_region((108, 192, 972, 1728), (1080, 1920)) == {
        "x1": 0.1, "y1": 0.1, "x2": 0.9, "y2": 0.9,
    }
    assert scale_point(
        {"x": 540, "y": 960, "source_resolution": {"width": 1080, "height": 1920}},
        (720, 1280),
    ) == (360, 640)
    assert scale_point(
        {"normalized_position": {"x": 0.25, "y": 0.75}},
        (720, 1280),
    ) == (180, 960)
    assert scale_region(
        {"normalized_region": {"x1": 0.1, "y1": 0.2, "x2": 0.9, "y2": 0.8}},
        (720, 1280),
    ) == (72, 256, 648, 1024)


def test_ui_fingerprint_can_relocate_after_resolution_change():
    original_nodes = parse_ui_hierarchy(SAMPLE_XML, (1080, 1920))
    fingerprint = fingerprint_at_point(original_nodes, 540, 1470)
    assert fingerprint["resource_id"] == "com.demo:id/login"

    resized_xml = SAMPLE_XML.replace("1080,1920", "720,1280").replace(
        "[240,1400][840,1540]", "[160,933][560,1027]"
    ).replace("[240,1580][840,1720]", "[160,1053][560,1147]")
    current_nodes = parse_ui_hierarchy(resized_xml, (720, 1280))
    matched, diagnostics = select_best_ui_node(fingerprint, current_nodes)

    assert diagnostics["matched"] is True
    assert matched["resource_id"] == "com.demo:id/login"
    assert matched["center"] == {"x": 360, "y": 980}


def test_airtest_record_position_uses_screen_width():
    assert airtest_record_position((440, 860, 640, 1060), (1080, 1920)) == (0.0, 0.0)


def test_semantic_locator_does_not_accept_position_only_match():
    nodes = parse_ui_hierarchy(SAMPLE_XML, (1080, 1920))
    fingerprint = {
        "version": 1,
        "text": "不存在的按钮",
        "class_name": "android.widget.Button",
        "normalized_position": {"x": 0.5, "y": 0.765625},
    }
    matched, diagnostics = select_best_ui_node(fingerprint, nodes)
    assert matched is None
    assert diagnostics["matched"] is False


def test_semantic_locator_skips_invisible_exact_match():
    fingerprint = {
        'version': 1,
        'resource_id': '请填写包含门牌号的地址',
        'text': '请填写包含门牌号的地址',
        'class_name': 'XCUIElementTypeStaticText',
    }
    nodes = [{
        'resource_id': '请填写包含门牌号的地址',
        'text': '请填写包含门牌号的地址',
        'content_desc': '请填写包含门牌号的地址',
        'class_name': 'XCUIElementTypeStaticText',
        'enabled': True,
        'visible': False,
        'bounds': {'x1': 522, 'y1': -171, 'x2': 1071, 'y2': -96},
    }]

    matched, diagnostics = select_best_ui_node(fingerprint, nodes)

    assert matched is None
    assert diagnostics['matched'] is False


def test_full_page_webview_fingerprint_uses_coordinate_fallback():
    fingerprint = {
        "text": "今东车融",
        "class_name": "android.webkit.WebView",
        "clickable": False,
        "normalized_bounds": {"x1": 0, "y1": 0.1, "x2": 1, "y2": 0.85},
    }

    assert fingerprint_is_replay_safe(fingerprint) is False


def test_ios_named_webview_fingerprint_still_uses_coordinate_fallback():
    fingerprint = {
        'resource_id': '今东车融',
        'text': '今东车融',
        'class_name': 'XCUIElementTypeWebView',
        'clickable': False,
        'normalized_bounds': {'x1': 0, 'y1': 0.08, 'x2': 1, 'y2': 0.96},
    }

    assert fingerprint_is_replay_safe(fingerprint) is False


def test_small_clickable_fingerprint_is_replay_safe():
    fingerprint = {
        "content_desc": "Chrome",
        "class_name": "android.widget.TextView",
        "clickable": True,
        "normalized_bounds": {"x1": 0.53, "y1": 0.78, "x2": 0.69, "y2": 0.86},
    }

    assert fingerprint_is_replay_safe(fingerprint) is True


def test_ui_hierarchy_preserves_focused_state_for_input_capture():
    xml = SAMPLE_XML.replace(
        'resource-id="com.demo:id/login"',
        'resource-id="com.demo:id/login" focused="true"',
    )

    nodes = parse_ui_hierarchy(xml, (1080, 1920))

    login = next(node for node in nodes if node.get('resource_id') == 'com.demo:id/login')
    assert login['focused'] is True


def test_ios_wda_hierarchy_is_normalized_for_semantic_locators():
    nodes = parse_ui_hierarchy(IOS_SAMPLE_XML, (390, 844))

    login = next(node for node in nodes if node.get('resource_id') == 'login_button')
    phone = next(node for node in nodes if node.get('resource_id') == 'phone_input')

    assert login['text'] == '登录'
    assert login['class_name'] == 'XCUIElementTypeButton'
    assert login['clickable'] is True
    assert login['center'] == {'x': 195, 'y': 644}
    assert phone['focused'] is True
    assert phone['value'] == '13800138000'
    assert phone['placeholder'] == '请输入手机号'
    assert phone['bounds'] == {'x1': 32, 'y1': 220, 'x2': 358, 'y2': 264}


def test_ios_wda_hierarchy_scales_logical_points_to_screenshot_pixels():
    nodes = parse_ui_hierarchy(IOS_SAMPLE_XML, (1170, 2532))

    phone = next(node for node in nodes if node.get('resource_id') == 'phone_input')

    assert phone['bounds'] == {'x1': 96, 'y1': 660, 'x2': 1074, 'y2': 792}
    assert phone['center'] == {'x': 585, 'y': 726}


def test_ios_wda_hierarchy_preserves_checkbox_state_attributes():
    nodes = parse_ui_hierarchy(IOS_SAMPLE_XML, (390, 844))

    checkbox = next(
        node for node in nodes if node.get('resource_id') == 'agreement_switch'
    )

    assert checkbox['class_name'] == 'XCUIElementTypeSwitch'
    assert checkbox['value'] == '0'
    assert checkbox['traits'] == 'ToggleButton, Button'
    assert checkbox['selected'] is False
    assert checkbox['visible'] is True
    assert checkbox['clickable'] is True


def test_old_image_element_is_upgraded_to_semantic_image_coordinate_chain():
    strategies = normalize_locator_strategies({
        'image_path': 'common/login.png',
        'fingerprint': {
            'resource_id': 'com.demo:id/login',
            'content_desc': '登录按钮',
            'text': '登录',
        },
        'fallback_position': {'x': 0.5, 'y': 0.75},
    }, 'image')

    assert [item['type'] for item in strategies] == [
        'resource_id', 'accessibility', 'text', 'ocr', 'image', 'position'
    ]


def test_explicit_locator_strategy_order_and_disabled_state_are_preserved():
    strategies = normalize_locator_strategies({
        'resource_id': 'com.demo:id/login',
        'text': '登录',
        'locator_strategies': [
            {'type': 'text', 'enabled': True},
            {'type': 'resource-id', 'enabled': False},
        ],
    }, 'appium')

    assert [item['type'] for item in strategies] == ['text', 'resource_id']
    assert strategies[1]['enabled'] is False
    assert strategies[0]['value'] == '登录'


def test_locator_strategy_stability_scores_and_recommendation_are_normalized():
    strategies = normalize_locator_strategies({
        'resource_id': 'com.demo:id/login',
        'accessibility_id': '登录按钮',
        'xpath': "//*[@text='登录']",
        'webview_xpath': "//button[normalize-space()='登录']",
    }, 'appium')

    by_type = {item['type']: item for item in strategies}
    assert by_type['resource_id']['stability_score'] == 95
    assert by_type['accessibility']['stability_score'] == 90
    assert by_type['xpath']['stability_score'] == 40
    assert by_type['webview_xpath']['value'] == "//button[normalize-space()='登录']"
    assert by_type['resource_id']['recommended'] is True
    assert by_type['accessibility']['recommended'] is False


def test_single_semantic_strategy_clears_other_identity_fields():
    fingerprint = strategy_fingerprint({
        'resource_id': 'com.demo:id/login',
        'text': '登录',
        'fingerprint': {
            'resource_id': 'com.demo:id/login',
            'content_desc': '登录按钮',
            'text': '登录',
            'class_name': 'android.widget.Button',
        },
    }, 'text')

    assert fingerprint['text'] == '登录'
    assert 'resource_id' not in fingerprint
    assert 'content_desc' not in fingerprint
    assert fingerprint['class_name'] == 'android.widget.Button'


def test_common_appium_xpath_can_be_converted_to_fingerprint():
    fingerprint = xpath_to_fingerprint(
        "//android.widget.Button[@resource-id='com.demo:id/login' and @text=\"登录\"]"
    )

    assert fingerprint == {
        'class_name': 'android.widget.Button',
        'resource_id': 'com.demo:id/login',
        'text': '登录',
    }
