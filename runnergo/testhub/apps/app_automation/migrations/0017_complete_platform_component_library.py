from django.db import migrations


TARGET = {'type': 'object'}
TARGET_LIST = {'type': 'array'}


def component(component_type, name, category, description, properties,
              defaults, required=None, sort_order=0):
    return {
        'type': component_type,
        'name': name,
        'category': category,
        'description': description,
        'schema': {
            'required': list(required or []),
            'properties': properties,
        },
        'default_config': defaults,
        'enabled': True,
        'sort_order': sort_order,
    }


SELECTOR_PROPERTIES = {
    'element_id': {'type': 'number'},
    'selector_type': {
        'type': 'string',
        'enum': ['id', 'xpath', 'text', 'ocr', 'image', 'pos', 'region'],
    },
    'selector': {'type': 'string'},
    'locator_strategies': {'type': 'array'},
    'image_scope': {'type': 'string'},
    'image_threshold': {'type': 'number'},
    'semantic_min_score': {'type': 'number'},
    'ocr_min_confidence': {'type': 'number'},
    'timeout': {'type': 'number'},
    'context_preference': {
        'type': 'string',
        'enum': ['auto', 'native', 'webview'],
    },
    'webview_context': {'type': 'string'},
    'webview_css': {'type': 'string'},
    'webview_xpath': {'type': 'string'},
    'webview_text': {'type': 'string'},
}


COMMON_RESULT_PROPERTIES = {
    'timeout': {'type': 'number'},
    'action_interval': {'type': 'number'},
    'save_as': {'type': 'string'},
    'scope': {'type': 'string', 'enum': ['local', 'global']},
}


