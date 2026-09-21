from django.urls import path

from . import views

app_name = 'messaging'

urlpatterns = [
    # Provider callbacks. Unauthenticated by design — verification is the
    # provider signature plus the secret in the path — and every refusal is
    # the same 404.
    path('hooks/<str:channel>/<str:secret>/', views.message_hook, name='message_hook'),
]
