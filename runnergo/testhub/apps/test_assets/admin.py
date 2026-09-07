from django.contrib import admin

from .models import (
    ApiAsset,
    AutomationScriptAsset,
    DefectAsset,
    PageAsset,
    TestAssetLink,
    TestAssetRequirement,
)


admin.site.register(TestAssetRequirement)
admin.site.register(ApiAsset)
admin.site.register(PageAsset)
admin.site.register(AutomationScriptAsset)
admin.site.register(DefectAsset)
admin.site.register(TestAssetLink)
