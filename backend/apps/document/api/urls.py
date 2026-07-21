from django.urls import path, include
from rest_framework.routers import DefaultRouter
from apps.document.api.views import (
    RAGQueryView,
    DocumentCreateAPIView,
    DocumentBulkCreateAPIView,
    DocumentBulkPublicAPIView,
    DocumentListAPIView,
    DocumentViewSet,
    CategoryViewSet,
    TopicsAutocompleteView,
)

router = DefaultRouter()
router.register(r'', DocumentViewSet, basename='document')

category_router = DefaultRouter()
category_router.register(r'', CategoryViewSet, basename='category')

urlpatterns = [
    path("rag/", RAGQueryView.as_view(), name="rag-query"),
    path('create/', DocumentCreateAPIView.as_view(), name='documentcreate'),
    path('create/bulk/', DocumentBulkCreateAPIView.as_view(), name='documentbulkcreate'),
    path('bulk-public/', DocumentBulkPublicAPIView.as_view(), name='documentbulkpublic'),
    path('list/', DocumentListAPIView.as_view(), name='documentlist'),
    path('topics/autocomplete/', TopicsAutocompleteView.as_view(), name='topics-autocomplete'),
    path('categories/', include(category_router.urls)),
    path('', include(router.urls)),
]
