from rest_framework.views import APIView
from rest_framework.generics import CreateAPIView, ListAPIView
from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework import viewsets, mixins
from django.shortcuts import get_object_or_404

from collections import defaultdict
from datetime import timedelta
import os

from django.db import transaction
from django.db.models import Q, Count, Case, When, Value, CharField, F, Func, TextField
from django.utils import timezone
from rest_framework.pagination import PageNumberPagination

from apps.document.models import (
    SmartChunk,
    Document,
    DocumentShare,
    Category,
    ChunkingStatus,
)
from apps.document.tasks import process_document_chunks
from apps.document.category_utils import category_descendant_ids
from apps.user.models import UserRole
from apps.document.api.filters import DocumentFilter
from apps.document.api.serializers import (
    SmartChunkSerializer,
    DocumentChunkSerializer,
    DocumentSerializer,
    DocumentCreateSerializer,
    DocumentBulkCreateSerializer,
    DocumentBulkPublicSerializer,
    DocumentDetailSerializer,
    DocumentUpdateSerializer,
    DocumentShareRoleUpdateSerializer,
    DocumentShareSerializer,
    DocumentShareWriteSerializer,
    CategorySerializer,
    CategoryWriteSerializer,
)
from apps.chat.models import ChatSession, DEFAULT_CHAT_MODEL
from apps.chat.api.serializers import ChatSessionSerializer

DOCUMENT_CHAT_SYSTEM_PROMPT = (
    "Eres Ecofilia, un asistente especializado en análisis documental. "
    'El usuario está analizando el documento "{doc_name}". '
    "Todas las preguntas se refieren exclusivamente a este documento. "
    "Responde siempre basándote en el contenido recuperado del documento. "
    "Si la información no aparece en el contexto proporcionado, indícalo claramente "
    "en lugar de inventar o hacer referencia a otros documentos."
)


def _can_manage_public_documents(user) -> bool:
    if not user:
        return False
    return bool(user.is_superuser or getattr(user, "role", None) == UserRole.ADMIN)


class RAGQueryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        query_text = request.query_params.get("query")
        if not query_text:
            return Response({"error": "Missing query parameter"}, status=status.HTTP_400_BAD_REQUEST)

        
        user = request.user
        if user.is_staff:
            qs = SmartChunk.objects.all()
        else:
            # Incluir chunks de documentos propios, públicos, compartidos y de proyectos compartidos
            from apps.project.models import ProjectShare
            shared_project_ids = ProjectShare.objects.filter(
                user=user
            ).values_list('project_id', flat=True)
            qs = SmartChunk.objects.filter(
                Q(document__owner=user) 
                | Q(document__is_public=True) 
                | Q(document__shares__user=user)
                | Q(document__projects__id__in=shared_project_ids)
            ).distinct()

        slugs = request.query_params.getlist("documents")
        if slugs:
            qs = qs.filter(document__slug__in=slugs)

        public_param = request.query_params.get("public")

        if public_param is not None:
            if public_param.lower() == "false":
                qs = qs.filter(document__is_public=False)
            elif public_param.lower() != "true":
                return Response({"error": "Invalid 'public' value. Use 'true' or 'false'."},
                                status=status.HTTP_400_BAD_REQUEST)

        try:
            chunks = qs.top_similar(query_text)
            serializer = SmartChunkSerializer(chunks, many=True)
            return Response({"query": query_text, "results": serializer.data})

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DocumentCreateAPIView(CreateAPIView):
    queryset = Document.objects.all()
    serializer_class = DocumentCreateSerializer
    permission_classes = [permissions.IsAuthenticated]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = serializer.save(owner=request.user)

        project = getattr(serializer, '_project', None)
        if project:
            from apps.project.models import ProjectDocument
            ProjectDocument.objects.get_or_create(
                project=project,
                document=document,
                defaults={"added_by": request.user},
            )

        response_serializer = DocumentSerializer(document, context=self.get_serializer_context())
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class DocumentBulkCreateAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        files = request.FILES.getlist('files') or request.FILES.getlist('files[]')

        if not files:
            return Response(
                {"error": "No files provided. Use 'files' or 'files[]' field(s)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = {'files': files}
        for key in ('name', 'category', 'category_slug', 'description', 'is_public', 'project_slug'):
            if key in request.data:
                data[key] = request.data[key]

        serializer = DocumentBulkCreateSerializer(
            data=data,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)
        validated_data = serializer.validated_data
        validated_files = validated_data['files']

        from apps.document.category_utils import resolve_write_category

        category_slug = validated_data.get('category_slug') or None
        category_text = validated_data.get('category') or None
        try:
            if category_slug and str(category_slug).strip():
                cat, cat_str = resolve_write_category(
                    request.user,
                    category_slug=str(category_slug).strip(),
                    is_staff=request.user.is_staff,
                )
            else:
                cat, cat_str = resolve_write_category(
                    request.user,
                    category_name=category_text,
                    is_staff=request.user.is_staff,
                )
        except ValueError as e:
            code = str(e)
            if code == "category_not_found":
                return Response(
                    {"error": "Category not found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {"error": "You do not have permission to use this category."},
                status=status.HTTP_403_FORBIDDEN,
            )

        document_props = {"category": cat_str, "category_ref": cat}
        for key in ('name', 'description', 'is_public'):
            if key in validated_data:
                document_props[key] = validated_data[key]

        project = getattr(serializer, '_project', None)
        successful = []
        failed = []
        created_documents = []

        for file in validated_files:
            try:
                document = Document.objects.create(
                    owner=request.user,
                    file=file,
                    **document_props,
                )
                created_documents.append(document)
                successful.append({
                    'filename': getattr(file, 'name', 'unknown'),
                    'id': document.id,
                    'slug': document.slug,
                })

                if project:
                    from apps.project.models import ProjectDocument
                    ProjectDocument.objects.get_or_create(
                        project=project,
                        document=document,
                        defaults={"added_by": request.user},
                    )
            except Exception as e:
                failed.append({
                    'filename': getattr(file, 'name', 'unknown'),
                    'error': str(e),
                })

        ctx = {'request': request, 'view': self}
        response_serializer = DocumentSerializer(
            created_documents, many=True, context=ctx,
        )

        response_data = {
            'successful': successful,
            'failed': failed,
            'created': len(successful),
            'documents': response_serializer.data,
        }
        if failed:
            response_data['errors'] = failed

        if not failed:
            return Response(response_data, status=status.HTTP_201_CREATED)
        elif successful:
            return Response(response_data, status=status.HTTP_207_MULTI_STATUS)
        else:
            return Response(
                {"message": "Failed to upload documents", "failed": failed},
                status=status.HTTP_400_BAD_REQUEST,
            )


class DocumentBulkPublicAPIView(APIView):
    """
    Superadmins and admin users only: bulk set is_public for the public library.
    Applies to every matched slug (any owner); unknown slugs are skipped.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        if not _can_manage_public_documents(request.user):
            raise PermissionDenied("Only superadmins and admin users can bulk change document visibility.")

        serializer = DocumentBulkPublicSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw_slugs = serializer.validated_data["slugs"]
        slugs = list(dict.fromkeys(raw_slugs))
        is_public = serializer.validated_data["is_public"]

        qs = Document.objects.filter(slug__in=slugs)
        matched = qs.count()
        updated = qs.update(is_public=is_public)

        return Response(
            {
                "updated": updated,
                "matched": matched,
                "requested": len(slugs),
            },
            status=status.HTTP_200_OK,
        )


def _expand_ancestor_ids(used_ids: set) -> set:
    if not used_ids:
        return set()
    out = set(used_ids)
    frontier = list(used_ids)
    while frontier:
        parents = {
            p
            for p in Category.objects.filter(id__in=frontier).values_list("parent_id", flat=True)
            if p is not None
        }
        new = [p for p in parents if p not in out]
        for p in new:
            out.add(p)
        frontier = new
    return out


def _build_topic_tree_for_documents(qs):
    """
    Build a flat topic tree from documents' `topics` ArrayField.
    Returns the same shape as _build_category_tree_for_documents so the
    frontend can use LibraryCategoryTreeResponse for both.
    """
    from django.db.models import Func, CharField

    class _Unnest(Func):
        function = 'UNNEST'

    topic_counts = (
        qs
        .filter(topics__len__gt=0)
        .annotate(_topic=_Unnest('topics', output_field=CharField()))
        .values('_topic')
        .annotate(c=Count('id', distinct=True))
        .order_by('_topic')
    )

    tree = [
        {
            "slug": row['_topic'],
            "name": row['_topic'],
            "parent_slug": None,
            "document_count_direct": row['c'],
            "document_count_total": row['c'],
            "children": [],
        }
        for row in topic_counts
        if row['_topic']
    ]
    unc = qs.filter(topics__len=0).count()
    return {"uncategorized_count": unc, "tree": tree}


def _build_category_tree_for_documents(qs):
    """
    Build nested category tree for a document queryset.
    Counts: document_count_direct (only docs in this node), document_count_total (subtree).
    """
    from django.db.models import Count

    direct_rows = (
        qs.exclude(category_ref__isnull=True)
        .values("category_ref_id")
        .annotate(c=Count("id"))
    )
    direct = {r["category_ref_id"]: r["c"] for r in direct_rows}
    if not direct:
        unc = qs.filter(
            Q(category_ref__isnull=True) & (Q(category__isnull=True) | Q(category=""))
        ).count()
        return {"uncategorized_count": unc, "tree": []}

    used = _expand_ancestor_ids(set(direct.keys()))
    categories = list(
        Category.objects.filter(id__in=used)
        .select_related("parent")
        .order_by("name")
    )
    by_parent = defaultdict(list)
    for c in categories:
        by_parent[c.parent_id].append(c)

    def total_for(cat_id: int) -> int:
        t = 0
        for did in category_descendant_ids(cat_id):
            t += direct.get(did, 0)
        return t

    def build_node(c):
        children_cats = sorted(by_parent.get(c.id, []), key=lambda x: (x.name or "").lower())
        return {
            "slug": c.slug,
            "name": c.name,
            "parent_slug": c.parent.slug if c.parent_id else None,
            "document_count_direct": direct.get(c.id, 0),
            "document_count_total": total_for(c.id),
            "children": [build_node(ch) for ch in children_cats],
        }

    root_cats = sorted(by_parent.get(None, []), key=lambda x: (x.name or "").lower())
    tree = [build_node(c) for c in root_cats]
    unc = qs.filter(
        Q(category_ref__isnull=True) & (Q(category__isnull=True) | Q(category=""))
    ).count()
    return {"uncategorized_count": unc, "tree": tree}


def _document_list_sort_order(request):
    sort = request.query_params.get("sort", "recent")
    order_map = {
        "recent": ("-created_at",),
        "oldest": ("created_at",),
        "title": ("name",),
        "title-desc": ("-name",),
        "year-desc": ("-year", "name"),
        "year-asc": ("year", "name"),
    }
    return order_map.get(sort, ("-created_at",))


def _library_category_queryset(queryset, library_category, subtree: bool = True):
    if library_category == "__uncategorized__":
        return queryset.filter(
            Q(category_ref__isnull=True) & (Q(category__isnull=True) | Q(category=""))
        )
    if not library_category:
        return queryset
    try:
        cat = Category.objects.get(slug=library_category)
    except Category.DoesNotExist:
        return queryset.filter(category=library_category)
    if subtree:
        return queryset.filter(category_ref_id__in=category_descendant_ids(cat.id))
    return queryset.filter(category_ref_id=cat.id)


class PublicDocumentListPagination(PageNumberPagination):
    page_size = 20
    page_query_param = "page"
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_page_size(self, request):
        raw = request.query_params.get("per_page")
        if raw is not None:
            try:
                n = int(raw)
                if n > 0:
                    return min(n, self.max_page_size)
            except ValueError:
                pass
        return super().get_page_size(request)


class DocumentListAPIView(ListAPIView):
    queryset = Document.objects.all()
    serializer_class = DocumentSerializer
    filterset_class = DocumentFilter
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return self._get_queryset_base().select_related("category_ref", "owner")

    def _get_queryset_base(self):
        qs = Document.objects.all()
        user = self.request.user
        scope = self.request.query_params.get("scope", "all")

        if user.is_staff:
            if scope == "own":
                qs = qs.filter(owner=user)
            elif scope == "public":
                qs = qs.filter(is_public=True)
            elif scope == "shared":
                qs = qs.filter(shares__user=user).exclude(owner=user).distinct()
        else:
            if scope == "own":
                qs = qs.filter(owner=user)
            elif scope == "public":
                qs = qs.filter(is_public=True)
            elif scope == "shared":
                qs = qs.filter(shares__user=user).exclude(owner=user).distinct()
            else:
                from apps.project.models import ProjectShare

                shared_project_ids = ProjectShare.objects.filter(
                    user=user
                ).values_list("project_id", flat=True)
                qs = qs.filter(
                    Q(owner=user)
                    | Q(is_public=True)
                    | Q(shares__user=user)
                    | Q(projects__id__in=shared_project_ids)
                ).distinct()
        return qs

    def list(self, request, *args, **kwargs):
        if request.query_params.get("summary") == "topic_tree":
            scope = request.query_params.get("scope", "all")
            if scope not in ("public", "own", "all"):
                return Response(
                    {"detail": "summary=topic_tree requires scope=public, own, or all"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = self.filter_queryset(self.get_queryset())
            tree_data = _build_topic_tree_for_documents(qs)
            return Response(tree_data)

        if request.query_params.get("summary") == "category_tree":
            scope = request.query_params.get("scope", "all")
            if scope not in ("public", "own", "all"):
                return Response(
                    {"detail": "summary=category_tree requires scope=public, own, or all"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = self.filter_queryset(self.get_queryset())
            tree_data = _build_category_tree_for_documents(qs)
            return Response(tree_data)

        if request.query_params.get("summary") == "categories":
            if request.query_params.get("scope") != "public":
                return Response(
                    {"detail": "summary=categories is only supported with scope=public"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            qs = self.filter_queryset(self.get_queryset())
            aggregated = (
                qs.annotate(
                    cat_key=Case(
                        When(
                            Q(category_ref__isnull=False),
                            then=F("category_ref__slug"),
                        ),
                        When(
                            Q(category__isnull=True) | Q(category=""),
                            then=Value("__uncategorized__"),
                        ),
                        default=F("category"),
                        output_field=CharField(),
                    )
                )
                .values("cat_key")
                .annotate(document_count=Count("id"))
                .order_by("cat_key")
            )
            return Response(
                {
                    "categories": [
                        {"category": row["cat_key"], "document_count": row["document_count"]}
                        for row in aggregated
                    ]
                }
            )

        if request.query_params.get("summary") == "topics":
            qs = self.filter_queryset(self.get_queryset())
            rows = (
                qs.annotate(topic=Func("topics", function="unnest", output_field=TextField()))
                .values("topic")
                .annotate(count=Count("id", distinct=True))
                .order_by("-count")
            )
            return Response([{"topic": r["topic"], "count": r["count"]} for r in rows if r["topic"]])

        ids_param = request.query_params.get("ids")
        if ids_param is not None and ids_param.strip() != "":
            id_list = []
            for part in ids_param.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    id_list.append(int(part))
                except ValueError:
                    return Response(
                        {"detail": "Invalid ids parameter; use comma-separated integers"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            if len(id_list) > 200:
                id_list = id_list[:200]
            if not id_list:
                return Response([])
            qs = self.filter_queryset(self.get_queryset()).filter(id__in=id_list)
            serializer = self.get_serializer(qs, many=True)
            return Response(serializer.data)

        paginate_flag = request.query_params.get("paginate", "").lower() in ("1", "true", "yes")
        scope = request.query_params.get("scope", "all")
        if paginate_flag and scope in ("own", "public", "all"):
            queryset = self.filter_queryset(self.get_queryset())
            library_category = request.query_params.get("library_category")
            subtree = request.query_params.get("library_category_subtree", "1").lower() in (
                "1",
                "true",
                "yes",
                "",
            )
            if library_category:
                queryset = _library_category_queryset(
                    queryset, library_category, subtree=subtree
                )
            topic = request.query_params.get("topic")
            if topic:
                if topic == "__uncategorized__":
                    queryset = queryset.filter(topics__len=0)
                else:
                    queryset = queryset.filter(topics__contains=[topic])
            queryset = queryset.order_by(*_document_list_sort_order(request))
            paginator = PublicDocumentListPagination()
            page = paginator.paginate_queryset(queryset, request, view=self)
            serializer = self.get_serializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        return super().list(request, *args, **kwargs)


class DocumentAccessPermission(permissions.BasePermission):
    """
    Permission class for document access:
    - Read: owner, staff users, public documents, shared documents, or documents in shared projects
    - Write/Delete: owner, staff, or editor role in share (for public docs, only superuser)
    """
    
    def has_object_permission(self, request, view, obj):
        # Staff users have full access
        if request.user.is_staff:
            return True
        
        # For safe methods (GET, HEAD, OPTIONS)
        if request.method in permissions.SAFE_METHODS:
            return obj.can_view(request.user)
        
        # For write/delete methods
        # - If document is public, only superuser can modify/delete
        if obj.is_public:
            return request.user.is_superuser
        
        # Owner can always modify/delete
        if obj.owner == request.user:
            return True
        
        # Check if user has editor role in share
        return obj.can_edit(request.user)


class DocumentViewSet(
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    ViewSet for retrieving, updating, and deleting document instances.
    Also handles sharing functionality.
    """
    queryset = Document.objects.all()
    permission_classes = [permissions.IsAuthenticated, DocumentAccessPermission]
    lookup_field = 'slug'
    lookup_url_kwarg = 'slug'

    def get_permissions(self):
        # chat_session only reads the document; the action itself checks can_view().
        # Excluding DocumentAccessPermission avoids the superuser-only gate that
        # fires for non-SAFE methods on public documents.
        if getattr(self, 'action', None) == 'chat_session':
            return [permissions.IsAuthenticated()]
        return super().get_permissions()

    def get_serializer_class(self):
        """Use different serializers for different operations"""
        if self.action == 'retrieve':
            return DocumentDetailSerializer
        elif self.action in ['update', 'partial_update']:
            return DocumentUpdateSerializer
        return DocumentSerializer
    
    def get_queryset(self):
        """Filter queryset based on user permissions"""
        qs = Document.objects.all().select_related("category_ref", "owner")
        user = self.request.user
        if not user.is_staff:
            # Incluir documentos propios, públicos, compartidos y de proyectos compartidos
            from apps.project.models import ProjectShare
            shared_project_ids = ProjectShare.objects.filter(
                user=user
            ).values_list('project_id', flat=True)
            qs = qs.filter(
                Q(owner=user) 
                | Q(is_public=True) 
                | Q(shares__user=user)
                | Q(projects__id__in=shared_project_ids)
            ).distinct()
        return qs
    
    def perform_destroy(self, instance):
        """Delete the document instance"""
        instance.delete()

    @action(
        detail=True,
        methods=["get"],
        url_path=r"chunks/(?P<chunk_index>\d+)",
        url_name="chunk-detail",
    )
    def chunk_detail(self, request, slug=None, chunk_index=None):
        """Return a single chunk for citation/source viewers."""
        document = self.get_object()
        chunk = get_object_or_404(
            document.chunks.all(),
            chunk_index=chunk_index,
        )
        serializer = DocumentChunkSerializer(
            chunk,
            context=self.get_serializer_context(),
        )
        return Response(serializer.data)
    
    @action(
        detail=True,
        methods=["post"],
        url_path="reprocess",
        url_name="reprocess",
        permission_classes=[permissions.IsAuthenticated],
    )
    def reprocess(self, request, slug=None):
        """Vuelve a correr la ingesta sobre este documento.

        Existe porque el pipeline mejora con el tiempo — mejor extractor, OCR,
        tablas — y un documento ya cargado se queda con el resultado del día en
        que se subió. Reprocesar la biblioteca entera no se justifica (se midió:
        solo aporta números de página), así que la decisión queda en manos de
        quien conoce el documento.

        Resetea el estado antes de despachar: la guarda anti-duplicados de
        ``process_document_chunks`` rechaza cualquier documento que ya esté en
        PROCESSING o DONE, así que sin este reset la tarea saldría sin hacer
        nada. Eso también permite recuperar documentos trabados en PROCESSING
        porque murió el worker.
        """
        document = self.get_object()

        if not document.can_edit(request.user):
            return Response(
                {"detail": "No tenés permisos para reprocesar este documento."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not document.file:
            return Response(
                {"detail": "El documento no tiene un archivo asociado."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Un documento que se está procesando ahora mismo no se toca: resetear
        # su estado pondría dos corridas a escribir sobre los mismos chunks.
        # Pasado el margen se asume worker muerto y se permite recuperarlo.
        if document.chunking_status == ChunkingStatus.PROCESSING:
            stale_after = timezone.now() - timedelta(
                hours=int(os.environ.get("DOC_PROCESSING_STALE_HOURS", "2"))
            )
            started = document.created_at
            if started and started > stale_after:
                return Response(
                    {"detail": "El documento ya se está procesando. Esperá a que termine."},
                    status=status.HTTP_409_CONFLICT,
                )

        Document.objects.filter(pk=document.pk).update(
            chunking_status=ChunkingStatus.PENDING,
            chunking_done=False,
            last_error="",
            retry_count=F("retry_count") + 1,
        )

        transaction.on_commit(lambda: process_document_chunks.delay(document.pk))

        document.refresh_from_db()
        serializer = DocumentSerializer(
            document,
            context=self.get_serializer_context(),
        )
        return Response(serializer.data, status=status.HTTP_202_ACCEPTED)

    @action(
        detail=True,
        methods=["get", "post"],
        url_path="shares",
        url_name="shares",
    )
    def shares(self, request, slug=None):
        """List or create document shares"""
        document = self.get_object()
        if not document.can_manage_shares(request.user):
            raise PermissionDenied("No puedes administrar los permisos de este documento.")
        
        if request.method == "GET":
            serializer = DocumentShareSerializer(
                document.shares.select_related("user"),
                many=True,
            )
            return Response(serializer.data)
        
        serializer = DocumentShareWriteSerializer(
            data=request.data,
            context={"document": document, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        share, created = DocumentShare.objects.update_or_create(
            document=document,
            user=serializer.validated_data["user"],
            defaults={"role": serializer.validated_data["role"]},
        )
        output = DocumentShareSerializer(share)
        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(output.data, status=status_code)
    
    @action(
        detail=True,
        methods=["patch", "delete"],
        url_path=r"shares/(?P<share_id>[^/]+)",
        url_name="share-detail",
    )
    def manage_share(self, request, slug=None, share_id=None):
        """Update or delete a document share"""
        document = self.get_object()
        if not document.can_manage_shares(request.user):
            raise PermissionDenied("No puedes administrar los permisos de este documento.")
        
        share = get_object_or_404(DocumentShare, document=document, pk=share_id)
        
        if request.method == "PATCH":
            serializer = DocumentShareRoleUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            share.role = serializer.validated_data["role"]
            share.save(update_fields=["role"])
            return Response(DocumentShareSerializer(share).data)
        
        share.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    
    @action(
        detail=True,
        methods=["get", "post"],
        url_path="chat-session",
        url_name="chat-session",
    )
    def chat_session(self, request, slug=None):
        """
        Obtener o crear la sesión de chat asociada a este documento.
        GET: Retorna la sesión existente o 404 si no existe
        POST: Crea una nueva sesión si no existe, o retorna la existente
        """
        document = self.get_object()
        
        # Verificar permisos de visualización
        if not document.can_view(request.user):
            raise PermissionDenied("No tienes permisos para ver este documento.")
        
        # Buscar sesión existente
        session = ChatSession.objects.filter(
            primary_document=document,
            owner=request.user
        ).first()
        
        if request.method == "GET":
            if session:
                serializer = ChatSessionSerializer(
                    session,
                    context={"request": request}
                )
                return Response(serializer.data)
            return Response(
                {"detail": "No hay sesión de chat para este documento."},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # POST: Crear sesión si no existe
        if session:
            serializer = ChatSessionSerializer(
                session,
                context={"request": request}
            )
            return Response(serializer.data, status=status.HTTP_200_OK)
        
        # Crear nueva sesión (body opcional: title, system_prompt, model, etc.)
        data = request.data if isinstance(request.data, dict) else {}
        doc_name = document.name or document.slug
        title = (data.get("title") or "").strip() or f"Chat: {doc_name}"
        system_prompt = (data.get("system_prompt") or "").strip() or (
            DOCUMENT_CHAT_SYSTEM_PROMPT.format(doc_name=doc_name)
        )
        model = (data.get("model") or "").strip() or DEFAULT_CHAT_MODEL
        language = (data.get("language") or "").strip() or "es"
        try:
            temperature = float(data.get("temperature", 0.3))
        except (TypeError, ValueError):
            temperature = 0.3

        session = ChatSession.objects.create(
            owner=request.user,
            primary_document=document,
            title=title,
            system_prompt=system_prompt,
            model=model,
            temperature=temperature,
            language=language,
        )
        session.allowed_documents.add(document)

        serializer = ChatSessionSerializer(
            session,
            context={"request": request}
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class TopicsAutocompleteView(APIView):
    """GET /document/topics/autocomplete/?q=<query>&limit=<n>
    Returns a list of topic strings matching the query from documents the
    user can access.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        q = request.query_params.get("q", "").strip().lower()
        try:
            limit = min(int(request.query_params.get("limit", 10)), 50)
        except (TypeError, ValueError):
            limit = 10

        user = request.user
        qs = Document.objects.all()
        if not user.is_staff:
            from apps.project.models import ProjectShare
            shared_project_ids = ProjectShare.objects.filter(
                user=user
            ).values_list('project_id', flat=True)
            qs = qs.filter(
                Q(owner=user)
                | Q(is_public=True)
                | Q(shares__user=user)
                | Q(projects__id__in=shared_project_ids)
            ).distinct()

        all_topics_lists = qs.exclude(topics__len=0).values_list('topics', flat=True)

        seen = set()
        results = []
        for topic_list in all_topics_lists:
            if not topic_list:
                continue
            for topic in topic_list:
                if not topic:
                    continue
                if topic in seen:
                    continue
                if not q or q in topic.lower():
                    seen.add(topic)
                    results.append(topic)

        results.sort()
        return Response(results[:limit])


class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.none()
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = 'slug'

    def get_queryset(self):
        user = self.request.user
        scope = (self.request.query_params.get("scope") or "").strip().lower()
        if user.is_staff and scope == "all":
            return Category.objects.all()
        return Category.objects.filter(owner=user)

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return CategoryWriteSerializer
        return CategorySerializer

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def perform_update(self, serializer):
        instance = self.get_object()
        if instance.owner != self.request.user and not self.request.user.is_staff:
            raise PermissionDenied("You do not have permission to edit this category.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.owner != self.request.user and not self.request.user.is_staff:
            raise PermissionDenied("You do not have permission to delete this category.")
        instance.delete()