"""
Inference App URL Configuration
"""
from django.urls import path
from . import views

app_name = 'inference'

urlpatterns = [
    # Documents
    path('documents/', views.document_list, name='document_list'),
    path('documents/<int:document_id>/', views.document_detail, name='document_detail'),
    path('documents/<int:document_id>/share/', views.document_share, name='document_share'),
    path('documents/<int:document_id>/download/', views.document_download, name='document_download'),
    
    # RAG
    path('rag/search/', views.rag_search, name='rag_search'),
    path('rag/query/', views.rag_query, name='rag_query'),
]
