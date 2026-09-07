from django.db import migrations


SHORT_VIDEO_COMPONENTS = [
    {
        'type': 'short_video_feed',
        'name': '短视频刷视频',
        'category': 'business',
        'description': '进入短视频流后，按随机观看时长循环播放检测、概率行为、智能滑动、异常恢复和截图验证',
        'schema': {
            'required': ['count', 'watch_time', 'direction'],
            'properties': {
                'count': {'type': 'number'},
                'watch_time': {'type': 'object'},
                'direction': {'type': 'string', 'enum': ['up', 'down']},
                'swipe_distance': {'type': 'number'},
                'random_swipe': {'type': 'boolean'},
                'random_stay': {'type': 'boolean'},
                'selector_type': {'type': 'string'},
                'selector': {'type': 'string'},
                'image_scope': {'type': 'string'},
                'image_threshold': {'type': 'number'},
                'video_region': {'type': 'array'},
                'play_timeout': {'type': 'number'},
                'sample_interval': {'type': 'number'},
                'motion_threshold': {'type': 'number'},
                'transition_timeout': {'type': 'number'},
                'transition_threshold': {'type': 'number'},
                'max_swipe_retries': {'type': 'number'},
                'like_probability': {'type': 'number'},
                'favorite_probability': {'type': 'number'},
                'comment_probability': {'type': 'number'},
                'follow_probability': {'type': 'number'},
                'action_targets': {'type': 'object'},
                'comment_texts': {'type': 'array'},
                'exception_detection': {'type': 'boolean'},
                'ocr_exception_detection': {'type': 'boolean'},
                'exception_keywords': {'type': 'array'},
                'exception_close_texts': {'type': 'array'},
                'exception_close_targets': {'type': 'object'},
                'capture_each_video': {'type': 'boolean'},
                'save_as': {'type': 'string'},
            },
        },
        'default_config': {
            'count': 100,
            'watch_time': {'min': 5, 'max': 15},
            'direction': 'up',
            'swipe_distance': 0.8,
            'random_swipe': True,
            'random_stay': True,
            'selector_type': 'image',
            'selector': '',
            'image_scope': 'common',
            'image_threshold': 0.85,
            'video_region': [0.05, 0.12, 0.95, 0.88],
            'play_timeout': 8,
            'sample_interval': 0.35,
            'motion_threshold': 0.012,
            'transition_timeout': 2.5,
            'transition_threshold': 0.045,
            'max_swipe_retries': 2,
            'like_probability': 0,
            'favorite_probability': 0,
            'comment_probability': 0,
            'follow_probability': 0,
            'action_targets': {},
            'comment_texts': [],
            'exception_detection': True,
            'ocr_exception_detection': True,
            'exception_keywords': [
                '请先登录', '登录后继续', '网络异常', '加载失败', '青少年模式', '跳过广告'
            ],
            'exception_close_texts': ['重试', '取消', '以后再说', '暂不登录', '我知道了', '关闭', '跳过'],
            'exception_close_targets': {},
            'capture_each_video': True,
            'save_as': 'short_video_feed_result',
        },
        'enabled': True,
        'sort_order': 10,
    },
    {
        'type': 'wait_video_play',
        'name': '等待视频播放',
        'category': 'business',
        'description': '通过视频区域连续帧变化判断视频已开始播放',
        'schema': {
            'required': ['timeout'],
            'properties': {
                'timeout': {'type': 'number'},
                'sample_interval': {'type': 'number'},
                'motion_threshold': {'type': 'number'},
                'required_motion_samples': {'type': 'number'},
                'video_region': {'type': 'array'},
                'exception_detection': {'type': 'boolean'},
                'ocr_exception_detection': {'type': 'boolean'},
            },
        },
        'default_config': {
            'timeout': 8,
            'sample_interval': 0.35,
            'motion_threshold': 0.012,
            'required_motion_samples': 2,
            'video_region': [0.05, 0.12, 0.95, 0.88],
            'exception_detection': True,
            'ocr_exception_detection': True,
        },
        'enabled': True,
        'sort_order': 20,
    },
    {
        'type': 'watch_video',
        'name': '观看视频',
        'category': 'business',
        'description': '在指定时长内停留并通过连续帧变化校验视频未卡住',
        'schema': {
            'required': ['duration'],
            'properties': {
                'duration': {'type': 'number'},
                'verify_playing': {'type': 'boolean'},
                'sample_interval': {'type': 'number'},
                'motion_threshold': {'type': 'number'},
                'video_region': {'type': 'array'},
            },
        },
        'default_config': {
            'duration': 10,
            'verify_playing': True,
            'sample_interval': 0.5,
            'motion_threshold': 0.012,
            'video_region': [0.05, 0.12, 0.95, 0.88],
        },
        'enabled': True,
        'sort_order': 30,
    },
    {
        'type': 'random_action',
        'name': '随机行为',
        'category': 'business',
        'description': '按概率执行点赞、收藏、评论和关注，所有目标均使用可配置的归一化坐标或定位配置',
        'schema': {
            'properties': {
                'like_probability': {'type': 'number'},
                'favorite_probability': {'type': 'number'},
                'comment_probability': {'type': 'number'},
                'follow_probability': {'type': 'number'},
                'action_targets': {'type': 'object'},
                'comment_texts': {'type': 'array'},
                'action_interval': {'type': 'number'},
                'save_as': {'type': 'string'},
            },
        },
        'default_config': {
            'like_probability': 0.1,
            'favorite_probability': 0.05,
            'comment_probability': 0.01,
            'follow_probability': 0.01,
            'action_targets': {},
            'comment_texts': [],
            'action_interval': 0.5,
            'save_as': 'random_action_result',
        },
        'enabled': True,
        'sort_order': 40,
    },
]


def add_short_video_components(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    for component in SHORT_VIDEO_COMPONENTS:
        defaults = dict(component)
        component_type = defaults.pop('type')
        AppComponent.objects.update_or_create(type=component_type, defaults=defaults)


def remove_short_video_components(apps, schema_editor):
    AppComponent = apps.get_model('app_automation', 'AppComponent')
    AppComponent.objects.filter(
        type__in=[component['type'] for component in SHORT_VIDEO_COMPONENTS]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('app_automation', '0010_remove_short_video_swipe_components'),
    ]

    operations = [
        migrations.RunPython(add_short_video_components, remove_short_video_components),
    ]
