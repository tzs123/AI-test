from rest_framework import serializers
from .models import TestSuite, TestSuiteCase


class TestSuiteCaseSerializer(serializers.ModelSerializer):
    testcase_name = serializers.CharField(source='testcase.name', read_only=True)

    class Meta:
        model = TestSuiteCase
        fields = ['id', 'testsuite', 'testcase', 'testcase_name', 'order']


class TestSuiteSerializer(serializers.ModelSerializer):
    testcases = TestSuiteCaseSerializer(source='testsuitecase_set', many=True, read_only=True)
    author_name = serializers.CharField(source='author.username', read_only=True)

    class Meta:
        model = TestSuite
        fields = ['id', 'project', 'name', 'description', 'testcases', 'author', 'author_name', 'created_at', 'updated_at']
        read_only_fields = ['author']

    def create(self, validated_data):
        validated_data['author'] = self.context['request'].user
        return super().create(validated_data)
