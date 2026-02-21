from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import Skill
from .serializers import SkillSerializer
from .services import SkillService
import threading

class SkillViewSet(viewsets.ModelViewSet):
    serializer_class = SkillSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        return (Skill.objects.filter(user=user) | Skill.objects.filter(is_shared=True)).distinct()

    def perform_create(self, serializer):
        skill = serializer.save(user=self.request.user)
        # Offload embedding update
        self._trigger_embedding_update(skill)

    def perform_update(self, serializer):
        skill = serializer.save()
        # Offload embedding update
        self._trigger_embedding_update(skill)

    def _trigger_embedding_update(self, skill):
        def update_task():
            import asyncio
            service = SkillService()
            asyncio.run(service.update_embedding(skill))
        
        threading.Thread(target=update_task).start()

    @action(detail=False, methods=['get'])
    def search(self, request):
        """
        Hybrid search endpoint.
        """
        from asgiref.sync import async_to_sync
        from .serializers import SkillSearchSerializer
        
        serializer = SkillSearchSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        
        params = serializer.validated_data
        
        service = SkillService()
        results = async_to_sync(service.hybrid_search)(
            query=params['query'],
            user=request.user,
            category = params.get('category'),
            page=params['page'],
            page_size=params['page_size'],
            tab=params['tab']
        )
        
        item_serializer = self.get_serializer(results['items'], many=True)
        return Response({
            'results': item_serializer.data,
            'total': results['total'],
            'page': results['page'],
            'pages': results['pages']
        })

    @action(detail=True, methods=['post'])
    def share(self, request, pk=None):
        """
        Toggle public sharing for a skill.
        """
        skill = self.get_object()
        
        # Security: only owner can share
        if skill.user != request.user:
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)
            
        skill.is_shared = not skill.is_shared
        skill.save()
        
        return Response({
            'id': skill.id,
            'is_shared': skill.is_shared,
            'message': f'Skill is now {"shared publicly" if skill.is_shared else "private"}'
        })

    @action(detail=True, methods=['post'])
    def fork(self, request, pk=None):
        """
        Incorporate (clone) a public skill into the user's own collection.
        """
        public_skill = self.get_object()
        
        # Security: can only fork public skills
        if not public_skill.is_shared and public_skill.user != request.user:
            return Response({'error': 'Skill is not public'}, status=status.HTTP_403_FORBIDDEN)
            
        # Create a new copy for the current user
        new_skill = Skill.objects.create(
            user=request.user,
            title=f"{public_skill.title} (Copy)",
            description=public_skill.description,
            content=public_skill.content,
            category=public_skill.category,
            author_name=public_skill.author_name, # Keep original author attribution
            is_shared=False # Initial copy is private
        )
        
        # Trigger background embedding update for the new copy
        self._trigger_embedding_update(new_skill)
        
        return Response(self.get_serializer(new_skill).data, status=status.HTTP_201_CREATED)
