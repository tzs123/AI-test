from django.test import SimpleTestCase

from apps.app_automation.utils.popup_selector import (
    PopupSelectionError,
    find_popup_text_node,
    popup_column_swipe,
    popup_node_tap_point,
    select_popup_text_path,
    split_popup_selection_path,
)


def popup_node(text, x1, y1, x2, y2, depth=5):
    return {
        'text': text,
        'content_desc': '',
        'class_name': 'android.view.View',
        'visible': True,
        'enabled': True,
        'depth': depth,
        'bounds': {'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2},
    }


class PopupSelectorTests(SimpleTestCase):
    def test_split_popup_selection_path_supports_common_separators(self):
        self.assertEqual(
            split_popup_selection_path('浙江省／杭州市 → 拱墅区'),
            ['浙江省', '杭州市', '拱墅区'],
        )

    def test_find_popup_text_node_stays_in_requested_column(self):
        nodes = [
            popup_node('北京市', 50, 700, 250, 760),
            popup_node('北京市', 720, 700, 970, 760),
        ]

        selected = find_popup_text_node(
            nodes,
            '北京市',
            (1080, 1920),
            column_index=0,
            column_count=3,
        )

        self.assertEqual(selected['bounds']['x1'], 50)

    def test_find_popup_text_node_matches_admin_division_shorthand_in_exact_mode(self):
        nodes = [
            popup_node('浙江省', 50, 700, 250, 760),
            popup_node('杭州市', 400, 700, 650, 760),
            popup_node('拱墅区', 720, 700, 970, 760),
        ]

        province = find_popup_text_node(
            nodes,
            '浙江',
            (1080, 1920),
            column_index=0,
            column_count=3,
            match_mode='exact',
        )
        city = find_popup_text_node(
            nodes,
            '杭州',
            (1080, 1920),
            column_index=1,
            column_count=3,
            match_mode='exact',
        )
        district = find_popup_text_node(
            nodes,
            '拱墅',
            (1080, 1920),
            column_index=2,
            column_count=3,
            match_mode='exact',
        )

        self.assertEqual(province['text'], '浙江省')
        self.assertEqual(city['text'], '杭州市')
        self.assertEqual(district['text'], '拱墅区')

    def test_popup_node_tap_point_uses_ios_picker_row_pitch(self):
        nodes = [
            popup_node('北京市', 0, 622, 134, 763),
            popup_node('天津市', 0, 666, 134, 807),
            popup_node('河北省', 0, 710, 134, 851),
        ]

        target = popup_node_tap_point(
            nodes[0],
            nodes,
            (402, 874),
            column_index=0,
            column_count=3,
        )

        self.assertEqual(target, (67, 644))

    def test_popup_node_tap_point_keeps_normal_non_overlapping_center(self):
        nodes = [
            popup_node('北京市', 0, 620, 134, 660),
            popup_node('天津市', 0, 680, 134, 720),
        ]

        target = popup_node_tap_point(
            nodes[0],
            nodes,
            (402, 874),
            column_index=0,
            column_count=3,
        )

        self.assertEqual(target, (67, 640))

    def test_popup_node_tap_point_uses_bottom_anchor_while_ios_wheel_moves(self):
        nodes = [
            popup_node('黑龙江省', 0, 393, 134, 622),
            popup_node('上海市', 0, 437, 134, 666),
            popup_node('江苏省', 0, 481, 134, 710),
            popup_node('浙江省', 0, 525, 134, 754),
        ]

        target = popup_node_tap_point(
            nodes[-1],
            nodes,
            (402, 874),
            column_index=0,
            column_count=3,
        )

        self.assertEqual(target, (67, 732))

    def test_popup_node_tap_point_rejects_row_hidden_by_bottom_toolbar(self):
        nodes = [
            popup_node('上城区', 268, 622, 402, 763),
            popup_node('下城区', 268, 666, 402, 807),
            popup_node('江干区', 268, 710, 402, 851),
            popup_node('拱墅区', 268, 754, 402, 895),
        ]

        target = popup_node_tap_point(
            nodes[-1],
            nodes,
            (402, 874),
            column_index=2,
            column_count=3,
        )

        self.assertIsNone(target)

    def test_popup_column_swipe_is_short_enough_for_picker_wheel(self):
        start, end = popup_column_swipe(
            (402, 874),
            column_index=0,
            column_count=3,
            direction='up',
        )

        self.assertLessEqual(abs(start[1] - end[1]), 48)

    def test_popup_column_swipe_preserves_logical_distance_on_retina_ios(self):
        logical_start, logical_end = popup_column_swipe(
            (402, 874),
            column_index=0,
            column_count=3,
            direction='up',
        )
        retina_start, retina_end = popup_column_swipe(
            (1206, 2622),
            column_index=0,
            column_count=3,
            direction='up',
        )

        logical_distance = abs(logical_start[1] - logical_end[1])
        retina_distance = abs(retina_start[1] - retina_end[1])
        self.assertAlmostEqual(retina_distance / 3, logical_distance, delta=1)
        self.assertGreater(retina_distance, 48)

    def test_select_popup_text_path_taps_each_dependent_column(self):
        states = [
            [popup_node('浙江省', 40, 1200, 320, 1280)],
            [
                popup_node('浙江省', 40, 1200, 320, 1280),
                popup_node('杭州市', 400, 1200, 680, 1280),
            ],
            [
                popup_node('浙江省', 40, 1200, 320, 1280),
                popup_node('杭州市', 400, 1200, 680, 1280),
                popup_node('拱墅区', 760, 1200, 1040, 1280),
            ],
        ]
        selected_count = {'value': 0}
        taps = []

        def get_nodes():
            return states[min(selected_count['value'], len(states) - 1)]

        def tap(x, y):
            taps.append((x, y))
            selected_count['value'] += 1

        result = select_popup_text_path(
            '浙江省/杭州市/拱墅区',
            resolution=(1080, 1920),
            get_nodes=get_nodes,
            tap=tap,
            swipe=lambda *_args: None,
            pause=lambda _seconds: None,
            max_swipes=0,
        )

        self.assertEqual(len(taps), 3)
        self.assertEqual(result['parts'], ['浙江省', '杭州市', '拱墅区'])
        self.assertTrue(result['matched'])

    def test_select_popup_text_path_scrolls_until_hidden_option_appears(self):
        state = {'swiped': False}
        swipes = []
        taps = []

        def get_nodes():
            return (
                [popup_node('黑龙江省', 360, 1100, 720, 1180)]
                if state['swiped']
                else [popup_node('北京市', 360, 1100, 720, 1180)]
            )

        def swipe(x1, y1, x2, y2, duration):
            swipes.append((x1, y1, x2, y2, duration))
            state['swiped'] = True

        result = select_popup_text_path(
            '黑龙江省',
            resolution=(1080, 1920),
            get_nodes=get_nodes,
            tap=lambda x, y: taps.append((x, y)),
            swipe=swipe,
            pause=lambda _seconds: None,
            max_swipes=1,
        )

        self.assertEqual(len(swipes), 1)
        self.assertEqual(len(taps), 1)
        self.assertEqual(result['selected'][0]['swipe_attempts'], 1)

    def test_select_popup_text_path_reports_missing_option(self):
        with self.assertRaisesRegex(PopupSelectionError, '未暴露可识别文本'):
            select_popup_text_path(
                '不存在',
                resolution=(1080, 1920),
                get_nodes=lambda: [],
                tap=lambda *_args: None,
                swipe=lambda *_args: None,
                pause=lambda _seconds: None,
                max_swipes=0,
            )