NEW_COMPONENTS = [
    component(
        'clear_text', '清空输入', 'base_element',
        '定位 Native 或 WebView 输入框并清空已有内容',
        {
            **SELECTOR_PROPERTIES,
            'clear_count': {'type': 'number'},
            'focus_wait': {'type': 'number'},
            'save_as': {'type': 'string'},
            'scope': {'type': 'string', 'enum': ['local', 'global']},
        },
        {
            'selector_type': 'text', 'selector': '', 'locator_strategies': [],
            'image_scope': 'common', 'image_threshold': 0.85,
            'semantic_min_score': 36, 'ocr_min_confidence': 0.5,
            'timeout': 5, 'context_preference': 'auto',
            'webview_context': '', 'webview_css': '', 'webview_xpath': '',
            'webview_text': '', 'clear_count': 128, 'focus_wait': 0.2,
            'save_as': 'clear_text_result', 'scope': 'local',
        },
        required=['selector_type', 'selector'], sort_order=25,
    ),
    component(
        'verify_code', '验证码流程', 'finance',
        '点击获取验证码、等待倒计时、输入验证码并可提交',
        {
            'verification_code': {'type': 'string'},
            'code_wait': {'type': 'number'},
            'get_code_target': TARGET,
            'verification_code_target': TARGET,
            'submit_target': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'verification_code': '', 'code_wait': 0,
            'get_code_target': {}, 'verification_code_target': {},
            'submit_target': {}, 'timeout': 5, 'action_interval': 0.2,
            'save_as': 'verify_code_result', 'scope': 'local',
        },
        required=['verification_code', 'verification_code_target'], sort_order=30,
    ),
    component(
        'identity_verify', '身份认证', 'finance',
        '组合姓名、身份证、银行卡、手机号、人脸和提交步骤完成身份认证',
        {
            'full_name': {'type': 'string'}, 'full_name_target': TARGET,
            'id_card': {'type': 'string'}, 'id_card_target': TARGET,
            'bank_card': {'type': 'string'}, 'bank_card_target': TARGET,
            'phone': {'type': 'string'}, 'phone_target': TARGET,
            'fields': {'type': 'array'},
            'face_target': TARGET, 'submit_target': TARGET,
            'success_target': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'full_name': '', 'full_name_target': {}, 'id_card': '', 'id_card_target': {},
            'bank_card': '', 'bank_card_target': {}, 'phone': '', 'phone_target': {},
            'fields': [], 'face_target': {}, 'submit_target': {}, 'success_target': {},
            'timeout': 8, 'action_interval': 0.2,
            'save_as': 'identity_verify_result', 'scope': 'local',
        },
        sort_order=40,
    ),
    component(
        'risk_popup_handle', '风险弹窗处理', 'finance',
        '探测并依次处理风险提示、协议确认和二次确认弹窗',
        {
            'targets': TARGET_LIST, 'confirm_targets': TARGET_LIST,
            'probe_timeout': {'type': 'number'},
            'max_handles': {'type': 'number'},
            'required': {'type': 'boolean'},
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'targets': [], 'confirm_targets': [], 'probe_timeout': 0.8,
            'max_handles': 5, 'required': False, 'timeout': 1,
            'action_interval': 0.2, 'save_as': 'risk_popup_result',
            'scope': 'local',
        },
        sort_order=50,
    ),
    component(
        'payment_flow', '支付流程', 'finance',
        '选择支付方式并处理密码、验证码、人脸、银行跳转和最终支付确认',
        {
            'start_target': TARGET, 'method_targets': TARGET_LIST,
            'payment_password': {'type': 'string'}, 'password_target': TARGET,
            'verification_code': {'type': 'string'},
            'code_wait': {'type': 'number'}, 'get_code_target': TARGET,
            'verification_code_target': TARGET,
            'face_target': TARGET, 'bank_jump_target': TARGET,
            'confirm_payment_target': TARGET,
            'allow_real_payment': {'type': 'boolean'},
            'success_target': TARGET, 'success_page': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'start_target': {}, 'method_targets': [], 'payment_password': '',
            'password_target': {}, 'verification_code': '', 'code_wait': 0,
            'get_code_target': {}, 'verification_code_target': {},
            'face_target': {}, 'bank_jump_target': {},
            'confirm_payment_target': {}, 'allow_real_payment': False,
            'success_target': {}, 'success_page': {}, 'timeout': 10,
            'action_interval': 0.2, 'save_as': 'payment_flow_result',
            'scope': 'local',
        },
        sort_order=60,
    ),
    component(
        'upload_video', '发布视频', 'short_video',
        '推送或选择视频、执行编辑步骤、填写文案并发布',
        {
            'media_path': {'type': 'string'}, 'remote_dir': {'type': 'string'},
            'require_push': {'type': 'boolean'},
            'entry_target': TARGET, 'album_target': TARGET,
            'media_target': TARGET, 'edit_targets': TARGET_LIST,
            'caption': {'type': 'string'}, 'caption_target': TARGET,
            'publish_target': TARGET, 'success_target': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'media_path': '', 'remote_dir': '/sdcard/Download/TestHub',
            'require_push': False, 'entry_target': {}, 'album_target': {},
            'media_target': {}, 'edit_targets': [], 'caption': '',
            'caption_target': {}, 'publish_target': {}, 'success_target': {},
            'timeout': 10, 'action_interval': 0.3,
            'save_as': 'upload_video_result', 'scope': 'local',
        },
        required=['media_target', 'publish_target'], sort_order=100,
    ),
    component(
        'search_user', '搜索用户', 'social',
        '输入关键词搜索用户并可打开匹配用户',
        {
            'keyword': {'type': 'string'}, 'search_target': TARGET,
            'search_button_target': TARGET, 'result_target': TARGET,
            'user_target': TARGET, **COMMON_RESULT_PROPERTIES,
        },
        {
            'keyword': '', 'search_target': {}, 'search_button_target': {},
            'result_target': {}, 'user_target': {}, 'timeout': 8,
            'action_interval': 0.2, 'save_as': 'search_user_result',
            'scope': 'local',
        },
        required=['keyword', 'search_target'], sort_order=10,
    ),
    component(
        'send_message', '发送消息', 'social',
        '打开会话、输入文本或添加附件并发送消息',
        {
            'conversation_target': TARGET, 'message': {'type': 'string'},
            'message_input_target': TARGET, 'attachment_target': TARGET,
            'send_target': TARGET, 'success_target': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'conversation_target': {}, 'message': '',
            'message_input_target': {}, 'attachment_target': {},
            'send_target': {}, 'success_target': {}, 'timeout': 5,
            'action_interval': 0.2, 'save_as': 'send_message_result',
            'scope': 'local',
        },
        required=['send_target'], sort_order=20,
    ),
    component(
        'publish_post', '发布帖子', 'social',
        '进入发布页面、添加媒体、填写内容和选项并发布帖子',
        {
            'entry_target': TARGET, 'attachment_targets': TARGET_LIST,
            'content': {'type': 'string'}, 'content_target': TARGET,
            'option_targets': TARGET_LIST, 'publish_target': TARGET,
            'success_target': TARGET, **COMMON_RESULT_PROPERTIES,
        },
        {
            'entry_target': {}, 'attachment_targets': [], 'content': '',
            'content_target': {}, 'option_targets': [], 'publish_target': {},
            'success_target': {}, 'timeout': 8, 'action_interval': 0.2,
            'save_as': 'publish_post_result', 'scope': 'local',
        },
        required=['publish_target'], sort_order=40,
    ),
    component(
        'upload_image', '上传图片', 'social',
        '推送或选择图片并确认添加到当前内容',
        {
            'media_path': {'type': 'string'}, 'remote_dir': {'type': 'string'},
            'require_push': {'type': 'boolean'}, 'entry_target': TARGET,
            'media_target': TARGET, 'confirm_target': TARGET,
            **COMMON_RESULT_PROPERTIES,
        },
        {
            'media_path': '', 'remote_dir': '/sdcard/Download/TestHub',
            'require_push': False, 'entry_target': {}, 'media_target': {},
            'confirm_target': {}, 'timeout': 8, 'action_interval': 0.2,
            'save_as': 'upload_image_result', 'scope': 'local',
        },
        required=['media_target'], sort_order=50,
    ),
    component(
        'share_content', '分享内容', 'social',
        '打开分享面板、选择渠道或对象并确认分享',
        {
            'share_target': TARGET, 'channel_target': TARGET,
            'recipient_target': TARGET, 'confirm_target': TARGET,
            'success_target': TARGET, **COMMON_RESULT_PROPERTIES,
        },
        {
            'share_target': {}, 'channel_target': {}, 'recipient_target': {},
            'confirm_target': {}, 'success_target': {}, 'timeout': 5,
            'action_interval': 0.2, 'save_as': 'share_content_result',
            'scope': 'local',
        },
        required=['share_target', 'confirm_target'], sort_order=60,
    ),
    component(
        'comment', '评论内容', 'social',
        '打开评论区、输入评论并提交',
        {
            'comment': {'type': 'string'}, 'comment_entry_target': TARGET,
            'comment_input_target': TARGET, 'submit_target': TARGET,
            'success_target': TARGET, **COMMON_RESULT_PROPERTIES,
        },
        {
            'comment': '', 'comment_entry_target': {},
            'comment_input_target': {}, 'submit_target': {},
            'success_target': {}, 'timeout': 5, 'action_interval': 0.2,
            'save_as': 'comment_result', 'scope': 'local',
        },
        required=['comment', 'comment_input_target', 'submit_target'], sort_order=70,
    ),
]


