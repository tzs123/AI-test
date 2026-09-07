# -*- coding: utf-8 -*-
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.app_automation.models import AppElement, AppElementFolder, AppProject, AppTestCase


User = get_user_model()


class AppElementFolderApiTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='folder_tester', password='secret')
        self.client.force_authenticate(self.user)
        self.project = AppProject.objects.create(name='文件夹测试项目', owner=self.user)
        self.folder = AppElementFolder.objects.create(name='iPhone 17 Pro', created_by=self.user)
        self.grouped = AppElement.objects.create(
            name='文件夹内元素',
            project=self.project,
            folder=self.folder,
            element_type='pos',
            config={'x': 10, 'y': 20},
            created_by=self.user,
        )
        self.unassigned = AppElement.objects.create(
            name='未分组元素',
            project=self.project,
            element_type='pos',
            config={'x': 30, 'y': 40},
            created_by=self.user,
        )

    def test_folder_list_contains_active_element_count(self):
        response = self.client.get('/api/app-automation/element-folders/')

        self.assertEqual(response.status_code, 200)
        item = next(row for row in response.data if row['id'] == self.folder.id)
        self.assertEqual(item['element_count'], 1)

    def test_elements_can_be_filtered_by_folder_and_unassigned(self):
        grouped_response = self.client.get(
            '/api/app-automation/elements/',
            {'folder': self.folder.id},
        )
        unassigned_response = self.client.get(
            '/api/app-automation/elements/',
            {'folder': 'unassigned'},
        )

        self.assertEqual(grouped_response.status_code, 200)
        self.assertEqual([row['id'] for row in grouped_response.data['results']], [self.grouped.id])
        self.assertEqual(unassigned_response.status_code, 200)
        self.assertEqual([row['id'] for row in unassigned_response.data['results']], [self.unassigned.id])

    def test_elements_can_be_searched_by_name_and_tag_on_sqlite(self):
        self.grouped.tags = ['联系人2', '确认页']
        self.grouped.save(update_fields=['tags'])

        name_response = self.client.get(
            '/api/app-automation/elements/',
            {'search': '文件夹内'},
        )
        tag_response = self.client.get(
            '/api/app-automation/elements/',
            {'search': '联系人2'},
        )

        self.assertEqual(name_response.status_code, 200)
        self.assertEqual(tag_response.status_code, 200)
        self.assertIn(self.grouped.id, [row['id'] for row in name_response.data['results']])
        self.assertIn(self.grouped.id, [row['id'] for row in tag_response.data['results']])

    def test_bulk_move_and_folder_delete_keep_elements(self):
        move_response = self.client.post(
            '/api/app-automation/elements/bulk-move/',
            {'element_ids': [self.unassigned.id], 'folder': self.folder.id},
            format='json',
        )

        self.assertEqual(move_response.status_code, 200)
        self.assertEqual(move_response.data['moved_count'], 1)
        self.unassigned.refresh_from_db()
        self.assertEqual(self.unassigned.folder_id, self.folder.id)

        delete_response = self.client.delete(
            f'/api/app-automation/element-folders/{self.folder.id}/'
        )

        self.assertEqual(delete_response.status_code, 204)
        self.grouped.refresh_from_db()
        self.unassigned.refresh_from_db()
        self.assertIsNone(self.grouped.folder_id)
        self.assertIsNone(self.unassigned.folder_id)

    def test_same_name_is_allowed_in_different_folders(self):
        android_folder = AppElementFolder.objects.create(name='Android 模拟器', created_by=self.user)

        first = self.client.post(
            '/api/app-automation/elements/',
            {
                'name': '登录按钮',
                'folder': self.folder.id,
                'element_type': 'pos',
                'config': {'x': 100, 'y': 200},
                'tags': [],
            },
            format='json',
        )
        second = self.client.post(
            '/api/app-automation/elements/',
            {
                'name': '登录按钮',
                'folder': android_folder.id,
                'element_type': 'pos',
                'config': {'x': 300, 'y': 400},
                'tags': [],
            },
            format='json',
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.data['id'], second.data['id'])

    def test_same_name_is_rejected_within_same_folder_and_unassigned(self):
        payload = {
            'name': '重复元素',
            'folder': self.folder.id,
            'element_type': 'pos',
            'config': {'x': 1, 'y': 2},
            'tags': [],
        }
        first = self.client.post('/api/app-automation/elements/', payload, format='json')
        second = self.client.post('/api/app-automation/elements/', payload, format='json')

        unassigned_payload = {**payload, 'name': '未分组重复元素', 'folder': None}
        first_unassigned = self.client.post(
            '/api/app-automation/elements/', unassigned_payload, format='json'
        )
        second_unassigned = self.client.post(
            '/api/app-automation/elements/', unassigned_payload, format='json'
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 400)
        self.assertIn('name', second.data)
        self.assertEqual(first_unassigned.status_code, 201)
        self.assertEqual(second_unassigned.status_code, 400)
        self.assertIn('name', second_unassigned.data)

    def test_bulk_move_requires_confirmation_for_name_conflict(self):
        target_element = AppElement.objects.create(
            name='同名元素',
            folder=self.folder,
            element_type='pos',
            config={'x': 1, 'y': 2},
            created_by=self.user,
        )
        source_element = AppElement.objects.create(
            name='同名元素',
            folder=None,
            element_type='pos',
            config={'x': 3, 'y': 4},
            created_by=self.user,
        )

        response = self.client.post(
            '/api/app-automation/elements/bulk-move/',
            {'element_ids': [source_element.id], 'folder': self.folder.id},
            format='json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn('同名元素', response.data['conflicting_names'])
        self.assertTrue(response.data['requires_overwrite'])
        source_element.refresh_from_db()
        self.assertIsNone(source_element.folder_id)
        self.assertTrue(AppElement.objects.filter(id=target_element.id).exists())

    def test_bulk_move_overwrites_same_name_and_adds_different_name(self):
        target_element = AppElement.objects.create(
            name='同名元素',
            folder=self.folder,
            element_type='pos',
            config={'x': 1, 'y': 2},
            created_by=self.user,
        )
        source_conflict = AppElement.objects.create(
            name='同名元素',
            folder=None,
            element_type='pos',
            config={'x': 3, 'y': 4},
            created_by=self.user,
        )
        source_new = AppElement.objects.create(
            name='新增元素',
            folder=None,
            element_type='pos',
            config={'x': 5, 'y': 6},
            created_by=self.user,
        )

        response = self.client.post(
            '/api/app-automation/elements/bulk-move/',
            {
                'element_ids': [source_conflict.id, source_new.id],
                'folder': self.folder.id,
                'overwrite': True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['moved_count'], 2)
        self.assertEqual(response.data['overwritten_count'], 1)
        self.assertFalse(AppElement.objects.filter(id=target_element.id).exists())
        source_conflict.refresh_from_db()
        source_new.refresh_from_db()
        self.assertEqual(source_conflict.folder_id, self.folder.id)
        self.assertEqual(source_new.folder_id, self.folder.id)
        self.assertEqual(
            AppElement.objects.get(folder=self.folder, name='同名元素').config,
            {'x': 3, 'y': 4},
        )

    def test_element_delete_is_blocked_while_test_case_references_it(self):
        test_case = AppTestCase.objects.create(
            name='引用元素的用例',
            project=self.project,
            ui_flow=[{
                'type': 'smart_click',
                'config': {'element_id': self.grouped.id},
            }],
            created_by=self.user,
        )

        response = self.client.delete(
            f'/api/app-automation/elements/{self.grouped.id}/'
        )

        self.assertEqual(response.status_code, 409)
        self.assertTrue(AppElement.objects.filter(id=self.grouped.id).exists())
        self.assertEqual(response.data['conflicts'][0]['element_id'], self.grouped.id)
        self.assertEqual(response.data['conflicts'][0]['test_cases'][0]['id'], test_case.id)

    def test_bulk_overwrite_is_blocked_when_target_is_referenced(self):
        target_element = AppElement.objects.create(
            name='被引用的同名元素',
            folder=self.folder,
            element_type='pos',
            config={'x': 1, 'y': 2},
            created_by=self.user,
        )
        source_element = AppElement.objects.create(
            name='被引用的同名元素',
            folder=None,
            element_type='pos',
            config={'x': 3, 'y': 4},
            created_by=self.user,
        )
        AppTestCase.objects.create(
            name='覆盖保护用例',
            project=self.project,
            ui_flow=[{
                'type': 'smart_click',
                'config': {'element_id': target_element.id},
            }],
            created_by=self.user,
        )

        response = self.client.post(
            '/api/app-automation/elements/bulk-move/',
            {
                'element_ids': [source_element.id],
                'folder': self.folder.id,
                'overwrite': True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 409)
        self.assertTrue(AppElement.objects.filter(id=target_element.id).exists())
        source_element.refresh_from_db()
        self.assertIsNone(source_element.folder_id)

    def test_test_case_api_rejects_missing_element_reference(self):
        response = self.client.post(
            '/api/app-automation/test-cases/',
            {
                'name': '悬空元素用例',
                'project': self.project.id,
                'ui_flow': [{
                    'type': 'smart_click',
                    'config': {'element_id': 999999},
                }],
                'variables': [],
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('999999', str(response.data['ui_flow'][0]))

    def test_folder_delete_rejects_conflicts_with_unassigned(self):
        AppElement.objects.create(
            name='删除冲突元素',
            folder=self.folder,
            element_type='pos',
            config={'x': 1, 'y': 2},
            created_by=self.user,
        )
        AppElement.objects.create(
            name='删除冲突元素',
            folder=None,
            element_type='pos',
            config={'x': 3, 'y': 4},
            created_by=self.user,
        )

        response = self.client.delete(
            f'/api/app-automation/element-folders/{self.folder.id}/'
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('删除冲突元素', response.data['conflicting_names'])
        self.assertTrue(AppElementFolder.objects.filter(id=self.folder.id).exists())

    def test_same_image_hash_is_isolated_by_folder(self):
        android_folder = AppElementFolder.objects.create(name='图片隔离文件夹', created_by=self.user)
        base_payload = {
            'name': '图片元素',
            'element_type': 'image',
            'config': {
                'image_path': 'common/shared.png',
                'file_hash': 'same-image-hash',
            },
            'tags': [],
        }

        first = self.client.post(
            '/api/app-automation/elements/',
            {**base_payload, 'folder': self.folder.id},
            format='json',
        )
        second = self.client.post(
            '/api/app-automation/elements/',
            {**base_payload, 'folder': android_folder.id},
            format='json',
        )
        duplicate = self.client.post(
            '/api/app-automation/elements/',
            {**base_payload, 'name': '同文件夹另一图片元素', 'folder': self.folder.id},
            format='json',
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn('config', duplicate.data)
