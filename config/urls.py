from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from rest_framework.authtoken.views import obtain_auth_token

from loans import api

urlpatterns = [
    path('admin/', admin.site.urls),
    path('healthz', lambda r: JsonResponse({'status': 'ok'})),
    path('api/token/', obtain_auth_token),
    path('api/applicants/', api.create_applicant),
    path('api/applications/', api.applications),
    path('api/applications/<str:ref>/', api.application),
    path('api/applications/<str:ref>/documents/', api.upload_document),
    path('api/applications/<str:ref>/history/', api.history),
    path('api/applications/<str:ref>/<str:name>/', api.action),
    path('api/documents/<int:pk>/review/', api.review_document),
]
