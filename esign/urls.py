from django.urls import path

from . import views

app_name = 'esign'

urlpatterns = [
    # The provider reporting back. Unauthenticated by design — the secret in
    # the path is the credential — and every refusal is the same 404.
    path('hooks/<str:secret>/', views.signature_hook, name='signature_hook'),
]
