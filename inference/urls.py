from django.urls import path

from . import dashboard_views, folder_views, page_views, views

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
    path('documents/search/', views.document_search, name='document_search'),
    path('documents/', views.document_list, name='document_list'),
    path('documents/new/', views.document_new, name='document_new'),
    path('documents/<int:document_id>/', views.document_detail, name='document_detail'),
    path('documents/<int:document_id>/share/', views.document_share, name='document_share'),
    path('documents/<int:document_id>/download/', views.document_download, name='document_download'),
    path('documents/<int:document_id>/content/', views.document_content, name='document_content'),
    path('documents/<int:document_id>/office/', views.document_office, name='document_office'),
    path('documents/<int:document_id>/copy/', views.document_copy, name='document_copy'),
    path('documents/<int:document_id>/import/', views.document_import, name='document_import'),
    path('documents/<int:document_id>/asset/', views.document_asset, name='document_asset'),
    path('documents/<int:document_id>/images/', views.document_images, name='document_images'),
    path('documents/<int:document_id>/preview-image/', views.document_preview_image,
         name='document_preview_image'),
    path('documents/<int:document_id>/archive/', views.document_archive, name='document_archive'),
    path('documents/<int:document_id>/draft/', views.document_draft, name='document_draft'),
    path('documents/<int:document_id>/export/', views.document_export, name='document_export'),
    path('documents/<int:document_id>/versions/', views.document_versions, name='document_versions'),
    path('documents/<int:document_id>/versions/<int:version_id>/download/',
         views.document_version_download, name='document_version_download'),
    path('documents/<int:document_id>/versions/<int:version_id>/restore/',
         views.document_version_restore, name='document_version_restore'),

    # Dashboards — live tiles bound to sources (see dashboard_views.py).
    path('dashboards/', dashboard_views.dashboard_list, name='dashboard_list'),
    path('dashboards/<int:dashboard_id>/', dashboard_views.dashboard_detail, name='dashboard_detail'),
    path('dashboards/<int:dashboard_id>/refresh/', dashboard_views.dashboard_refresh,
         name='dashboard_refresh'),

    # RAG
    path('rag/search/', views.rag_search, name='rag_search'),
    path('rag/query/', views.rag_query, name='rag_query'),

    # Hosted pages — snapshots of outputs shareable by link.
    path('pages/', page_views.page_list, name='page_list'),
    path('pages/<slug:slug>/', page_views.page_detail, name='page_detail'),
    path('pages/<slug:slug>/download/', page_views.page_download,
         name='page_download'),

    # The public pair: the third unauthenticated surface in the product.
    # Every refusal is the same 404; see page_views for why.
    path('public/pages/', page_views.public_page_list, name='public_page_list'),
    path('public/pages/<slug:slug>/', page_views.public_page_detail,
         name='public_page_detail'),
    path('public/pages/<slug:slug>/download/', page_views.public_page_download,
         name='public_page_download'),
]
