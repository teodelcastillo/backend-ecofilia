import django_filters
from apps.document.models import Document


class TopicFilter(django_filters.CharFilter):
    """Case-insensitive containment filter for the topics ArrayField."""

    def filter(self, qs, value):
        if not value:
            return qs
        return qs.filter(topics__contains=[value.lower().strip()])


class DocumentFilter(django_filters.FilterSet):
    topic = TopicFilter()

    class Meta:
        model = Document
        fields = {
            'slug': ['exact', 'icontains'],
            'name': ['exact', 'icontains'],
            'extracted_text': ['icontains'],
            'category': ['exact', 'icontains'],
            'category_ref': ['exact'],
            'category_ref__slug': ['exact'],
            'chunking_status': ['exact'],
            'created_at': ['exact', 'year__gt', 'year__lt'],
            'is_public': ['exact'],
            'region': ['exact'],
            'year': ['exact'],
        }