def add_schema_properties(component, properties, defaults):
    schema = dict(component.schema or {})
    schema_properties = dict(schema.get('properties') or {})
    schema_properties.update(properties)
    schema['properties'] = schema_properties
    component.schema = schema
    default_config = dict(component.default_config or {})
    for key, value in defaults.items():
        default_config.setdefault(key, value)
    component.default_config = default_config
    component.save(update_fields=['schema', 'default_config'])


def install_components(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    for definition in NEW_COMPONENTS:
        defaults = dict(definition)
        component_type = defaults.pop('type')
        AppComponent.objects.update_or_create(type=component_type, defaults=defaults)

    # input 原先来自启动时加载的 YAML。迁移必须主动 upsert，避免全新环境在
    # migrate 完成后才加载旧 YAML，从而丢失 Native clear_first 能力。
    AppComponent.objects.update_or_create(
        type='input',
        defaults={
            'name': '输入文本',
            'category': 'base_element',
            'description': '定位 Native/WebView 输入框，支持输入前清空和回车',
            'schema': {
                'required': ['selector_type', 'selector', 'value'],
                'properties': {
                    **SELECTOR_PROPERTIES,
                    'value': {'type': 'string'},
                    'input_kind': {
                        'type': 'string',
                        'enum': [
                            'auto', 'text', 'phone', 'password',
                            'verification_code', 'money', 'bank_card',
                            'license_plate',
                        ],
                    },
                    'clear_first': {'type': 'boolean'},
                    'clear_count': {'type': 'number'},
                    'send_enter': {'type': 'boolean'},
                },
            },
            'default_config': {
                'selector_type': 'text', 'selector': '',
                'locator_strategies': [], 'image_scope': 'common',
                'image_threshold': 0.85, 'semantic_min_score': 36,
                'ocr_min_confidence': 0.5, 'timeout': 5,
                'context_preference': 'auto', 'webview_context': '',
                'webview_css': '', 'webview_xpath': '', 'webview_text': '',
                'value': '', 'input_kind': 'auto', 'clear_first': True,
                'clear_count': 128, 'send_enter': False,
            },
            'enabled': True,
            'sort_order': 20,
        },
    )

    for component_type in ('smart_click', 'smart_input', 'self_heal_click', 'self_heal_input'):
        component_obj = AppComponent.objects.filter(type=component_type).first()
        if component_obj:
            add_schema_properties(
                component_obj,
                {'locator_strategies': {'type': 'array'}},
                {'locator_strategies': []},
            )

    input_component = AppComponent.objects.filter(type='input').first()
    if input_component:
        add_schema_properties(
            input_component,
            {
                'clear_first': {'type': 'boolean'},
                'clear_count': {'type': 'number'},
            },
            {'clear_first': True, 'clear_count': 128},
        )

    login_component = AppComponent.objects.filter(type='login').first()
    if login_component:
        login_schema = dict(login_component.schema or {})
        login_properties = dict(login_schema.get('properties') or {})
        mode_property = dict(login_properties.get('mode') or {'type': 'string'})
        mode_property['enum'] = [
            'password', 'verification_code', 'biometric', 'fingerprint', 'faceid'
        ]
        login_properties['mode'] = mode_property
        login_schema['properties'] = login_properties
        login_component.schema = login_schema
        login_component.save(update_fields=['schema'])

    for component_type in ('page_detect', 'wait_page', 'assert_page'):
        component_obj = AppComponent.objects.filter(type=component_type).first()
        if component_obj:
            add_schema_properties(
                component_obj,
                {
                    'expected_page': {'type': 'string'},
                    'page_rules': {'type': 'array'},
                },
                {'expected_page': '', 'page_rules': []},
            )

    follow_component = AppComponent.objects.filter(type='follow_user').first()
    if follow_component:
        follow_component.category = 'social'
        follow_component.name = '关注用户'
        follow_component.description = '智能定位并关注指定用户，可用于短视频和社交场景'
        follow_component.sort_order = 30
        follow_component.save(update_fields=['category', 'name', 'description', 'sort_order'])


def uninstall_components(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    AppComponent.objects.filter(
        type__in=[item['type'] for item in NEW_COMPONENTS]
    ).delete()
    AppComponent.objects.filter(type='follow_user').update(
        category='short_video',
        description='智能定位并关注当前作者',
        sort_order=80,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0016_add_visual_self_healing_components'),
    ]

    operations = [
        migrations.RunPython(install_components, uninstall_components),
    ]
