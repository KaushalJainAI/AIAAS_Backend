from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import MCPServerViewSet

router = DefaultRouter()
router.register(r'servers', MCPServerViewSet)

urlpatterns = [
    path('', include(router.urls)),
]
