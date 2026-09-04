from django.urls import path

from . import folder_views, views

app_name = 'inference'

urlpatterns = [
    # Folders — the per-user tree. Id-addressed throughout: `path` goes out for
    # display but is never accepted as a locator, which is what keeps traversal
    # off the table rather than guarded against. See inference/filesystem.py.
    path('folders/', folder_views.folder_list, name='folder_list'),
    path('folders/<int:folder_id>/', folder_views.folder_detail, name='folder_detail'),
    path('fs/move/', folder_views.fs_move, name='fs_move'),

    # Recycle bin — trash is a state, not a place (inference/recycle.py).
    path('trash/', folder_views.trash_list, name='trash_list'),
    path('trash/restore/', folder_views.trash_restore, name='trash_restore'),
    path('trash/empty/', folder_views.trash_empty, name='trash_empty'),

    # Documents — KB is internal (one implicit Default KB per user, no CRUD views)
    path('documents/', views.document_list, name='document_list'),
    path('documents/<int:document_id>/', views.document_detail, name='document_detail'),
    path('documents/<int:document_id>/share/', views.document_share, name='document_share'),
    path('documents/<int:document_id>/download/', views.document_download, name='document_download'),

    # RAG
    path('rag/search/', views.rag_search, name='rag_search'),
    path('rag/query/', views.rag_query, name='rag_query'),
]
